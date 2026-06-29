# Nexent 对话持久化实施计划

> **范围：P0 + P1 + P2**（对应 design_overall.md §8 的前三阶段）
> **不在本计划内的：** P3（fork/子 agent 传播/导出器）、P4（PG 实现 + 迁移脚本）
> **设计文档：** `persist_design/design_overall.md` v0.2
> **知识基线：** `persist_design/knowledge_summary.md`

---

## 1. 设计决策

### 1.1 事件信封 + 判别联合 payload

| 项 | 内容 |
|---|---|
| **决策** | Event 信封持有公共字段（id、时间戳、分组 ID 等），payload 用 Pydantic `Annotated[Union[..., ...], Field(discriminator="kind")]` 判别联合；`type` 在信封冗余但用 `model_validator` 锁死与 `payload.kind` 一致 |
| **备选 A** | Event 本身做成子类判别联合（每类事件一个 Event 子类） |
| **备选 B** | payload 纯 dict，不建模 |
| **为何选此** | 备选 A 打散信封公共字段，使 `EventStore.append(event)` 签名复杂化（需 `Union[UserInputEvent, ToolCallEvent, ...]`）；备选 B 无类型安全，压缩/offload 恢复时字段拼写错误编译不可检。当前方案在"信封统一 + payload 类型安全"间取平衡——信封字段不变，payload 各子类有 `kind` 判别，落盘冗余仅 `kind` 一个字符串 |

### 1.2 JSONL 单文件按 session，不按 agent 拆

| 项 | 内容 |
|---|---|
| **决策** | 一个 session 一个 `events.jsonl`，所有 agent 事件按 `seq` 全序混排 |
| **备选** | `main.jsonl` + `agents/<id>.jsonl`（按 agent 拆文件） |
| **为何选此** | handoff 跨 agent，拆文件破坏全局时序与 `parent_event_id` 树。按 agent 查看 = 用 `agent_id` 过滤，不需要拆文件。design_overall.md §4.2 / §10 已论证 |

### 1.3 `seq` 在单写者锁内分配

| 项 | 内容 |
|---|---|
| **决策** | `append()` 内持 `threading.Lock`（进程内）+ `fcntl`/`msvcrt` 文件锁（跨进程），紧挨写入分配 seq |
| **备选 A** | "先算 seq 后写"（无锁） |
| **备选 B** | 每 agent 独立序列 |
| **为何选此** | 备选 A 有竞态；备选 B 破坏全局有序，resume 的 `read_branch` 无法确定跨 agent 时序。当前方案在 JSONL 阶段够用，迁 PG 后用 DB 序列消失 |

### 1.4 写入失败不阻断 agent 循环，但标记 `Run.log_complete=false`

| 项 | 内容 |
|---|---|
| **决策** | `EventStore.append` 失败时 try/except 吞异常 + 结构化告警，不向 agent 循环抛；置 `Run.log_complete=false` |
| **备选** | 写入失败直接抛，让上层感知 |
| **为何选此** | 持久化是副车道，不应拖垮对话主流程。但失败必须对 resume 可见——`log_complete=false` + 结构性 gap 检测（`parent_event_id` 指向缺失事件 / `seq` 空洞）双保险 |

### 1.5 Level 0 与 Level 1 resume 共存，入口由 `resume_fidelity` 选择

| 项 | 内容 |
|---|---|
| **决策** | `MemoryReconstructor` 接受 `fidelity: Literal["lossy", "faithful"]`；Level 0 与现状 `add_history_to_agent` 等价，Level 1 全量忠实重建 |
| **备选** | 只实现 Level 1 |
| **为何选此** | Level 0 几乎不依赖 smolagents step 内部细节，是升级时的安全兜底。Level 1 与 step 形状强耦合，smolagents 升级可能打断。两者共存让用户/调用方按场景选择保真度 |

### 1.6 tool_call_id 改 UUID

| 项 | 内容 |
|---|---|
| **决策** | `tool_call_id` 从 `call_<step_count>` 改为 `tc_<uuid4hex16>` |
| **备选** | 保持 `call_<step_count>` |
| **为何选此** | 局部递增跨 run 不唯一，resume 后新 run 又从 `call_0` 开始，无法跨 run/跨进程配对 tool_call 和 tool_result。UUID 全局唯一 |

### 1.7 新增模块放在 `agent_event/` 子包，不改动现有文件布局

| 项 | 内容 |
|---|---|
| **决策** | 新增 `sdk/nexent/core/agents/agent_event/` 子包，含 models.py、store.py、jsonl_store.py、reconstructor.py；对现有文件的改动仅限"加 hook 调用"和"加 ID 字段" |
| **备选** | 把 Event 模型塞进 `agent_model.py` |
| **为何选此** | `agent_model.py` 已有 200+ 行且职责是配置/运行信息模型。Event 体系是独立的数据模型域（10 个 payload 子类、判别联合、序列化/反序列化），与配置模型正交。独立子包便于测试、迁 PG 时替换 store 实现、smolagents 升级时隔离适配器 |

---

## 2. 变更清单

> 按依赖序排列：A 在 B 之前 ⇔ B 依赖 A

### 变更 C1：新增 `agent_event/models.py` — Event / Payload / Session / Run 模型

| 项 | 内容 |
|---|---|
| **文件** | `sdk/nexent/core/agents/agent_event/models.py`（新建） |
| **函数/类** | `EventType`、10 个 `*Payload(BaseModel)`、`PayloadUnion`、`Event(BaseModel)`、`Session(BaseModel)`、`Run(BaseModel)` |
| **变更** | 无（全新模块） |
| **风险** | **Low** — 纯数据模型，无运行时副作用 |

