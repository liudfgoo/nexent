# Nexent Agent 运行模型关键概念

> 基于 smolagents CodeAgent + nexent CoreAgent 实际代码，为持久化设计提供背景知识。

## 1. 核心数据结构（smolagents 定义，`memory.py`）

```
AgentMemory
  ├─ system_prompt: SystemPromptStep       # 系统提示词
  └─ steps: list[MemoryStep]               # 对话步骤列表
       ├─ TaskStep                          # 用户输入（task + images）
       ├─ ActionStep                        # 一次 ReAct 循环的完整产物
       ├─ PlanningStep                      # 规划步骤（nexent 未使用）
       └─ FinalAnswerStep                   # 最终答案
```

### ActionStep 字段（`memory.py:51-64`）

```python
@dataclass
class ActionStep(MemoryStep):
    step_number: int                                    # 步骤序号（1, 2, 3...）
    timing: Timing                                      # 计时
    model_input_messages: list[ChatMessage] | None      # 喂给 LLM 的输入
    model_output_message: ChatMessage | None            # LLM 原始响应
    model_output: str | list[dict] | None               # LLM 输出文本
    code_action: str | None                             # 从 model_output 提取的代码
    tool_calls: list[ToolCall] | None                   # 工具调用（CodeAct 下固定为 python_interpreter）
    observations: str | None                            # 代码执行结果
    error: AgentError | None                            # 执行错误
    token_usage: TokenUsage | None                      # token 用量
    is_final_answer: bool = False                       # 是否为最终答案
    ...
```

### ToolCall（`memory.py:25-28`）

```python
@dataclass
class ToolCall:
    name: str          # 工具名
    arguments: Any     # 参数
    id: str            # 调用 ID（nexent 中为 "call_<step_count>"，递增，跨 run 不唯一）
```

## 2. 层级关系

```
session（会话，= 监控层 conversation_id）
  └─ turn（逻辑轮次，1 次用户输入）
       └─ run（物理执行，1 次 agent_run 调用）
            └─ N × ActionStep + 1 × FinalAnswerStep
                 └─ 1 个 ActionStep → 1~3 条 ChatMessage
```

### turn 与 run 的关系

| 场景 | turn : run | 说明 |
|------|-----------|------|
| 正常执行 | 1 : 1 | 用户说一次，agent 跑一次 |
| resume | 1 : N | 同一用户输入，多次物理执行续跑 |

### ActionStep → ChatMessage 的映射（`memory.py:92-150`）

ActionStep 的 `to_messages()` 方法根据字段存在情况产出不同数量的 ChatMessage：

| 场景 | 产出 | 触发条件 |
|------|------|---------|
| 有工具调用（常见） | 3 条 | ASSISTANT(model_output) + TOOL_CALL(tool_calls) + TOOL_RESPONSE(observations) |
| 直接回答无工具 | 1 条 | 仅 ASSISTANT(model_output)，无 tool_calls/observations |
| 执行出错 | 3 条 | ASSISTANT + TOOL_CALL + TOOL_RESPONSE(error) |

## 3. CodeAct 模型下的特殊约束

nexent 的 `CoreAgent` 继承 smolagents `CodeAgent`，采用 CodeAct 模式：

### 3.1 工具粒度是代码执行步，不是离散函数调用

每个 ReAct step 只有 **1 个** `ToolCall(name="python_interpreter")`，`arguments` 是整段代码。真正的工具调用发生在代码内部，靠 `invoked_tool_signatures` 事后抽取。

```
ActionStep:
  tool_calls = [ToolCall(name="python_interpreter", arguments="整段代码")]
  invoked_tool_signatures = ["web_search(...)", "read_file(...)"]  # 代码内实际调用的工具
```

### 3.2 FinalAnswerError 不是错误

当模型输出**没有代码块**时，`parse_code_blobs()` 解析失败，代码用 `FinalAnswerError` 异常跳出 step 流程（`core_agent.py:498-503`）：

```python
except Exception:                          # parse_code_blobs 解析失败
    raise FinalAnswerError()               # 不是错误，是"模型选择直接文本回答"
```

外层捕获后直接把 `model_output` 当最终答案（`core_agent.py:875-877`），不报错、不重试。这是 smolagents 用异常做控制流的设计——名字叫 "Error" 但语义是"结束"。

### 3.3 tool_call_id 的局部性问题

当前 `tool_call_id` 的生成方式（`core_agent.py:508,531`）：

```python
id=f"call_{len(self.memory.steps)}"    # 如 "call_3"
```

**问题**：这是递增计数，resume 后新 run 的 `memory.steps` 从零计数，又会产生 `call_0`、`call_1`，与之前 run 的 ID 冲突，无法跨 run/跨进程配对 tool_call 和 tool_result。设计文档要求改为 UUID（`tc_<uuid4hex16>`）。

## 4. 当前代码中的 ID 现状

| ID 类型 | 当前是否存在 | 当前来源 | 设计文档要求 |
|---------|------------|---------|------------|
| session_id | 仅监控层有 | `conversation_id` | 统一到事件层 |
| run_id | 无 | — | 新增，UUID |
| turn_id | 无 | — | 新增，UUID |
| event_id (uuid) | 无 | — | 新增，UUID4 |
| parent_event_id | 无 | — | 新增，链/树 |
| llm_message_id | 无 | — | 新增，取自 LLM 响应 id |
| tool_call_id | 有 | `call_<step_count>` | 改为 UUID |
| step_number | 有 | ActionStep 内递增 | 保留为 step_index |
| agent_id | 仅监控层有 | — | 统一到事件层 |

## 5. 当前 resume 机制

`add_history_to_agent`（`nexent_agent.py:465-492`）：

```python
self.agent.memory.reset()                    # 彻底清空
for msg in history:                           # 线性重放
    if msg.role == 'user':
        self.agent.memory.steps.append(TaskStep(task=msg.content))
    elif msg.role == 'assistant':
        self.agent.memory.steps.append(ActionStep(action_output=msg.content, model_output=msg.content))
self.agent._history_step_count = len(steps)   # 标记历史边界
```

**现状**：只保留 user/assistant 文本，丢弃 tool_calls、observations、token_usage、压缩、offload——即设计文档中的 Level 0 有损桩。`memory.steps` 是扁平列表，无 parent 指针、无分支能力。
