"""
上下文管理模块 — 为 Nexent Agent 提供基于 LLM 摘要的上下文压缩能力。
 
设计原则：
  1. 独立模块，不修改现有类的继承关系
  2. 通过开关控制，关闭时行为与原系统完全一致（baseline）
  3. 基于 token 阈值触发压缩，而非固定轮次截断
  4. 使用 LLM 生成结构化摘要替换旧步骤，保留语义而非丢弃信息
  5. 保留最近 N 步不压缩，保证当前推理链完整
 
 
用法：

    def _step_stream(self, ...):
        memory_messages = self.write_memory_to_messages()
        input_messages = memory_messages.copy()

        if self.context_manager and self.context_manager.config.enabled:
            input_messages = self.context_manager.compress_if_needed(
                self.memory, input_messages, self.model, self._history_step_count
            )        
    ...
"""



import json 
import logging
from dataclasses import dataclass, field 
from typing import Any, Dict, List, Optional, TYPE_CHECKING, Union
from smolagents.memory import ActionStep, TaskStep, AgentMemory, MemoryStep
from smolagents.models import ChatMessage, MessageRole
import hashlib
import re 
logger = logging.getLogger("agent_context")

@dataclass 
class PreviousSummaryCache:
    """缓存已压缩的 previous-run 摘要。
    这里的缓存同样不只在于每次New Run刚开启的时候
    还在于current run每一次 _step_stream，都会携带累积的历史包括 previous complete run 还有 current just-appeared run"""
    summary_text: str
    """摘要内容。"""

    covered_pairs: int
    """此摘要覆盖了前 N 个 T-A 对。"""

    anchor_fingerprint: str
    """被覆盖的最后一个 T-A 对的指纹，用于校验一致性。
    = hash(last_covered_task.content + last_covered_action.content)"""

@dataclass 
class CurrentSummaryCache:
    """专门面向current run的摘要压缩, current run 是 Task, Action,Action,...
    A的话会有assisant(思考），tool-call(calling tools), tool-response(observation),assistant(思考）
    而对于一些简单任务，则大概不会有tool-call, tool-response"""
    summary_text: str
    end_steps: int 
    # 此摘要作用的最后step的索引
    anchor_fingerprint:str
    # end_step的指纹，双重验证