具体内容对照 design_overall.md §3.2 / §3.3 / §3.4：
- `SCHEMA_VERSION = 1`
- `EventType` 枚举：user_input / system_prompt / assistant_reasoning / assistant_message / tool_call / tool_result / compaction_summary / offload_record / error / run_lifecycle
- 10 个 payload 子类（逐一列出，对照 §3.4）：
  - `UserInputPayload(kind="user_input", text, attachments?)`
  - `SystemPromptPayload(kind="system_prompt", rendered_prompt, tool_defs_hash, injected_components)`
  - `AssistantReasoningPayload(kind="assistant_reasoning", text)`
  - `AssistantMessagePayload(kind="assistant_message", text, is_final_answer: bool)`
  - `ToolCallPayload(kind="tool_call", tool_call_id, tool_name, arguments, source="local"|"mcp", mcp_server?)`
  - `ToolResultPayload(kind="tool_result", tool_call_id, output_raw, output_chars, is_error: bool, duration_ms, offload_handle?)`
  - `CompactionSummaryPayload(kind="compaction_summary", structured_summary, covers_event_ids, covered_pairs, end_steps, anchor_fingerprint, call_type, previous_summary_event_id?, summarizer_model_id, input_tokens, output_tokens, input_chars, output_chars, is_active: bool)`
  - `OffloadRecordPayload(kind="offload_record", handle, description, original_chars, preview, content_ref, source_event_id)`
  - `ErrorPayload(kind="error", error_type, message, traceback, step_index?)`
  - `RunLifecyclePayload(kind="run_lifecycle", action, stop_reason?, render_config?)`
- `PayloadUnion = Annotated[Union[..., ...], Field(discriminator="kind")]`
- `Event` 信封完整字段（对照 §3.2）：
  - `schema_version: int = SCHEMA_VERSION` (alias `schemaVersion`)
  - `event_id: UUID` (alias `uuid`)
  - `parent_event_id: UUID | None` (alias `parentUuid`)
  - `session_id: UUID` (alias `sessionId`)
  - `run_id: UUID | None` (alias `runId`)
  - `turn_id: UUID | None` (alias `turnId`)
  - `agent_id: str` (alias `agentId`)
  - `llm_message_id: str | None` (alias `llmMessageId`)
  - `seq: int` (alias `seq`)
  - `type: EventType`
  - `role: str`（user | assistant | tool | system）
  - `is_sidechain: bool = False` (alias `isSidechain`)
  - `sidechain_parent_id: UUID | None` (alias `sidechainParentId`)
  - `step_index: int | None` (alias `stepIndex`)
  - `created_at: datetime` (alias `timestamp`)
  - `model_id: str | None` (alias `model`)
  - `usage: dict | None`
  - `latency_ms: int | None` (alias `latencyMs`)
  - `stop_reason: str | None` (alias `stopReason`)
  - `superseded_by: UUID | None` (alias `supersededBy`)
  - `visibility: str = "normal"`（normal | internal | compressed_out）
  - `payload: PayloadUnion`
  - `metadata: dict = {}`
- `_consistent` model_validator：`assert type.value == payload.kind`
- `Session`：session_id / title / head_event_id / status / created_at / updated_at
- `Run`：run_id / session_id / turn_id / agent_id / query / status / stop_reason / started_at / ended_at / final_answer / model_config_snapshot / token_totals / **log_complete: bool = True**
- `ConfigDict(populate_by_name=True)`；落盘 `model_dump_json(by_alias=True, exclude_none=True)`

### 变更 C2：新增 `agent_event/store.py` — EventStore 抽象接口

| 项 | 内容 |
|---|---|
| **文件** | `sdk/nexent/core/agents/agent_event/store.py`（新建） |
| **函数/类** | `EventStore(ABC)` |
| **变更** | 无（全新模块） |
| **风险** | **Low** — 纯接口定义 |

方法对照 design_overall.md §4.1：
- `append(event: Event) -> None` — 幂等：`(run_id, step_index, type, tool_call_id)` 重复则跳过
- `next_seq(session_id: UUID) -> int`
- `read_session(session_id: UUID) -> BranchResult` — 含 `events: list[Event]` + `has_gap: bool` + `gap_event_id: UUID | None`
- `read_branch(leaf_event_id: UUID) -> BranchResult` — 同上（含结构性 gap 检测）
- `get_session(session_id: UUID) -> Session`
- `upsert_session(s: Session) -> None`
- `get_run(run_id: UUID) -> Run`
- `upsert_run(r: Run) -> None`
- `put_blob(data: str) -> str`
- `get_blob(content_ref: str) -> str`

> `BranchResult` 为轻量 dataclass：`events: list[Event], has_gap: bool = False, gap_event_id: UUID | None = None`

### 变更 C3：新增 `agent_event/jsonl_store.py` — JSONL 实现

| 项 | 内容 |
|---|---|
| **文件** | `sdk/nexent/core/agents/agent_event/jsonl_store.py`（新建） |
| **函数/类** | `JsonlEventStore(EventStore)` |
| **变更** | 无（全新模块） |
| **风险** | **Medium** — 文件 I/O、锁、seq 分配的正确性需测试 |