@dataclass 
class ContextManagerConfig:
    """
    Context manager configuration with sensible defaults for all fields.
    """

    enabled: bool = False
    """Master switch. False = baseline mode, no compression."""

    token_threshold: int = 60000
    """Token estimation threshold that triggers compression. Triggers compression
    when the estimated token count of all serialized steps in memory exceeds this value.
    Recommended to set 60%-75% of the model's context window."""

    keep_recent_steps: int = 4
    """
    面向 current run steps的保留
    注意 tool-call, tool-response的配对
    """
    keep_recent_pairs: int = 2
    """
    面向 previous history pairs的保留
    注意 TaskStep与ActionStep的配对保留
    """
    max_chunk_count: int = 5 
    """最大的'块数', 这里的一块就对应于一个临近threshold的多个TA对; 这对应于前端页面关闭后又重新开启的时候, 历史history重新导入
    时, 此时的历史对话数量可能很多, 不能对全量历史做摘要, 而是需要先map式地摘要, 然后再reduce式地总结."""

    max_observation_length: int = 500
    """Maximum characters to retain for old step observations.
    对于tool-call/response而言, 前者对应Calling tools，后者对应Observation"""


    summary_system_prompt: str = (
        "你是一个对话摘要助手。请将以下对话历史压缩为结构化摘要，"
        "保留所有关键信息：用户的核心需求、已完成的工作、重要发现和决策、"
        "待办事项、需要保留的上下文。输出严格 JSON 格式，不要包含 markdown 代码块标记。"
    )

    summary_json_schema: Dict[str, Any] = field(default_factory=lambda: {
        "task_overview": "用户的核心请求与成功标准（≤150字）",
        "completed_work": "已完成的工作、产出的文件或结果（≤200字）",
        "key_decisions": "重要发现、做出的决策及其理由（≤200字）",
        "pending_items": "待完成的具体步骤、阻塞项（≤150字）",
        "context_to_preserve": "用户偏好、领域细节、做出的承诺（≤150字）",
    })
    """摘要的 JSON schema 描述，作为 prompt 的一部分引导 LLM 输出。"""


    max_summary_input_chars: int = 30000
    """Maximum input characters to send to the summarization LLM.
    When exceeded, older content will be further truncated to prevent 
    the summary call itself from exceeding the model context."""


    chars_per_token: float = 1.5
    """Rough character/token conversion ratio for token estimation.
    Chinese: approximately 0.8-1.5 chars/token, English: approximately 3.5-4.5 chars/token.
    Default 2.5 leans toward Chinese scenarios (Nexent's primary language),
    Slightly overestimating token count to trigger compression early is preferable to underestimating and causing OOM.
    For pure English scenarios, recommend adjusting to 4.0.
    这里可以实现一个估算token的函数；估算token是有必要的，可能会想着不是可以得到input_token这些数值吗？但是这些数值是要先
    将message给LLM后才知道的，也就是在message送给LLM之前，总需要先估计的。"""

    # Previous Run  摘要配置
    # previous_summary_system_prompt: str = ""
    # previsou_json_schema: dict = field()
    # merge_system_prompt: str = "" # merge的时候，也可以不用LLM

    # Current run 摘要配置
    # current_summary_system_prompt: str = ""
    # current_json_schema: dict = field()
    # 注意 current run 下虽然是T,A,A,A ...，但是ActionStep对应的角色不止 assistant, 还有 tool-call, tool-response.


@dataclass
class CompressionCallRecord:
    call_type: str           # "previous_summary" | "current_summary" | "merge_summary" | "map_reduce_chunk"
    input_tokens: int = 0
    output_tokens: int = 0
    input_chars: int = 0   
    output_chars: int = 0    
    cache_hit: bool = False  
    details: dict = field(default_factory=dict)


@dataclass
class SummaryTaskStep(TaskStep):
    """Special TaskStep, summary generated by LLM 
    
    Inherits from TaskStep (dataclass) to maintain serialization compatibility (e.g., dict()),
    while overriding to_messages() to prevent the base class's default "New Task:" prefix from misleading
    the model into treating the summary as a new task.
    QUESTION: 目前继承自 TaskStep, 在转为 chatmessage的时候, 角色是作为 user
    """
    is_summary: bool = True
 
    def to_messages(self, summary_mode: bool = False) -> list:
        content = [{"type": "text", "text": f"Previous context summary:\n{self.task}"}]
        return [ChatMessage(role=MessageRole.USER, content=content)]




class ContextManager:
    def __init__(self, config: Optional[ContextManagerConfig] = None, max_steps: Optional[int] = None):
        self.config = config or ContextManagerConfig()
        self._previous_summary_cache: Optional[PreviousSummaryCache] = None
        self._current_summary_cache: Optional[CurrentSummaryCache] = None
        if max_steps is not None and self.config.keep_recent_steps >= max_steps:
            self.config.keep_recent_steps = max_steps
        self.compression_calls_log: List[CompressionCallRecord] = []
        self._step_local_log: List[CompressionCallRecord] = []  # 用于临时收集单次 compress_if_needed 内的调用

    def compress_if_needed(self, model, memory, original_messages:List[ChatMessage], current_run_start_idx) -> List[ChatMessage]:
        # 这里传入的 memory 应该是 memory 的 copy 不应该影响其原始值
        # 阶段 0: 开关检查与全局检查
        if not self.config.enabled:
            return original_messages
        if self._estimate_tokens(memory) <= self.config.token_threshold:
            return original_messages

        self._step_local_log.clear()

        # 阶段1：分段切分 & 分别估算
        prev_steps = memory.steps[:current_run_start_idx] # historical steps 不包括当前run内的step
        curr_steps = memory.steps[current_run_start_idx:] # current steps

        prev_tokens = self._estimate_tokens_for_steps(prev_steps)
        curr_tokens = self._estimate_tokens_for_steps(curr_steps)

        # 阶段2：决定prev与curr的压缩
        compress_prev, compress_curr = False, False
        if prev_tokens > self.config.token_threshold * 0.6:
            compress_prev = True
        if curr_tokens > self.config.token_threshold * 0.4:
            compress_curr = True 
        



        # 阶段3：先处理 compress_prev 的情况
        prev_summary_step = None # SummaryTaskStep or None 
        prev_tail_steps = []

        if compress_prev:
            prev_pairs = self._extract_pairs(prev_steps)
            if prev_pairs:
                keep_n = min(self.config.keep_recent_pairs, len(prev_pairs))
                pairs_to_compress = prev_pairs[:-keep_n] if keep_n > 0 else prev_pairs 
                pairs_to_keep = prev_pairs[-keep_n:] if keep_n > 0 else []

                if pairs_to_compress:
                    # 带缓存的 previous 压缩
                    summary_text = self._compress_previous_with_cache(
                        pairs_to_compress, model
                    )
                    if summary_text:
                        prev_summary_step = SummaryTaskStep(task=summary_text)
                        prev_tail_steps = self._pairs_to_steps(pairs_to_keep)

        curr_kept_steps = curr_steps # 流向 model 的 current task + actionsteps
        if compress_curr and curr_steps:

            curr_task = curr_steps[0] if isinstance(curr_steps[0], TaskStep) else None
            curr_action_steps = [s for s in curr_steps if isinstance(s, ActionStep)]

            keep_n = min(self.config.keep_recent_steps, len(curr_action_steps))
            # 确保 keep_n step中的tool-call与tool-response要相配对
            if curr_steps[-keep_n].observations is not None and curr_steps[-keep_n-1].tool_calls is not None:
                keep_n += 1
            actions_to_compress = curr_action_steps[:-keep_n] if keep_n > 0 else []
            actions_to_keep = curr_action_steps[-keep_n:] if keep_n > 0 else curr_action_steps

            if actions_to_compress:
                curr_summary_text = self._compress_current_with_cache(
                    curr_task, actions_to_compress, model
                )
                if curr_summary_text:
                    # 这里没有明确传入 当前 task
                    curr_kept_steps = [
                        SummaryTaskStep(task=curr_summary_text),
                        *actions_to_keep
                    ]
                else:
                    # 摘要失败，降级为截断版本;
                    # 这里 先不进行截断
                    truncated_actions = actions_to_compress
                    curr_kept_steps = (
                        ([curr_task] if curr_task else [])
                        + truncated_actions + actions_to_keep
                    )

        if not self._step_local_log:
            record = CompressionCallRecord(
                call_type="no_op",
                cache_hit=True,
                details={"reason": "all_cache_hit_or_no_content"}
            )

            self.compression_calls_log.append(record)
            self._step_local_log.append(record)
        return self._build_messages(
            memory, prev_summary_step, prev_tail_steps, curr_kept_steps
        )


    def _extract_pairs(self, steps):
        """将 steps 按 (TaskStep, ActionStep) 配对提取。
        这个需要再确认：如果有PlanningStep的话，仍需要更改"""
        pairs = []
        i = 0
        while i < len(steps):
            if isinstance(steps[i], TaskStep) and not isinstance(steps[i], SummaryTaskStep):
                if i + 1 < len(steps) and isinstance(steps[i + 1], ActionStep):
                    pairs.append((steps[i], steps[i + 1]))
                    i += 2
                    continue
            i += 1
        return pairs
    
    def _compress_previous_with_cache(
        self,
        pairs_to_compress: List[tuple],
        model
    ) -> Optional[str]:
        """
        对 previous 的 T-A 对做摘要压缩，带缓存增量机制。

        Args:
            pairs_to_compress: 需要压缩的 (TaskStep, ActionStep) 对列表
            model: LLM 模型实例

        Returns:
            摘要文本，失败返回 None
        """
        if not pairs_to_compress:
            return None

        # 检查缓存
        cached_summary, new_pairs = self._check_previous_cache(pairs_to_compress)

        if cached_summary is not None and not new_pairs:
            # 缓存完全命中，无新增
            return cached_summary

        if cached_summary is not None and new_pairs:
            # 缓存部分命中，有增量
            summary_text = self._merge_previous_incremental(
                cached_summary, new_pairs, model
            )
        else:
            # 缓存未命中，全量压缩
            summary_text = self._summarize_pairs(pairs_to_compress, model)

        # 更新缓存
        if summary_text:
            last_t, last_a = pairs_to_compress[-1]
            self._previous_summary_cache = PreviousSummaryCache(
                summary_text=summary_text,
                covered_pairs=len(pairs_to_compress),
                anchor_fingerprint=self._pair_fingerprint(
                    last_t.task or "", last_a.action_output or last_a.output or ""
                ),
            )

        return summary_text
    
    def _check_previous_cache(
        self,
        pairs: List[tuple]
    ) -> tuple:
        """
        校验 previous 缓存有效性。

        Returns:
            (cached_summary, new_pairs):
                - (str, [])       : 完全命中，无新增
                - (str, [pairs..]) : 部分命中，有增量对
                - (None, None)     : 缓存失效或不存在，需全量压缩
        """
        cache = self._previous_summary_cache
        if cache is None:
            return None, None

        n = cache.covered_pairs

        if n > len(pairs):
            # 对话被删减，缓存失效
            self._previous_summary_cache = None
            return None, None

        # 校验锚点：第 n 个对是否一致
        anchor_t, anchor_a = pairs[n - 1]
        fp = self._pair_fingerprint(
            anchor_t.task or "", anchor_a.action_output or ""
        )
        # 这里需要打开 借助前端实际运行，来看看这里的 prev run中的T,A对怎么去取内容

        if fp != cache.anchor_fingerprint:
            # 内容变化，缓存失效
            self._previous_summary_cache = None
            return None, None

        # 命中
        new_pairs = pairs[n:]
        return cache.summary_text, new_pairs

    def _pair_fingerprint(self, task_content: str, action_content: str) -> str:
        """取末尾若干字符做轻量hash，避免对超长content做全量hash。"""
        raw = (task_content[-200:] + action_content[-200:])
        return hashlib.md5(raw.encode()).hexdigest()
        
    def _merge_previous_incremental(
        self,
        cached_summary: str,
        new_pairs: List[tuple],
        model
    ) -> Optional[str]:
        """
        将已有缓存摘要与增量的新 T-A 对合并。
        短文本直接拼接，长文本调 LLM 合并。
        """
        new_text = self._pairs_to_text(new_pairs)
        combined = f"{cached_summary}\n\n[后续对话]\n{new_text}"

        if len(combined) <= self.config.max_summary_input_chars * 0.5:
            # 够短，直接拼接，不调 LLM
            return combined

        # 拼接后太长，用 LLM 合并
        return self._generate_merge_summary(combined, model)
    


    def _summarize_pairs(
        self,
        pairs: List[tuple],
        model
    ) -> Optional[str]:
        """
        对 T-A 对列表做全量摘要。
        内容短则直接单次摘要，内容长则 map-reduce。
        """
        full_text = self._pairs_to_text(pairs)

        if len(full_text) <= self.config.max_summary_input_chars:
            return self._generate_summary(full_text, model, call_type="previous_summary")

        # 内容过长，按对切分后 map-reduce
        chunks = self._split_pairs_into_chunks(pairs)
        return self._map_reduce_summarize(chunks, model)


    def _split_pairs_into_chunks(
        self,
        pairs: List[tuple]
    ) -> List[List[tuple]]:
        """
        将 T-A 对列表按字符长度切分为多个 chunk，
        每个 chunk 不超过 max_summary_input_chars。
        """
        chunks = []
        current_chunk = []
        current_size = 0
        max_size = self.config.max_summary_input_chars

        for pair in pairs:
            pair_text = self._pairs_to_text([pair])
            pair_size = len(pair_text)

            if current_size + pair_size > max_size and current_chunk:
                chunks.append(current_chunk)
                current_chunk = []
                current_size = 0

                if len(chunks) >= self.config.max_chunk_count - 1:
                    # 剩余全部塞入最后一块
                    current_chunk = list(pairs[pairs.index(pair):])
                    break

            current_chunk.append(pair)
            current_size += pair_size

        if current_chunk:
            chunks.append(current_chunk)

        return chunks


    def _map_reduce_summarize(
        self,
        chunks: List[List[tuple]],
        model
    ) -> Optional[str]:
        """Map: 每个 chunk 独立摘要。Reduce: 合并所有摘要。"""
        # Map
        chunk_summaries = []
        for idx, chunk in enumerate(chunks):
            text = self._pairs_to_text(chunk)
            summary = self._generate_summary(text, model)
            if summary:
                chunk_summaries.append(
                    f"[对话段 {idx+1}/{len(chunks)}]\n{summary}"
                )

        if not chunk_summaries:
            return None
        if len(chunk_summaries) == 1:
            return chunk_summaries[0]

        # Reduce
        merged_input = "\n\n---\n\n".join(chunk_summaries)
        return merged_input
        # 这里默认就先不过多次的压缩
        # if len(merged_input) <= 2000:
        #     return merged_input

        # return self._generate_merge_summary(merged_input, model)



    def _compress_current_with_cache(
        self,
        curr_task: Optional[TaskStep],
        actions_to_compress: List[ActionStep],
        model
    ) -> Optional[str]:
        """
        对 current run 中的旧 ActionStep 做 LLM 摘要，带缓存。

        缓存逻辑：current run 是 append-only 的，
        如果缓存覆盖了前 M 个 action，且第 M 个的指纹没变，
        则只对 M+1 之后的增量做摘要再合并。
        """
        if not actions_to_compress:
            return None

        # 构造指纹用于缓存校验
        last_action = actions_to_compress[-1]
        current_fp = self._action_fingerprint(last_action)

        # 检查缓存
        cache = self._current_summary_cache
        if cache is not None:
            if (cache.end_steps <= len(actions_to_compress)
                and cache.anchor_fingerprint == self._action_fingerprint(
                    actions_to_compress[cache.end_steps - 1]
                )):
                # 缓存命中
                new_actions = actions_to_compress[cache.end_steps:]
                if not new_actions:
                    return cache.summary_text

                # 增量部分转文本
                new_text = self._actions_to_text(new_actions)
                combined = f"{cache.summary_text}\n\n[后续步骤]\n{new_text}"

                if len(combined) <= self.config.max_summary_input_chars * 0.5:
                    summary_text = combined
                else:
                    summary_text = self._generate_merge_summary(combined, model)
                    if not summary_text:
                        summary_text = combined  # merge 失败，降级为拼接

                self._current_summary_cache = CurrentSummaryCache(
                    summary_text=summary_text,
                    end_steps=len(actions_to_compress),
                    anchor_fingerprint=current_fp,
                )
                return summary_text

        # 缓存未命中，全量摘要
        task_text = f"当前任务: {curr_task.task}\n\n" if curr_task else ""
        actions_text = self._actions_to_text(actions_to_compress)
        full_text = task_text + actions_text

        if len(full_text) > self.config.max_summary_input_chars:
            full_text = full_text[:self.config.max_summary_input_chars]

        summary_text = self._generate_summary(full_text, model, call_type="current_summary")

        if summary_text:
            self._current_summary_cache = CurrentSummaryCache(
                summary_text=summary_text,
                end_steps=len(actions_to_compress),
                anchor_fingerprint=current_fp,
            )

        return summary_text


    def _actions_to_text(self, actions: List[ActionStep]) -> str:
        """将 ActionStep 列表转为结构化可读文本，保留工具调用链信息。"""
        parts = []
        for i, step in enumerate(actions):
            lines = [f"[步骤 {step.step_number or i+1}]"]

            if step.model_output:
                # 截取思考部分，避免过长
                thought = step.model_output[:500]
                lines.append(f"  思考: {thought}")

            if step.tool_calls:
                for tc in step.tool_calls:
                    args_preview = (tc.arguments[:200] + "...") if tc.arguments and len(tc.arguments) > 200 else (tc.arguments or "")
                    lines.append(f"  调用: {tc.name}({args_preview})")

            if step.observations:
                obs = step.observations[:self.config.max_observation_length]
                lines.append(f"  结果: {obs}")

            if step.action_output is not None:
                lines.append(f"  输出: {str(step.action_output)[:300]}")

            if step.error:
                lines.append(f"  错误: {str(step.error)[:200]}")

            parts.append("\n".join(lines))

        return "\n\n".join(parts)


    @staticmethod
    def _action_fingerprint(action: ActionStep) -> str:
        """取 ActionStep 的轻量指纹。"""
        raw = (
            str(action.step_number or "")
            + (action.model_output or "")[-200:]
            + (action.action_output or "" if isinstance(action.action_output, str)
            else str(action.action_output) or "")[-200:]
        )
        return hashlib.md5(raw.encode()).hexdigest()



    # ============================================================
    #  LLM 调用
    # ============================================================

    def _generate_summary(self, text: str, model, call_type: str = "summary") -> Optional[str]:
        """调用 LLM 生成结构化摘要。"""
        schema_desc = json.dumps(
            self.config.summary_json_schema, ensure_ascii=False, indent=2
        )
        user_prompt = (
            f"请按以下 JSON 结构输出摘要：\n{schema_desc}\n\n"
            f"需要摘要的对话内容：\n{text}"
        )

        try:
            messages = [
                ChatMessage(
                    role=MessageRole.SYSTEM,
                    content=[{"type": "text", "text": self.config.summary_system_prompt}]
                ),
                ChatMessage(
                    role=MessageRole.USER,
                    content=[{"type": "text", "text": user_prompt}]
                ),
            ]
            response = model(messages, stop_sequences=[])

            raw_output = response.content
            if isinstance(raw_output, list):
                raw_output = " ".join(
                    block.get("text", "")
                    for block in raw_output
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            if not isinstance(raw_output, str):
                raw_output = str(raw_output)
            
            summary = self._format_summary(raw_output)
            self._record_llm_call_token(input_len=self._msg_char_count(messages), output_len=len(raw_output), response=response,call_type=call_type)
            return summary

        except Exception as e:
            logger.error(f"摘要生成异常: {e}")
            return None


    def _generate_merge_summary(self, text: str, model) -> Optional[str]:
        """Reduce 阶段：将多段摘要合并为一个统一摘要。"""
        schema_desc = json.dumps(
            self.config.summary_json_schema, ensure_ascii=False, indent=2
        )
        merge_prompt = (
            f"请将以下多段对话摘要合并为一个统一摘要，"
            f"去除重复信息，保留所有关键内容。\n"
            f"按此 JSON 结构输出：\n{schema_desc}\n\n"
            f"需要合并的摘要：\n{text}"
        )

        try:
            messages = [
                ChatMessage(
                    role=MessageRole.SYSTEM,
                    content=[{"type": "text", "text": self.config.summary_system_prompt}]
                ),
                ChatMessage(
                    role=MessageRole.USER,
                    content=[{"type": "text", "text": merge_prompt}]
                ),
            ]
            response = model(messages, stop_sequences=[])

            raw_output = response.content
            if isinstance(raw_output, list):
                raw_output = " ".join(
                    block.get("text", "")
                    for block in raw_output
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            if not isinstance(raw_output, str):
                raw_output = str(raw_output)

            self._record_llm_call_token(input_len=self._msg_char_count(messages), output_len=len(raw_output), response=response,call_type="merge_summary")

            return self._format_summary(raw_output)

        except Exception as e:
            logger.error(f"摘要合并异常: {e}")
            return None

    def _record_llm_call_token(self, input_len, output_len, response, call_type):
        record = CompressionCallRecord(
            call_type = call_type,
            input_tokens=getattr(getattr(response, "token_usage", None), "input_tokens", 0) or 0,
            output_tokens=getattr(getattr(response, "token_usage", None), "output_tokens", 0) or 0,
            input_chars = input_len,
            output_chars = output_len
        )
        self.compression_calls_log.append(record)
        self._step_local_log.append(record)

    def _format_summary(self, raw_output: str) -> Optional[str]:
        """
        清洗 LLM 输出：去除 markdown 代码块标记，验证 JSON 可解析性。
        无论是否为合法 JSON，都返回清洗后的文本（摘要本身作为文本嵌入即可）。
        """
        cleaned = raw_output.strip()
        # 去除 ```json ... ``` 包裹
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```\s*$", "", cleaned)

        if not cleaned:
            return None

        # 尝试解析 JSON 以验证格式，但无论如何返回文本
        try:
            parsed = json.loads(cleaned)
            # 格式化为可读文本，便于后续作为 context 嵌入
            return json.dumps(parsed, ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            logger.warning("摘要输出非合法 JSON，将作为纯文本使用")
            return cleaned



    def _pairs_to_text(self, pairs: List[tuple]) -> str:
        """将 T-A 对列表转为可读文本，供摘要 LLM 使用。"""
        parts = []
        for i, (task_step, action_step) in enumerate(pairs):
            task_text = task_step.task or ""
            action_text = action_step.action_output or action_step.model_output or ""
            parts.append(
                f"[对话 {i+1}]\n"
                f"user: {task_text[:self.config.max_summary_input_chars // len(pairs)]}\n"
                f"assistant: {action_text[:self.config.max_summary_input_chars // len(pairs)]}"
            )
            # 这里使用 // 保证了每轮对话的均匀性。
        return "\n\n".join(parts)
    

    def _pairs_to_steps(self, pairs: List[tuple]) -> List[MemoryStep]:
        """将 [(TaskStep, ActionStep), ...] 还原为 [T, A, T, A, ...] 扁平列表。
        这里仍默认 History 为TaskStep, ActionStep，目前未看到 Planning Step"""
        steps = []
        for task_step, action_step in pairs:
            steps.append(task_step)
            steps.append(action_step)
        return steps

    def _build_messages(
        self,
        memory: AgentMemory,
        prev_summary_step: Optional[SummaryTaskStep],
        prev_tail_steps: List[MemoryStep],
        curr_kept_steps: List[MemoryStep]
    ) -> List[ChatMessage]:
        """将各部分组装为最终的 List[ChatMessage]。"""
        result = []

        # system prompt
        if memory.system_prompt:
            result.extend(memory.system_prompt.to_messages())

        # previous
        if prev_summary_step:
            result.extend(prev_summary_step.to_messages())
        for step in prev_tail_steps:
            result.extend(step.to_messages())

        # current
        for step in curr_kept_steps:
            result.extend(step.to_messages())

        return result

    ######################################
    ## 估计 Tokens (委托给模块级纯函数)
    ######################################

    def _estimate_tokens_for_steps(self, steps: MemoryStep):
        return estimate_tokens_for_steps(steps, self.config.chars_per_token)

    def _estimate_tokens(self, memory: AgentMemory) -> int:
        return estimate_tokens(memory, self.config.chars_per_token)

    def _msg_char_count(self, msg: Union[ChatMessage, List[ChatMessage]]) -> int:
        return msg_char_count(msg)

    def _msg_token_count(self, msg):
        return msg_token_count(msg, self.config.chars_per_token)

    def get_step_compression_stats(self) -> dict:
        """返回最近一次 compress_if_needed 的汇总统计"""
        if not self._step_local_log:
            return {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_hits": 0}

        return {
            "calls": len([r for r in self._step_local_log if not r.cache_hit]),
            "input_tokens": sum(r.input_tokens for r in self._step_local_log),
            "output_tokens": sum(r.output_tokens for r in self._step_local_log),
            "input_chars": sum(r.input_chars for r in self._step_local_log),
            "output_chars": sum(r.output_chars for r in self._step_local_log),
            "cache_hits": sum(1 for r in self._step_local_log if r.cache_hit),
        }

    def get_all_compression_stats(self) -> dict:
        """返回全生命周期的汇总统计"""
        real_calls = [r for r in self.compression_calls_log if not r.cache_hit]
        return {
            "total_calls": len(real_calls),
            "total_input_tokens": sum(r.input_tokens for r in real_calls),
            "total_output_tokens": sum(r.output_tokens for r in real_calls),
            "total_cache_hits": sum(1 for r in self.compression_calls_log if r.cache_hit),
        }


# =============================================================================
# 模块级纯工具函数 —— 供 ContextManager 与外部（如 CoreAgent）复用
# =============================================================================

def msg_char_count(msg: Union[ChatMessage, List[ChatMessage]]) -> int:
    """计算单条或多条 ChatMessage 的字符总数。
    兼容 content 为 str 或 list[{"type": "text", "text": "..."}] 的格式。
    """
    if isinstance(msg, list):
        return sum(msg_char_count(single_msg) for single_msg in msg)

    if isinstance(msg.content, str):
        return len(msg.content)
    if isinstance(msg.content, list):
        return sum(
            len(block.get("text", ""))
            for block in msg.content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return 0


def msg_token_count(msg: Union[ChatMessage, List[ChatMessage]], chars_per_token: float = 1.5) -> int:
    """单条或多条消息 token 估算。"""
    return int(msg_char_count(msg) / chars_per_token)


def estimate_tokens_for_steps(steps: List[MemoryStep], chars_per_token: float = 1.5) -> int:
    """精确估算 steps 的 token 数。"""
    total_tokens = 0
    for step in steps:
        total_tokens += msg_token_count(step.to_messages(), chars_per_token)
    return total_tokens


def estimate_tokens(memory: AgentMemory, chars_per_token: float = 1.5) -> int:
    """估算 memory 中当前上下文的 token 数。

    优先使用最后一个 ActionStep 的 input_tokens（它代表该步调用时的
    完整上下文长度），再加上该步之后新增步骤的文本估算。

    注意：不能简单累加所有步骤的 input_tokens，因为每步的 input_tokens
    已经是累积值（包含了前面所有步骤的内容）。
    """
    last_known_tokens = 0
    last_known_idx = -1
    for i, step in enumerate(memory.steps):
        if isinstance(step, ActionStep) and step.token_usage:
            last_known_tokens = step.token_usage.input_tokens
            last_known_idx = i

    if last_known_tokens > 0:
        incremental_chars = 0
        for step in memory.steps[last_known_idx + 1 :]:
            incremental_chars += msg_char_count(step.to_messages())
        return last_known_tokens + int(incremental_chars / chars_per_token)

    total_chars = 0
    if memory.system_prompt:
        total_chars += msg_char_count(memory.system_prompt.to_messages())
    for step in memory.steps:
        total_chars += msg_char_count(step.to_messages())
    return int(total_chars / chars_per_token)