关键实现：
- 目录结构：`<root>/<session_id>/events.jsonl` + `meta.json` + `runs.jsonl` + `blobs/<sha256>`
- `append`：`open('a')` 写一行 + `fsync`；锁内分配 seq（`threading.Lock` + `msvcrt.locking` / `fcntl.flock`）；冷启动读末行 `seq+1`
- `append` 幂等检查（§6.3）：写入前检查 `(run_id, step_index, type, tool_call_id)` 组合是否已存在于内存索引（`_append_index: dict`），已存在则跳过写入并返回（重试/续写不产生重复事件）
- `read_branch`：全量入内存建 `event_id->Event` dict，沿 `parent_event_id` 回溯；回溯同时执行**结构性 gap 检测**（§12.1）：若 `parent_event_id` 指向缺失事件或 `seq` 出现空洞，在返回结果中标记 `has_gap=True` + `gap_event_id`（第一个断裂点），供 `MemoryReconstructor` 判断是否退化 Level 0
- `put_blob`：sha256 内容寻址，天然去重
- `append` 失败：try/except 吞异常 + `logging.error`，调用方负责置 `Run.log_complete=false`
- `read_session` 同样执行结构性 gap 检测

### 变更 C4：新增 `agent_event/__init__.py` — 子包入口

| 项 | 内容 |
|---|---|
| **文件** | `sdk/nexent/core/agents/agent_event/__init__.py`（新建） |
| **变更** | re-export `Event, EventType, EventStore, JsonlEventStore, Session, Run, PayloadUnion, BranchResult, render` |
| **风险** | **Low** |

### 变更 C5：新增 `agent_event/render.py` — render() 纯函数（§5 Layer 1→2 桥接）

| 项 | 内容 |
|---|---|
| **文件** | `sdk/nexent/core/agents/agent_event/render.py`（新建） |
| **函数/类** | `render(events, render_config=None) -> list[Message]`、`pick_active_compaction(events) -> CompactionSummaryPayload | None` |
| **变更** | 无（全新模块） |
| **风险** | **Low** — 纯函数，无副作用 |

对照 design_overall.md §5：
- `pick_active_compaction(events)`：从事件列表中找 `is_active=True` 的 `compaction_summary`，校验其 `anchor_fingerprint` 与实际 `covers_event_ids` 指纹一致
- `render(events, render_config)` 主流程：
  1. 调 `pick_active_compaction` 得 active summary
  2. 遍历 events：被 `covers_event_ids` 覆盖的跳过；`tool_result` 有 `offload_handle` 则替换为 `[[OFFLOAD:handle:desc]]` 标记；其余转 `to_message(ev)`
  3. 在合适位置插入 summary_message(active)
  4. 按 `llm_message_id` 归组为统一 `role + content[]` 格式消息
- `render_config` 参数：可选的渲染参数快照（来自 `run_lifecycle(started).render_config`），传入时用它做截断/offload，不传时使用默认值
- Level 1 `MemoryReconstructor` 调用此函数 re-render observations

### 变更 C6：`agent_model.py` — AgentRunInfo 增加持久化字段

| 项 | 内容 |
|---|---|
| **文件** | `sdk/nexent/core/agents/agent_model.py` |
| **函数/类** | `AgentRunInfo` |
| **变更** | 新增字段：`event_store: Optional[Any] = None`、`session_id: Optional[str] = None`、`resume_from: Optional[str] = None`（leaf_event_id）、`resume_fidelity: Optional[str] = None`（"lossy"/"faithful"） |
| **风险** | **Low** — 可选字段，默认 None，不传时行为不变 |

### 变更 C7：`core_agent.py` — CoreAgent 持有 event_store 引用 + tool_call_id 改 UUID

| 项 | 内容 |
|---|---|
| **文件** | `sdk/nexent/core/agents/core_agent.py` |
| **函数/类** | `CoreAgent.__init__`、`_step_stream` |
| **变更** | ① `__init__` 新增 `self._event_store: Optional[EventStore] = None`、`self._session_id: Optional[UUID] = None`、`self._run_id: Optional[UUID] = None`、`self._turn_id: Optional[UUID] = None`、`self._parent_event_cursor: Optional[UUID] = None`、`self._llm_message_id: Optional[str] = None`<br>② `_step_stream` 中 `ToolCall.id` 从 `f"call_{len(self.memory.steps)}"` 改为 `f"tc_{uuid4().hex[:16]}"` |
| **风险** | **Medium** — tool_call_id 变更可能影响依赖 `call_N` 格式的下游代码（如 `step_renderer.py` 中 `tool_use_id` 引用、`write_memory_to_messages` 中的 TOOL_RESPONSE 匹配） |

**缓解**：搜索所有 `call_` 硬编码引用，确保 smolagents `AgentMemory.to_messages()` 和 `write_memory_to_messages` 用 `tool_call.id` 做配对而非假设格式。当前 smolagents `ActionStep.to_messages()` 用 `tool_calls[0].id` 作为 `ToolResponseMessage` 的 `tool_call_id`，不假设格式——安全。

### 变更 C8：`core_agent.py` — _step_stream 写入 assistant_message / tool_call / tool_result 事件

| 项 | 内容 |
|---|---|
| **文件** | `sdk/nexent/core/agents/core_agent.py` |
| **函数/类** | `CoreAgent._step_stream` |
| **变更** | 在关键时机调用 `self._emit_event(...)` 写事件（见下表） |
| **风险** | **High** — 直接改核心循环，写入时机错误或异常泄漏会拖垮对话主流程 |

**写入时机与位置：**

| 事件 | 写入位置 | 关键约束 |
|------|---------|---------|
| `assistant_reasoning` | `_step_stream:461`（model_output 赋值后） | visibility="internal"；如有 thinking/reasoning 内容 |
| `assistant_message` | `_step_stream:461`（model_output 赋值后） | `is_final_answer=False`（此处不是最终答案） |
| `tool_call` | `_step_stream:528`（ToolCall 创建后） | `tool_call_id` = 新 UUID；`metadata.invoked_tools` = `invoked_tool_signatures` |
| `tool_result` | `_step_stream:~603`（observation 组装后、**截断前**） | `output_raw` = 全量 observation（截断前的值）；`duration_ms` = exec_duration_ms |
| `error` | `_step_stream:466`（AgentGenerationError）和 `:591`（AgentExecutionError） | — |

**缓解措施：**
- `_emit_event` 内 try/except，失败仅 log，不向 agent 循环抛
- 所有写入在"数据已定稿"的时机执行，不改变 step 的数据流
- 添加 `if self._event_store is None: return` 前置守卫

### 变更 C9：`core_agent.py` — run() 入口写入 user_input / system_prompt / run_lifecycle(started)

| 项 | 内容 |
|---|---|
| **文件** | `sdk/nexent/core/agents/core_agent.py` |
| **函数/类** | `CoreAgent.run` |
| **变更** | 在 `memory.steps.append(TaskStep)` 后写入 `user_input` 事件；在 `SystemPromptStep` 设置后写入 `system_prompt` 事件；run 开始时写入 `run_lifecycle(started)` 含 `render_config` 快照 |
| **风险** | **Medium** — 改动 run 入口，但逻辑是追加式的，不改变原有流程 |

### 变更 C10：`nexent_agent.py` — ID 穿线 + event_store 注入

| 项 | 内容 |
|---|---|
| **文件** | `sdk/nexent/core/agents/nexent_agent.py` |
| **函数/类** | `NexentAgent.__init__`、`create_single_agent`、`add_history_to_agent`、`agent_run_with_observer` |
| **变更** | ① `__init__` 新增 `self._event_store: Optional[EventStore] = None`<br>② `create_single_agent` 把 `event_store`/`session_id` 注入 CoreAgent<br>③ `add_history_to_agent` 调用 `MemoryReconstructor.reconstruct(fidelity="lossy")` 替代原逻辑（原逻辑内联到 Level 0 重建器）<br>④ `agent_run_with_observer` 入口生成 `run_id`/`turn_id`，设 `CoreAgent._run_id`/`_turn_id`，写入 `run_lifecycle(started)`；出口写 `run_lifecycle(ended)` |
| **风险** | **High** — 改动 NexentAgent 的主运行路径，涉及 run 生命周期管理 |

**缓解**：
- `add_history_to_agent` 改为委托 `MemoryReconstructor`，Level 0 行为与原逻辑严格等价（用现有测试验证）
- `event_store is None` 时跳过所有持久化逻辑，不改变无持久化场景的行为

### 变更 C11：`run_agent.py` — agent_run_thread 传递 event_store / resume 参数

| 项 | 内容 |
|---|---|
| **文件** | `sdk/nexent/core/agents/run_agent.py` |
| **函数/类** | `agent_run_thread` |
| **变更** | 从 `AgentRunInfo` 取 `event_store`/`session_id`/`resume_from`/`resume_fidelity`，传给 NexentAgent；resume 时调用 `MemoryReconstructor.reconstruct` 替代 `add_history_to_agent` |
| **风险** | **Medium** — 改入口参数传递，但全部可选、默认 None |

### 变更 C12：新增 `agent_event/reconstructor.py` — MemoryReconstructor

| 项 | 内容 |
|---|---|
| **文件** | `sdk/nexent/core/agents/agent_event/reconstructor.py`（新建） |
| **函数/类** | `MemoryReconstructor` |
| **变更** | 无（全新模块） |
| **风险** | **High** — Level 1 重建器与 smolagents `ActionStep` 内部形状强耦合 |

**Level 0 实现**（P1）：
- `user_input` → `TaskStep(task=text)`
- 每轮最终 `assistant_message` → `ActionStep(action_output=text, model_output=text)`
- 设 `_history_step_count = len(steps)`
- 与原 `add_history_to_agent` 行为严格等价

**Level 1 实现**（P2）：
- 按 `step_index` / `llm_message_id` 归组事件
- 逐步重建完整 `ActionStep`：`model_output` / `model_output_message` / `code_action` / `tool_calls` / `observations` / `token_usage` / `error` / `invoked_tool_signatures`
- observations 用"按当时所见"渲染：调用 `render.py` 的 `render()` 函数，渲染参数取自 `run_lifecycle(started).render_config` 快照；**逐 step 退化**：若某 step 的 render 快照缺失或与当前配置不一致且无法复现，该 step 的 observation 退化 Level 0 语义（只保留可读文本），不影响其他 step
- **fingerprint 校验 + eager 最深有效摘要选择**（§6.1 step 3 / §6.2）：
  - `pick_valid_summary(events, branch_event_ids)` 辅助函数：从摘要链尾向旧遍历，选**第一个 `covers_event_ids ⊆ 当前分支前缀事件集`（fingerprint 命中）的有效摘要**
  - 校验通过：写入 `PreviousSummaryCache`（`summary_text` / `covered_pairs` / `anchor_fingerprint`）
  - 校验失败的尾部摘要：丢弃，对应事件到**下次 `compress_if_needed` 惰性重压**（安全窗口：compress 在模型调用前执行，不会把超预算上下文喂给模型）
  - **不变量：绝不把未通过 fingerprint 校验的摘要载入缓存或喂给模型**
- 恢复 `SummaryTaskStep` + `PreviousSummaryCache`（从 `pick_valid_summary` 选出的有效摘要）
- 恢复 `OffloadStore`（从 `offload_record` 重灌 handle→preview），blob 懒加载
- **结构性 gap 检测**：检查 `BranchResult.has_gap`；若 `has_gap=True`，从 `gap_event_id` 处往后退化到 Level 0（日志断裂点之后无法忠实重建）
- 设 `_history_step_count`

**缓解**：
- Level 0 与 smolagents 几乎无耦合，是安全兜底
- Level 1 的 `Event → ActionStep` 适配器集中在此单一模块，smolagents 升级只需改此文件
- 为 ActionStep 字段映射加版本快照测试

### 变更 C13：`agent_context/manager.py` — compress_if_needed 写入 compaction_summary 事件

| 项 | 内容 |
|---|---|
| **文件** | `sdk/nexent/core/agents/agent_context/manager.py` |
| **函数/类** | `ContextManager.compress_if_needed`、`ContextManager.__init__` |
| **变更** | ① `__init__` 新增 `self._event_store: Optional[EventStore] = None`<br>② `compress_if_needed` 在压缩完成后（LLM 调用返回、`CurrentSummaryCache`/`PreviousSummaryCache` 更新后）写入 `compaction_summary` 事件，**完整字段**（对照 §3.5）：`structured_summary`（五段 summary）、`covers_event_ids`（被覆盖事件 UUID 列表）、`covered_pairs`（= PreviousSummaryCache.covered_pairs）、`end_steps`（= CurrentSummaryCache.end_steps）、`anchor_fingerprint`（= hash(covers_event_ids)）、`call_type`（full/incremental）、`previous_summary_event_id`（增量链上一份摘要 UUID）、`summarizer_model_id`、`input_tokens`/`output_tokens`/`input_chars`/`output_chars`（= CompressionCallRecord）、`is_active`（当前渲染是否采用）<br>③ `_event_store` 的注入由 C10 `create_single_agent` 在创建 agent 后执行：`agent.context_manager._event_store = event_store` 和 `agent.context_manager._offload_store._event_store = event_store` |
| **风险** | **High** — 压缩路径是 agent 上下文管理核心，写入逻辑必须不干扰压缩本身 |

**缓解**：
- 事件写入在压缩计算完成之后、返回结果之前，不干预压缩逻辑
- `_event_store is None` 时跳过
- 写入失败仅 log，不抛

### 变更 C14：`agent_context/offload_store.py` — OffloadStore 写入 offload_record 事件

| 项 | 内容 |
|---|---|
| **文件** | `sdk/nexent/core/agents/agent_context/offload_store.py` |
| **函数/类** | `OffloadStore.store` |
| **变更** | ① `__init__` 新增 `self._event_store: Optional[EventStore] = None`<br>② `store()` 在内容入库后写入 `offload_record` 事件，**完整字段**（对照 §3.6）：`handle`（UUID handle）、`description`（人类可读提示）、`original_chars`（原始长度）、`preview`（head+tail 摘要）、`content_ref`（blob 引用，如 `blob://<sha256>`）、`source_event_id`（来自哪条 tool_result）<br>③ 大 blob（超阈值）走 `EventStore.put_blob`，event 中放 `content_ref`；小内容直接存入 `preview` 不另存 blob |
| **风险** | **Medium** — offload 是可选功能，且已有 `enable_reload` 守卫 |

### 变更 C15：`nexent_agent.py` — agent_run_with_observer 出口写 run_lifecycle(ended) + final_answer

| 项 | 内容 |
|---|---|
| **文件** | `sdk/nexent/core/agents/nexent_agent.py` |
| **函数/类** | `NexentAgent.agent_run_with_observer` |
| **变更** | ① 正常结束时写 `run_lifecycle(ended, stop_reason=...)` + 最终 `assistant_message(is_final_answer=True)`<br>② `stop_event` 中断时写 `run_lifecycle(interrupted)`；若有进行中的 step，尽力 flush 已有数据为带 `metadata.partial=true` 的事件（如已拿到 model_output 但未执行完，写 partial 的 assistant_message / tool_call）——§12.3<br>③ 异常时写 `run_lifecycle(ended, stop_reason="error")` + `error` 事件<br>④ 消费循环中若 `step_log.error` 非空（非 AgentExecutionError 路径），写 `error` 事件——§11.2 |
| **风险** | **Medium** — 在 finally 块中追加，不影响主流程 |

### 变更 C16：`agents/__init__.py` — 导出 agent_event 子包

| 项 | 内容 |
|---|---|
| **文件** | `sdk/nexent/core/agents/__init__.py` |
| **变更** | 新增 `from .agent_event import Event, EventType, EventStore, JsonlEventStore, ...` |
| **风险** | **Low** |

---

## 3. 数据流

### 3.1 Before（现状）

```
用户输入
  → AgentRunInfo(query, history, ...)
    → agent_run_thread
      → NexentAgent.add_history_to_agent(history)     # 仅 TaskStep/ActionStep 文本
      → agent_run_with_observer(query)
        → CoreAgent.run(query)
          → _step_stream (ReAct 循环)
            → model → ToolCall → execute → observation
            → 所有数据仅进 memory.steps (内存)
            → observer.add_message (推前端，不持久化)
        → step_metrics → nexent_context_metrics.log (仅指标)
        → 最终答案 → observer → 前端

进程退出 → memory.steps / PreviousSummaryCache / OffloadStore 全部丢失
```

### 3.2 After（P0+P1+P2 完成后）

```
用户输入
  → AgentRunInfo(query, history, event_store?, session_id?, resume_from?, resume_fidelity?)
    → agent_run_thread
      ├─ [resume?]
      │   event_store.read_branch(leaf) → events
      │   MemoryReconstructor.reconstruct(agent, events, fidelity)
      │     Level 0: TaskStep + ActionStep 文本 (等价旧 add_history_to_agent)
      │     Level 1: 完整 ActionStep + SummaryTaskStep + OffloadStore + PreviousSummaryCache
      │
      └─ [正常启动]
          NexentAgent.add_history_to_agent → MemoryReconstructor(fidelity="lossy")

      → agent_run_with_observer(query)
        → 生成 run_id / turn_id，设 CoreAgent._run_id / _turn_id
        → 写 run_lifecycle(started) + render_config 快照
        → CoreAgent.run(query)
          → run() 入口写 user_input + system_prompt 事件
          → _step_stream (ReAct 循环)
            → model
              → 写 assistant_message + assistant_reasoning 事件 (usage/llm_message_id)
            → ToolCall 创建 (id = tc_<uuid4hex16>)
              → 写 tool_call 事件 (metadata.invoked_tools)
            → execute → observation (全量)
              → 写 tool_result 事件 (output_raw = 截断前全量)
              → [截断/offload 在写入事件之后]
            → compress_if_needed
              → 压缩完成 → 写 compaction_summary 事件
              → offload → 写 offload_record 事件 + blob
          → 所有事件 → EventStore.append → events.jsonl
        → 正常结束 → 写 run_lifecycle(ended) + final_answer
        → 中断/异常 → 写 run_lifecycle(interrupted/ended) + error
        → 更新 Session.head_event_id / Run 状态
```

**关键变化：**
1. **新路径**：`CoreAgent._step_stream` → `_emit_event()` → `EventStore.append()` → `events.jsonl`
2. **新路径**：`ContextManager.compress_if_needed` → 写 `compaction_summary` 事件
3. **新路径**：`OffloadStore.store` → 写 `offload_record` 事件 + blob
4. **新路径**：resume → `EventStore.read_branch` → `MemoryReconstructor.reconstruct` → `CoreAgent.memory`
5. **不变的路径**：`_step_stream` 内部数据流（model → parse → execute → observe）完全不变，事件写入是追加式的旁路

---

## 4. 不变项

以下模块/函数/行为**必须保持不变**：

| 模块 | 不变内容 | 原因 |
|------|---------|------|
| `core_agent.py: _step_stream` 内部数据流 | model→parse→execute→observe→truncate 的顺序和逻辑 | 对话主流程正确性 |
| `core_agent.py: _step_stream:644-660` | 截断逻辑（head+tail+marker） | 截断发生在事件写入之后，不影响事件内容 |
| `agent_context/manager.py: compress_if_needed` 压缩算法 | token 预算计算、previous/current 压缩选择、cache 更新 | 压缩正确性 |
| `agent_context/step_renderer.py` | 渲染逻辑 | 事件写入不改变渲染行为 |
| `agent_context/budget.py` | fingerprint/extract_pairs/trim 函数 | 压缩基础逻辑 |
| `agent_context/summary_step.py` | SummaryTaskStep.to_messages() | 渲染格式 |
| `agent_context/current_compression.py` | CurrentCompressor | 压缩算法 |
| `agent_context/previous_compression.py` | PreviousCompressor | 压缩算法 |
| `agent_context/llm_summary.py` | LLMSummary | LLM 调用逻辑 |
| `agent_context/stats_export.py` | 统计导出 | 纯函数 |
| `agent_model.py` 现有字段 | AgentConfig / ModelConfig / ToolConfig 等 | 向后兼容 |
| `run_agent.py: agent_run` | async generator 框架 | 前端交互层 |
| `utils/observer.py` | MessageObserver | 前端推送通道 |
| `verification.py` | VerificationController | 验证逻辑 |
| `summary_cache.py` | PreviousSummaryCache / CurrentSummaryCache / CompressionCallRecord 数据类 | 压缩缓存数据结构 |
| `summary_config.py` | ContextManagerConfig | 配置模型 |
| `smolagents/` 包 | 任何 smolagents 内部代码 | 第三方依赖，不可修改 |

**核心原则：事件写入是旁路（side channel），不改变任何既有数据流。**

---

## 5. 失败处理

### 5.1 C8: _step_stream 事件写入失败

| 项 | 内容 |
|---|---|
| **失败场景** | `EventStore.append` 抛异常（磁盘满/权限/IO 错误） |
| **影响** | 该条事件丢失，events.jsonl 出现空洞 |
| **处理** | `_emit_event` 内 try/except，失败时 `logging.error` + `logging.warning`，不向 `_step_stream` 抛异常 |
| **后续** | 调用方（`nexent_agent.py`）在 run 结束时检查 `append` 是否有失败，有则置 `Run.log_complete=false` |
| **回滚** | 事件写入失败不影响 agent 循环继续运行；run 结束后 `Run.log_complete=false` 标记可让下次 resume 知道该 run 日志不完整 |

### 5.2 C10: NexentAgent 主路径改动

| 项 | 内容 |
|---|---|
| **失败场景** | ID 穿线/生命周期事件写入引入 bug，导致 run 无法正常完成 |
| **影响** | 对话功能受损 |
| **处理** | 所有新增逻辑加 `if self._event_store is None: return` 前置守卫——**只要 event_store 未注入，行为与改动前完全一致** |
| **回滚** | 在 `AgentRunInfo` 中不传 `event_store` 即可回退到无持久化行为；或删除 `agent_event/` 子包 + revert 三个文件的 hook 调用 |

### 5.3 C12: MemoryReconstructor Level 1 重建失败

| 项 | 内容 |
|---|---|
| **失败场景** | smolagents 升级后 ActionStep 字段变化，Level 1 重建抛异常或产出错误 memory |
| **影响** | resume 后对话行为异常 |
| **处理** | `reconstruct()` 内部 try/except，Level 1 失败时自动退化到 Level 0（`logging.warning("Level 1 reconstruction failed, falling back to Level 0")`） |
| **回滚** | 调用方可指定 `resume_fidelity="lossy"` 强制走 Level 0 |

### 5.4 C13: compress_if_needed 事件写入干扰压缩

| 项 | 内容 |
|---|---|
| **失败场景** | compaction_summary 事件写入抛异常，异常泄漏到 compress_if_needed |
| **影响** | 压缩中断，可能影响上下文管理 |
| **处理** | 事件写入在压缩计算完成之后；`_emit_event` 内 try/except 不抛；最坏情况是压缩完成但事件未落盘，下次 resume 走全量重压 |
| **回滚** | 与 C7 相同——`_event_store is None` 时跳过 |

### 5.5 C7: tool_call_id 改 UUID 后下游不兼容

| 项 | 内容 |
|---|---|
| **失败场景** | 某处代码假设 `tool_call_id` 格式为 `call_N` |
| **影响** | tool_call 与 tool_result 配对失败 |
| **处理** | 搜索所有 `call_` 硬编码引用，确认 smolagents `ActionStep.to_messages()` 和 `write_memory_to_messages` 用 `tool_call.id` 做匹配而非正则匹配；当前 smolagents 用等值比较，安全 |
| **回滚** | 如发现不兼容，可临时保留 `call_N` 格式，在事件中额外存 `uuid_tool_call_id` 字段，P1 验证无问题后再切 |

### 5.6 C3: 结构性 gap 检测在 resume 中的影响

| 项 | 内容 |
|---|---|
| **失败场景** | `read_branch` 返回 `has_gap=True`，gap 之后的日志不可靠 |
| **影响** | Level 1 resume 重建出的 memory 在 gap 点之后可能不完整 |
| **处理** | `MemoryReconstructor` 检查 `BranchResult.has_gap`；若为 True，从 `gap_event_id` 处往后退化到 Level 0（gap 点之前的部分仍可 Level 1 重建） |
| **回滚** | 全局退化到 Level 0：`resume_fidelity="lossy"` |

---

## 6. 验证步骤

### 6.1 AC: 往返一致（design_overall.md §11.7-1）

**验证**：跑一段会话 → 落库 → Level 1 重建 → `write_memory_to_messages()` 与原 run 逐条消息一致

```bash
# 步骤 1：运行测试脚本
cd $PROJECT_ROOT
$PYTHON_EXE -m pytest sdk/nexent/core/agents/agent_event/tests/test_roundtrip.py -v

# 步骤 2：手动端到端验证
# 1. 启动 agent，执行 3 轮对话（含工具调用）
# 2. 检查 events.jsonl 是否写入
# 3. 用 MemoryReconstructor Level 1 重建
# 4. 对比 write_memory_to_messages() 输出
```

### 6.2 AC: 续跑等价（§11.7-2）

**验证**：A（不中断跑到底）vs B（中途落库→新进程 Level 1 resume→续跑），最终 memory / final_answer 等价

```bash
$PYTHON_EXE -m pytest sdk/nexent/core/agents/agent_event/tests/test_resume_equivalence.py -v
```

测试设计：
- 同一 query + 同一 mock model（固定输出）
- A: 单次跑 5 步
- B: 跑 3 步 → 落库 → 新 CoreAgent + Level 1 resume → 续跑 2 步
- 对比最终 `memory.steps` 与 `final_answer`

### 6.3 AC: 压缩连续（§11.7-3）

**验证**：resume 后首次 `compress_if_needed` 命中增量路径（`anchor_fingerprint` 校验通过）

```bash
$PYTHON_EXE -m pytest sdk/nexent/core/agents/agent_event/tests/test_compression_continuity.py -v
```

测试设计：
- 跑一段长对话触发压缩 → 落库 → Level 1 resume → 触发 `compress_if_needed`
- 检查 `PreviousSummaryCache.anchor_fingerprint` 与 events 中 `compaction_summary` 的 fingerprint 一致
- 检查 compression_calls_log 中最新记录的 `call_type` = "incremental"（非 "full"）

### 6.4 AC: offload 可用（§11.7-4）

**验证**：resume 后 `reload_original_context_messages` 能按 handle 取回 blob

```bash
$PYTHON_EXE -m pytest sdk/nexent/core/agents/agent_event/tests/test_offload_resume.py -v
```

测试设计：
- 跑一段对话触发 offload → 落库 → Level 1 resume → 调用 `OffloadStore.reload(handle)`
- 验证返回内容与原 offload 内容一致

### 6.5 AC: Level 0 退化（§11.7-5）

**验证**：去掉重型事件仍能 resume，且与旧 `add_history_to_agent` 行为一致

```bash
$PYTHON_EXE -m pytest sdk/nexent/core/agents/agent_event/tests/test_level0_equivalence.py -v
```

测试设计：
- 用同一 history 分别走旧 `add_history_to_agent` 和 `MemoryReconstructor(fidelity="lossy")`
- 对比 `memory.steps` 数量、每个 step 的 `task`/`action_output`/`model_output` 字段

### 6.6 AC: 事件模型序列化往返

**验证**：Event 模型 `model_dump_json(by_alias=True) → parse_raw → 再 dump → 一致`

```bash
$PYTHON_EXE -m pytest sdk/nexent/core/agents/agent_event/tests/test_models.py -v
```

### 6.7 AC: JsonlEventStore 基本读写 + gap 检测

**验证**：append → read_session → read_branch → 数据一致、seq 单调；手动删除中间行后 read_branch 返回 has_gap=True

```bash
$PYTHON_EXE -m pytest sdk/nexent/core/agents/agent_event/tests/test_jsonl_store.py -v
```

### 6.8 AC: 写入失败不阻断

**验证**：mock `EventStore.append` 抛异常 → agent 循环正常完成 → `Run.log_complete=False`

```bash
$PYTHON_EXE -m pytest sdk/nexent/core/agents/agent_event/tests/test_failure_handling.py -v
```

### 6.9 AC: 无 event_store 时行为不变

**验证**：`AgentRunInfo.event_store=None` → 所有代码路径与改动前一致

```bash
$PYTHON_EXE -m pytest sdk/nexent/core/agents/agent_event/tests/test_no_store_passthrough.py -v
```

### 6.10 AC: fingerprint 校验 + gap 检测在 resume 中的行为

**验证**：
- fingerprint 校验：篡改 compaction_summary 的 covers_event_ids 后，resume 不再复用该摘要（走全量重压）
- gap 检测：手动删除 events.jsonl 中间行后，Level 1 resume 在 gap 点退化到 Level 0

```bash
$PYTHON_EXE -m pytest sdk/nexent/core/agents/agent_event/tests/test_fingerprint_and_gap.py -v
```

---

## 7. 阶段交付里程碑

### Milestone P0（纯新增，零耦合）

| 变更 | 交付物 |
|------|--------|
| C1 | `agent_event/models.py` — 全部 Event/Payload/Session/Run 模型（含完整字段列表） |
| C2 | `agent_event/store.py` — EventStore 抽象接口（含 BranchResult + 幂等 append） |
| C3 | `agent_event/jsonl_store.py` — JsonlEventStore 实现（含幂等检查 + gap 检测） |
| C4 | `agent_event/__init__.py` — 子包入口 |
| C5 | `agent_event/render.py` — render() 纯函数 |
| C16 | `agents/__init__.py` — 导出 |

**验证**：6.6 + 6.7 通过

### Milestone P1（写入 hook + ID 穿线 + Level 0 resume）

| 变更 | 交付物 |
|------|--------|
| C6 | `agent_model.py` — AgentRunInfo 新字段 |
| C7 | `core_agent.py` — CoreAgent 持有 store 引用 + tool_call_id UUID |
| C8 | `core_agent.py` — _step_stream 写入事件 hook |
| C9 | `core_agent.py` — run() 入口事件 |
| C10 | `nexent_agent.py` — ID 穿线 + event_store 注入 + lifecycle 事件 |
| C11 | `run_agent.py` — 传递持久化参数 |
| C12 | `reconstructor.py` — Level 0 实现 |
| C15 | `nexent_agent.py` — run 结束事件（含 partial flush + 消费循环 error） |

**验证**：6.5 + 6.8 + 6.9 通过，可端到端跑一轮对话并落盘

### Milestone P2（压缩/offload 持久化 + Level 1 resume）

| 变更 | 交付物 |
|------|--------|
| C13 | `manager.py` — compress_if_needed 写 compaction_summary（含完整字段） |
| C14 | `offload_store.py` — store() 写 offload_record + blob（含完整字段） |
| C12 | `reconstructor.py` — Level 1 实现（含 fingerprint 校验 + eager 选择 + gap 退化 + 逐 step render 退化） |

**验证**：6.1 + 6.2 + 6.3 + 6.4 通过，完整闭环

---

## 8. P3/P4 前置条件清单

> 以下信息在 P0-P2 实现后才能确定，供下次 plan 使用

| 编号 | 信息点 | 为何 P3/P4 需要它 |
|------|--------|------------------|
| F1 | `agent_event/models.py` 最终的 `Event` 字段集与 `PayloadUnion` 子类 | P3 fork 需要 `parent_event_id` 树操作；P4 PG schema 需精确列定义 |
| F2 | `JsonlEventStore.read_branch` 实际性能（冷启动读全量建 dict 的耗时） | P3 fork 可能频繁 read_branch，需评估是否要加索引 |
| F3 | Level 1 `MemoryReconstructor` 的 ActionStep 字段映射完整表 | P3 子 agent 传播需在 `create_single_agent` 递归注入 event_store + sidechain 标记 |
| F4 | `compaction_summary` 事件的 `anchor_fingerprint` 校验在 resume 中的实际命中率 | P3 fork 决策树依赖此数据决定是否惰性重压 |
| F5 | `append` 失败率与 `Run.log_complete=false` 的实际出现频率 | P4 PG 实现是否需要事务性 append（vs 现在的尽力写入） |
| F6 | `create_single_agent` 递归建子 agent 时的 ContextManager 传播路径 | P3 sidechain 事件挂载需要在此路径注入 `sidechain_parent_id` |
| F7 | smolagents 版本快照测试的 ActionStep 字段映射基线 | P3/P4 维护期间 smolagents 升级时回归测试基线 |
