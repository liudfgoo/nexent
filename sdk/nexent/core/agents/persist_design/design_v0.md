# Nexent SDK 对话持久化设计文档

> 状态：Draft v0.3 ｜ 范围：`sdk/nexent/core/agents` ｜ 目标读者：SDK 维护者
>
> v0.3 变更：新增 §11「集成设计（与现有代码的耦合）」——基于对 `core_agent.py` / `nexent_agent.py` / `run_agent.py` 的通读，给出调用流、带行号的 hook 表、ID 穿线、**可选保真度的 resume（有损桩 ↔ 全量忠实重建）**、子 agent 传播与 smolagents 耦合隔离。前十章为「数据 + 存储」设计，本章为「集成」设计，二者合并方为可照着写代码的完整文档。
>
> v0.2 变更：字段命名向《Nexent vs Claude Code 对比总结》对齐（session/turn/parentUuid 等），新增 `turn_id`、`llm_message_id`，新增 §10「与对比总结的取舍（批判性吸取）」。
>
> 命名贴合现有代码：`AgentMemory` / `TaskStep` / `ActionStep`（smolagents 基类）、`ContextManager`、`OffloadStore`、`SummaryTaskStep`、`PreviousSummaryCache` / `CurrentSummaryCache` / `CompressionCallRecord`、`MessageObserver`、`AgentRunInfo`。

---

## 1. 背景与目标

### 1.1 现状：对话链路在 SDK 层零持久化

| 数据 | 存储位置 | 是否持久化 |
|------|---------|-----------|
| 对话步骤（TaskStep / ActionStep） | `AgentMemory.steps` 内存列表 | 进程退出即丢失 |
| 输入历史（history） | 外部调用者传入 `AgentRunInfo.history` | 外部负责，SDK 不管 |
| 压缩缓存（PreviousSummaryCache） | `ContextManager` 属性 | 内存，新 run 清空 |
| 卸载内容（OffloadStore） | 内存 dict | 内存，新 run 清空 |
| 步骤指标 / 压缩调用记录 | `step_metrics` / `compression_calls_log` | 内存 |
| 上下文指标日志 | `nexent_context_metrics.log` | 磁盘（仅指标，无对话内容） |
| LLM 监控记录 | `MonitoringRecordBuffer` → PostgreSQL | 数据库（仅模型指标，无对话内容） |

**核心问题：整个对话链路的内容在 SDK 层没有任何持久化。** 且对话内容与可观测指标分属两套系统（监控层有 `conversation_id` / `agent_id`，对话步骤却拿不到），无法按消息归因。

### 1.2 目标

- 对话内容（用户输入、模型输出、工具调用与结果、压缩摘要、卸载内容）可无损落盘。
- 支持**对话重启 / 恢复（resume）**与**分支（fork）**。
- 压缩既能 replay 出"压缩态上下文"，又能恢复被压缩掉的原文。
- 对话内容与可观测指标统一在一套身份体系下（session / turn / agent）。
- SDK 层零基础设施依赖（先 JSONL），未来可平滑迁移到 PostgreSQL，且不改 SDK 业务代码。

### 1.3 非目标

- 不做跨会话分析 / 多租户查询（那是 backend / PG 层的职责）。
- 不替换 `MonitoringRecordBuffer` 的模型监控（二者互补，本设计与之共用同一套 ID）。

---

## 2. 设计原则（第一性原理）

### 2.1 事件溯源：存"发生过的事件"，不存"内存对象"

不持久化 `AgentMemory` 这个对象，而是把对话还原成一条**只追加（append-only）的事件日志**。内存里的 `AgentMemory.steps`、`PreviousSummaryCache`、`OffloadStore` 全部是这条日志的**派生视图**，通过 replay 重建。

### 2.2 两层模型（解决压缩与有损渲染的关键）

```
Layer 1  原始事件日志（immutable, append-only）   ← 真相源，存全量
              │  render(events, compaction_state, offload_state)
              ▼
Layer 2  渲染上下文（derived, 不落盘或仅快照）      ← 喂给 LLM 的 message 列表
```

- **Layer 1** 存一切：完整工具原文、被压缩掉的 step、每次压缩的摘要、每个 offload 的全量内容。
- **Layer 2** 是纯函数 `render(...) -> messages`，输出统一的 `role + content[]` 格式（user/assistant/tool 结构一致）。

铁律：**截断（`max_observation_length`）、offload、压缩都只发生在渲染层；Layer 1 永远存原始全量。**

### 2.3 只追加，永不修改

压缩、改写、分支都通过**追加新事件**表达，老事件原地不动。resume = 从叶子沿父指针回溯；fork = 从中间节点挂新枝，老分支无损共存。

### 2.4 存储无关

记录 schema 与存储后端解耦。SDK 只认 `EventStore` 接口；JSONL 与 PG 各实现一份。

### 2.5 命名与落盘格式

落盘 JSONL 采用 **camelCase**（与 Claude Code 及《对比总结》一致，保证可移植 / 可对照）；Python 模型用 snake_case，通过 Pydantic `alias` 桥接。字段名对照见 §9.1。

---

## 3. 核心数据模型

### 3.1 身份层级（回答「event_id 与 session_id 是什么关系」）

二者是**身份层级中不同层的对象**：`session_id` 是分组键，`event_id` 是单条事件唯一标识，1 个 session 含 N 条 event。完整层级：

```
session_id   会话（最外层分组；= 监控层 conversation_id，二者统一）
  ├─ run_id      一次物理执行（一次 agent_run 调用）
  └─ turn_id     一次逻辑用户轮次（一条用户输入 + 它触发的全部行为）
       └─ llm_message_id   一次 LLM completion 产出的所有事件（→ 可重组为一条 API message）
            └─ event_id    单条事件（uuid）；parent_event_id 串成树（resume/fork）
```

数量关系：

| 关系 | 基数 | 说明 |
|------|------|------|
| session : turn | 1 : N | 多轮对话 |
| session : run | 1 : N | 平时 1 turn ≈ 1 run；resume/retry 时 1 turn 对应多 run |
| turn : event | 1 : N | 一次用户输入触发的全部事件 |
| llm_message_id : event | 1 : N | 一次模型调用拆出的 assistant_message + 若干 tool_call |
| tool_call_id : (调用,结果) | 1 : 1 | 跨进程精确配对 |

### 3.2 Event 模型（snake_case + camelCase alias）

```python
from enum import Enum
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict

SCHEMA_VERSION = 1

class EventType(str, Enum):
    user_input          = "user_input"
    system_prompt       = "system_prompt"
    assistant_reasoning = "assistant_reasoning"   # thinking / CoT
    assistant_message   = "assistant_message"
    tool_call           = "tool_call"
    tool_result         = "tool_result"
    compaction_summary  = "compaction_summary"
    offload_record      = "offload_record"
    error               = "error"
    run_lifecycle       = "run_lifecycle"

class Event(BaseModel):
    model_config = ConfigDict(populate_by_name=True)  # 接受 snake_case 或 alias 入参

    schema_version: int       = Field(SCHEMA_VERSION, alias="schemaVersion")
    # —— 身份 / 分组 ——
    event_id: UUID            = Field(alias="uuid")          # 单条事件唯一（Claude Code: uuid）
    parent_event_id: UUID|None= Field(None, alias="parentUuid")
    session_id: UUID          = Field(alias="sessionId")     # = 监控层 conversation_id
    run_id: UUID|None         = Field(None, alias="runId")   # 一次物理执行
    turn_id: UUID|None        = Field(None, alias="turnId")  # 一次逻辑用户轮次
    agent_id: str             = Field(alias="agentId")
    llm_message_id: str|None  = Field(None, alias="llmMessageId")  # = Claude Code message.id
    seq: int                  = Field(alias="seq")           # 会话内单调序号（确定性排序）
    # —— 类型 / 角色 ——
    type: EventType
    role: str                                                 # user | assistant | tool | system
    is_sidechain: bool        = Field(False, alias="isSidechain")
    sidechain_parent_id: UUID|None = Field(None, alias="sidechainParentId")
    step_index: int|None      = Field(None, alias="stepIndex")  # = ActionStep.step_number
    created_at: datetime      = Field(alias="timestamp")     # ISO8601 毫秒
    # —— 可观测性（消息级，对齐 step_metrics / MonitoringRecordBuffer） ——
    model_id: str|None        = Field(None, alias="model")
    usage: dict|None          = None                          # {"input_tokens":..,"output_tokens":..,"cached":..}
    latency_ms: int|None      = Field(None, alias="latencyMs")
    stop_reason: str|None     = Field(None, alias="stopReason")  # tool_use | end_turn
    # —— 渲染层视图状态 ——
    superseded_by: UUID|None  = Field(None, alias="supersededBy")  # 被哪条 compaction_summary 替代
    visibility: str           = "normal"                      # normal | internal | compressed_out
    # —— 内容 ——
    payload: dict             = {}                            # 分类型内容（见 3.4）
    metadata: dict            = {}                            # 扩展位（如 sdk_version）
```

> 落盘：`event.model_dump_json(by_alias=True, exclude_none=True)` → 一行 camelCase JSON。

### 3.3 会话与运行

```python
class Session(BaseModel):                # = 监控层 conversation
    session_id: UUID
    title: str | None = None
    head_event_id: UUID | None = None    # 当前活跃叶子（resume 入口）
    status: str = "active"
    created_at: datetime
    updated_at: datetime

class Run(BaseModel):
    run_id: UUID
    session_id: UUID
    turn_id: UUID                         # 本次执行所属逻辑轮次
    agent_id: str
    query: str
    status: str = "running"              # running | completed | interrupted | error
    stop_reason: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    final_answer: str | None = None
    model_config_snapshot: dict | None = None
    token_totals: dict | None = None
```

> `Session` / `Run` 是少量可变小对象（JSONL 实现里各为独立小文件，可覆写）；`Event` 是只追加的大头。

### 3.4 分类型 payload

```python
# user_input         {"text": str, "attachments": list|None}
# system_prompt      {"rendered_prompt": str, "tool_defs_hash": str, "injected_components": [str]}
# assistant_reasoning{"text": str}                          # visibility="internal"
# assistant_message  {"text": str, "is_final_answer": bool}
# tool_call          {"tool_call_id": str, "tool_name": str, "arguments": dict,
#                     "source": "local|mcp", "mcp_server": str|None}
# tool_result        {"tool_call_id": str, "output_raw": str, "output_chars": int,
#                     "is_error": bool, "duration_ms": int, "offload_handle": str|None}
#                     ← output_raw 存全量；截断/offload 是渲染层的事
# error              {"error_type": str, "message": str, "traceback": str, "step_index": int|None}
# run_lifecycle      {"action": "started|ended|interrupted|resumed", "stop_reason": str|None}
```

> `tool_call_id` 用 UUID（`tc_<uuid4hex16>`），全局唯一、跨 run / 跨进程可配对，替代当前 `call_<step_count>` 的局部递增。

### 3.5 `compaction_summary`（吸收 CurrentSummaryCache / CompressionCallRecord / summary_json_schema）

```python
{
  "structured_summary": {                  # = summary_json_schema 五段
      "task_overview": str, "completed_work": str, "key_decisions": str,
      "pending_items": str, "context_to_preserve": str },
  "covers_event_ids": [UUID],              # 被覆盖事件（或 covers_seq_range:[start,end]）
  "covered_pairs": int,                    # = PreviousSummaryCache.covered_pairs
  "end_steps": int,                        # = CurrentSummaryCache.end_steps
  "anchor_fingerprint": str,               # = hash(covers_event_ids) 或 last_covered_event_id
  "call_type": "full|incremental",
  "previous_summary_event_id": UUID|None,  # 增量链：上一份摘要
  "summarizer_model_id": str,
  "input_tokens": int, "output_tokens": int, "input_chars": int, "output_chars": int,  # = CompressionCallRecord
  "is_active": bool                        # 当前渲染是否采用这份摘要
}
```

- **增量压缩链**：`previous_summary_event_id` 串「上一份摘要 + 新 step → 新摘要」（对应 `incremental_summary_system_prompt`）；replay 取链尾 `is_active`。
- **`anchor_fingerprint`**：覆盖事件集合的指纹；resume/fork 时校验「摘要覆盖的前缀是否被改动」，决定复用还是重算——分支安全护栏。

### 3.6 `offload_record`（让 OffloadStore 落盘）

```python
{
  "handle": str,             # [[OFFLOAD:handle:desc]] 的 handle
  "description": str,
  "original_chars": int,
  "preview": str,            # head+tail 摘要
  "content_ref": str,        # 全量另存引用，如 "blob://<sha256>"
  "source_event_id": UUID    # 来自哪条 tool_result
}
```

> 大 blob 不进 Event 主体，单独存，Event 仅放 `content_ref`。

---

## 4. 存储后端

### 4.1 窄接口 `EventStore`（SDK 唯一认的东西）

```python
from abc import ABC, abstractmethod

class EventStore(ABC):
    @abstractmethod
    def append(self, event: Event) -> None: ...
    @abstractmethod
    def next_seq(self, session_id: UUID) -> int: ...
    @abstractmethod
    def read_session(self, session_id: UUID) -> list[Event]: ...        # 全部事件，按 seq 升序
    @abstractmethod
    def read_branch(self, leaf_event_id: UUID) -> list[Event]: ...      # 沿 parent 回溯到根
    @abstractmethod
    def get_session(self, session_id: UUID) -> Session: ...
    @abstractmethod
    def upsert_session(self, s: Session) -> None: ...
    @abstractmethod
    def put_blob(self, data: str) -> str: ...                           # 返回 content_ref
    @abstractmethod
    def get_blob(self, content_ref: str) -> str: ...
```

> **铁律：ContextManager / replay / agent loop 只调用 `EventStore`，绝不直接 open 文件 / 写 SQL。** 这是 JSONL→PG 迁移成本接近零的根本原因。

### 4.2 JSONL 实现（第一阶段）

```
<root>/
  <session_id>/
    events.jsonl        # 单一只追加日志：所有 agent 的事件按 seq 全序混排
    meta.json           # Session 小对象（可覆写）
    runs.jsonl          # Run 记录
    blobs/<sha256>      # large_content：offload 全量、超长工具原文
```

> **刻意不按 agent 拆文件**（见 §10）：handoff 跨 agent，拆文件会破坏全局时序与 `parent_event_id` 树。按 agent 查看 = 用 `agent_id` 过滤。

实现要点：`append` = `open('a')` 写一行 + `fsync`；`next_seq` 进程内计数，冷启动读末行 `seq+1`；`read_branch` 先全量入内存建 `event_id->Event` 再回溯；`put_blob` 用 `sha256` 作文件名（内容寻址、天然去重）。

**已知短板（明确边界）**：单文件并发写、单行超约 4KB 多进程可能交错（SDK 当前单进程单会话无此问题）；跨会话查询是全量扫描（→ 迁 PG 的动机，非 SDK 职责）。

### 4.3 PostgreSQL 实现（未来阶段）

```sql
CREATE TABLE event (
    event_id        UUID PRIMARY KEY,
    session_id      UUID NOT NULL,
    run_id          UUID,
    turn_id         UUID,
    parent_event_id UUID,
    seq             BIGINT NOT NULL,
    type            TEXT NOT NULL,
    agent_id        TEXT,
    created_at      TIMESTAMPTZ NOT NULL,
    schema_version  INT NOT NULL,
    record          JSONB NOT NULL,        -- 完整 Event（与 JSONL 行一致）
    UNIQUE (session_id, seq)
);
CREATE INDEX ON event (session_id, seq);
CREATE INDEX ON event (parent_event_id);
CREATE INDEX ON event (turn_id);
```

> `record` 列直接装 JSONL 那一整行 → **迁移 = 逐行 INSERT，无损**。blob 走 `large_content` 表或对象存储，`content_ref` 不变。

### 4.4 迁移规则（现在就要遵守）

1. 唯一 `Event` 模型 + 统一序列化；每条带 `schema_version`。
2. SDK 只依赖 `EventStore` 抽象。
3. blob 一开始就外置（`content_ref`）。
4. `seq` 写进记录本体，不依赖后端自增。
5. 迁移脚本：`for line in events.jsonl: INSERT`。

---

## 5. 压缩与 Offload 的持久化（Layer 1 ↔ Layer 2）

```python
def render(events: list[Event]) -> list[Message]:
    active = pick_active_compaction(events)   # is_active 且 anchor_fingerprint 校验通过
    out = []
    for ev in events:
        if active and ev.event_id in active.covers_event_ids:
            continue                          # 被压缩掉的原文：渲染跳过，仍在 Layer 1
        if ev.type == EventType.tool_result and ev.payload.get("offload_handle"):
            out.append(offload_marker(ev))    # [[OFFLOAD:handle:desc]]
        else:
            out.append(to_message(ev))
    if active:
        out.insert(pos, summary_message(active))
    return group_by_llm_message(out)          # 同 llm_message_id 合并为一条 role+content[] 消息
```

- 压缩 = 追加 `compaction_summary` 事件，被覆盖的原始 step 仍留 Layer 1（可审计/恢复）。
- offload = 工具结果全量入 Layer 1，渲染替换为标记；agent 经 `reload_original_context_messages` 按 handle 拉回，跨进程 resume 后仍可用（`offload_record` 已落盘、blob 懒加载）。

---

## 6. 重启 / 恢复 / 分支（replay）

### 6.1 同会话 resume

```
1. read_branch(session.head_event_id) → 事件链
2. 重建 AgentMemory.steps：user_input→TaskStep；assistant_*+tool_call/result→ActionStep；
   active compaction_summary→SummaryTaskStep
3. 重建 PreviousSummaryCache：取链尾 is_active 摘要，还原 summary_text/covered_pairs/anchor_fingerprint
4. 重建 OffloadStore：从 offload_record 重灌 handle→preview，blob 懒加载
5. 继续 ReAct 循环（新 run_id，沿用同一 turn_id 或新建）
```

### 6.2 分支 / fork

选任一 `event_id` 作 fork 点，新 run 首条事件 `parent_event_id = fork点`；老分支无损共存。`anchor_fingerprint` 校验被改动的摘要前缀，失效则重算。

### 6.3 幂等

唯一约束 `(run_id, step_index, type, tool_call_id)` 去重，重试/续写不产生重复事件。

---

## 7. 接入点（SDK 内部）

| 写入时机 | 现有位置 | type | 内容来源 |
|---------|---------|------|---------|
| 用户输入到达 | `nexent_agent.py` add history 前 | user_input | `AgentRunInfo.query` |
| LLM 输出文本 | `core_agent.py` append ActionStep | assistant_message | `model_output` |
| LLM 请求工具 | `core_agent.py` 创建 ToolCall | tool_call | `tool_calls` + `code_action` |
| 工具返回 | `core_agent.py` 设 observations | tool_result | `observations` |
| 最终回复 | `core_agent.py` FinalAnswerStep | assistant_message(is_final) | `final_answer` |
| 压缩完成 | `manager.py` compress 回调 | compaction_summary | `CurrentSummaryCache` + `CompressionCallRecord` |

- **以 step 边界为权威写入**（`ActionStep` 定稿 = canonical truth）；`MessageObserver` 流仅补「未完成 step 的预览 / 中断快照」。
- `turn_id` 在 `agent_run()` 入口生成并向下传递；`llm_message_id` 取自 LLM 响应的 `id`（无则本地生成），同一次调用的 assistant_message + tool_call 共享。
- `AgentRunInfo` 增加可选 `event_store` 字段注入。

---

## 8. 分阶段落地

| 阶段 | 内容 | 价值 |
|------|------|------|
| P0 | `Event` 模型 + `EventStore` 抽象 + JSONL 实现 + uuid/parentUuid + tool_call_id 改 UUID | 契约立住，对话不再丢失 |
| P1 | turn_id / llm_message_id / timestamp / model+usage 落消息级 + 同会话 resume | 可续跑、可按消息归因 |
| P2 | `compaction_summary` + `offload_record` 持久化与 replay | 压缩/卸载可跨会话恢复 |
| P3 | 分支 fork + JSONL 导出器 + 环境信息(sdk_version) | 多分支调试、可移植 |
| P4 | PG 实现 + 迁移脚本（按需，backend 协作） | 跨会话查询 / 多租户 / 并发 |

---

## 9. 附录

### 9.1 字段命名对照表

| 本设计（Python / JSONL alias） | Claude Code | 《对比总结》 | 含义 |
|------|------|------|------|
| `event_id` / `uuid` | `uuid` | `uuid` | 单条事件唯一 ID |
| `parent_event_id` / `parentUuid` | `parentUuid` | `parentUuid` | 父事件，链/树 |
| `session_id` / `sessionId` | `sessionId` | `sessionId`(=conversation_id) | 会话分组 |
| `agent_id` / `agentId` | `agentId` | `agentId` | 代理 |
| `turn_id` / `turnId` | `promptId` | `turnId` | 逻辑用户轮次 |
| `run_id` / `runId` | —（无独立概念） | — | 物理执行（本设计新增，区别于 turn） |
| `llm_message_id` / `llmMessageId` | `message.id` | （指出为缺口） | 一次 LLM 调用分组 |
| `tool_call_id`（payload）| `tool_use_id` | `toolCallId` | 工具调用↔结果配对（UUID） |
| `created_at` / `timestamp` | `timestamp` | `timestamp` | ISO8601 毫秒 |
| `model_id` / `model` | `message.model` | `model` | 模型名 |
| `usage` | `message.usage` | `usage` | token 用量 |
| `stop_reason` / `stopReason` | `stop_reason` | `stopReason` | 停止原因 |
| `seq` | —（靠文件行序） | — | 确定性排序（本设计新增） |
| `covers_event_ids` / `anchor_fingerprint` / `content_ref` | — | —（最小集未含） | 压缩恢复 / 分支安全 / offload 全量（本设计保留） |

### 9.2 JSONL 单行样例（camelCase）

```json
{"schemaVersion":1,"uuid":"a1...","parentUuid":"a0...","sessionId":"c0...","runId":"r0...","turnId":"t0...","agentId":"main","llmMessageId":"chatcmpl-x","seq":7,"type":"tool_call","role":"assistant","stepIndex":3,"timestamp":"2026-06-18T09:00:00.123Z","model":"gpt-4o","usage":{"input_tokens":1200,"output_tokens":80},"payload":{"tool_call_id":"tc_9f3a...","tool_name":"web_search","arguments":{"q":"..."},"source":"mcp","mcp_server":"search"}}
```

---

## 10. 与《对比总结》的取舍（批判性吸取）

| 对比总结的建议 | 处理 | 理由 |
|------|------|------|
| `uuid` + `parentUuid` 链式 | ✅ 采纳 | 定位 / 检索 / 回放的基础 |
| `tool_call_id` 改 UUID | ✅ 采纳 | 局部递增 `call_N` 跨 run 不唯一 |
| `turn_id` 轮次追踪 | ✅ 采纳并细化 | **但 turn_id ≠ run_id**：resume/retry 时 1 turn → N run，故两者并存 |
| 每条消息带 `model` + `usage` + `timestamp` | ✅ 采纳 | 成本/时序归因到消息级 |
| 统一 `role + content[]` 消息格式 | ✅ 采纳（在渲染层） | 存储用细粒度事件，render 时合并回统一格式 |
| LLM 调用 ID（被列为缺口） | ✅ 补齐为 `llm_message_id` | 顺带解决「消息粒度 vs 事件粒度」 |
| 实时逐条追加、不攒批 | ✅ 采纳 | 崩溃不丢；以 step 边界为权威 |
| 「一次 LLM 调用写一行」（消息粒度） | ⚠️ 部分采纳 | 改为**事件粒度行 + llm_message_id 归组**：offload/压缩需对单个 tool_result 精确寻址 |
| 按 agent 拆 `main.jsonl` + `agents/<id>.jsonl` | ❌ 不采纳 | handoff 跨 agent，拆文件破坏全局时序与父子树；改单文件 + `agent_id` 过滤 |
| 12 字段「最小完备」集 | ❌ 不降级 | 缺 `seq`/`covers_event_ids`/`anchor_fingerprint`/`content_ref`，无法做可逆压缩恢复、分支安全、offload 落盘——这些正是本设计的核心能力。命名向其对齐，字段集合保持完整 |
| 环境信息（version/cwd/gitBranch） | 🔸 选择性 | SDK 非 CLU，cwd/gitBranch 意义弱；仅保留 `sdk_version`（入 `metadata`）用于兼容性 |

---

## 11. 集成设计（与现有代码的耦合）

> 本章基于通读 `core_agent.py`（1102 行，`CoreAgent(CodeAgent)`）、`nexent_agent.py`（724 行，`NexentAgent`）、`run_agent.py`（153 行，`agent_run_thread`）。**前十章是"存什么"，本章是"在运行中怎么把它接进去"，是真正的工作量与风险所在。**

### 11.0 一个前置事实：CoreAgent 是 CodeAct（CodeAgent）

一个 ReAct step 在 smolagents `CodeAgent` 下只有**一个** `python_interpreter` ToolCall，其 `arguments` 是整段代码；真正的工具是在代码内部被调用的，靠 `invoked_tool_signatures` 抽取。因此：

- 持久化的"工具粒度"是**代码执行步**，不是离散函数调用。
- `tool_call` 事件：`tool_name="python_interpreter"`，`arguments` = `code_action`（或 ContextManager 开启时的 compact 签名），把 `invoked_tool_signatures` 放入 `metadata.invoked_tools`。
- `tool_result` 事件：来自 `code_output`（`logs` + 末值 `output`），见 §11.2「截断前抓原文」。

### 11.1 调用流时序

```
run_agent.agent_run_thread (run_agent.py:77)
  └─ NexentAgent.create_single_agent (nexent_agent.py:373)   # 递归建主/子 agent
  └─ [resume] 注入历史 / 重建记忆                              # 见 §11.4
  └─ NexentAgent.agent_run_with_observer (nexent_agent.py:524)
       └─ CoreAgent.run(query, stream=True, reset=...) (core_agent.py:672)
            ├─ build_system_prompt + memory.steps.append(TaskStep)  (715-733)
            └─ _run_stream → 多次 _step_stream (core_agent.py:401)
                 ├─ write_memory_to_messages()                       (409)
                 ├─ context_manager.compress_if_needed(...)          (424)  ← 压缩 hook
                 ├─ chat_message = self.model(input_messages)        (456)  ← assistant hook
                 ├─ ToolCall(name="python_interpreter", ...)         (507)  ← tool_call hook
                 ├─ python_executor(code_action) → code_output       (~520)
                 └─ memory_step.observations = ... (原地截断)          (~600) ← tool_result hook（截断前）
       └─ 消费循环逐个吃 ActionStep（524 内）                          ← 大部分事件最干净的写入点
```

### 11.2 写入 hook 表（带真实行号）

| 事件 type | 位置 | 关键约束 |
|----------|------|---------|
| `run_lifecycle(started)` / `system_prompt` / `user_input` | `core_agent.py:715-733` | TaskStep append、build_system_prompt 处 |
| `assistant_message` / `assistant_reasoning` + `usage` | `core_agent.py:456` | `llm_message_id` 取 `chat_message` 原始响应 id（无则本地生成），同次调用的 assistant + tool_call 共享 |
| `tool_call` | `core_agent.py:507` | name=python_interpreter；signatures 入 metadata |
| `tool_result` | `core_agent.py:~600` | **必须在 `max_observation_length` 原地截断之前**读 `code_output.output`/`observation`；全量入 `output_raw`（或 blob） |
| `compaction_summary` | `manager.py` `compress_if_needed` 内 | 摘要在 ContextManager 缓存，不在 step 循环；从 `CurrentSummaryCache` + `CompressionCallRecord` 取字段 |
| `offload_record` + blob | `OffloadStore.put` / 压缩归档时 | `[[OFFLOAD:handle:desc]]` 的 handle ↔ 全量 blob |
| `error` | `_step_stream` 异常路径 / 消费循环 `step_log.error` | — |
| `run_lifecycle(ended/interrupted)` / 最终 `assistant_message` | `agent_run_with_observer:524` 消费循环 + `stop_event` 检查 | final_answer 已在此处理 |

**结论：不存在单一 sink。** 多数结构化事件在 `agent_run_with_observer` 的 ActionStep 消费循环里写最干净；但"截断前原文 / 压缩 / offload"这三类拿不到完成态 ActionStep，必须在 `_step_stream` 与 ContextManager / OffloadStore 内另开 hook。

### 11.3 ID 穿线（触及全部三个文件）

| ID | 生成处 | 流向 |
|----|--------|------|
| `session_id` | 从 `AgentRunInfo` / 监控 `AgentRunMetadata` 取（统一二者） | 贯穿全程 |
| `run_id` | `agent_run_thread` 或 `agent_run_with_observer` 入口 | 一次执行 |
| `turn_id` | 同上入口（一次用户输入一个） | 本轮全部事件 |
| `agent_id` | `create_single_agent` 建每个 agent 时 | 该 agent 的事件 |
| `llm_message_id` | `_step_stream` 模型响应 | 同次调用事件归组 |
| `seq` | `EventStore.next_seq(session_id)` | 单调排序 |
| `parent_event_id` | 循环内维护"当前父指针"游标 | 链/树 |

落地动作：`AgentRunInfo` 增 `event_store` / `session_id` / `resume_*` 字段；`NexentAgent` 持有 store 与当前 `turn_id` / 父指针游标；`CoreAgent` 在 `_step_stream` 写事件时引用之。三层签名都要小改。

### 11.4 Resume：可选保真度（本章核心）

现状 `add_history_to_agent`（`nexent_agent.py:465`）已是一个 resume 接缝，但它把每轮塌缩成只有 `content` 的裸 TaskStep / ActionStep（丢 tool_calls、observations、token_usage、model_input），并设置内存态 `_history_step_count`。我们**不新增并行链路，而是把这个接缝抽象成一个可选保真度的重建器**，现状行为成为其中的 Level 0。

```python
class MemoryReconstructor:
    """events (EventStore) → 注入 CoreAgent 的 memory / 压缩缓存 / offload。"""
    def reconstruct(self, agent: CoreAgent, events: list[Event],
                    fidelity: Literal["lossy", "faithful"]) -> None: ...
```

入口改为：
```python
# AgentRunInfo 新增：resume_from(session_id|leaf_event_id), resume_fidelity
if agent_run_info.resume_from:
    events = event_store.read_branch(leaf) or read_session(sid)
    MemoryReconstructor().reconstruct(agent, events, agent_run_info.resume_fidelity)
else:
    nexent.add_history_to_agent(agent_run_info.history)   # 旧路径保留
agent_run_with_observer(query, reset=False)   # resume 时一律 reset=False，绝不 memory.reset()
```

#### Level 0 — 有损桩（与现状等价）

- 只重建：`user_input` → `TaskStep(task=text)`；每轮最终 `assistant_message` → `ActionStep(action_output=text, model_output=text)`。
- 丢弃 tool_call / tool_result / reasoning / 压缩 / offload。
- 设 `_history_step_count = len(steps)`。
- **特性**：与 smolagents step 形状几乎无耦合，**可跨 SDK 版本、跨模型安全 resume**；成本低、上下文短。
- **适用**：向后兼容、跨模型续聊、token 预算紧、只要对话连续性的场景。

#### Level 1 — 全量忠实重建

- 按 `step_index` / `llm_message_id` 把事件归组，逐步重建完整 `ActionStep`：`model_output` / `model_output_message` / `code_action` / `tool_calls`（保留原始 id）/ `observations` / `token_usage` /（可选）`model_input_messages` / `error` / `invoked_tool_signatures`。
- **observations 用"按当时所见"渲染**：对全量 `output_raw` 跑 §5 的 `render`（套用 active 压缩 + offload 标记 + 截断），保证 `write_memory_to_messages` 还原出与当时一致的上下文。
- **恢复压缩态**：从链尾 `is_active` 的 `compaction_summary` 重建 `SummaryTaskStep` 与 `PreviousSummaryCache`（`summary_text` / `covered_pairs` / `anchor_fingerprint`），使下一次 `compress_if_needed` 走增量路径而非从头压。
- **恢复 offload**：从 `offload_record` 重灌 `OffloadStore`（handle→preview），blob 懒加载，`reload_original_context_messages` 工具续可用。
- 恢复 `_history_step_count`。
- **特性**：真正的连续续跑——压缩连续、offload 可拉回、成本/时序归因完整。代价是**与 smolagents step 形状强耦合**（见 §11.6）。
- **适用**：长任务断点续跑、需要保留压缩/卸载上下文、审计回放。

> 两级是同一光谱的两端；如需中间档（忠实对话但丢重型工件），在重建器里加开关即可，schema 无需变。

### 11.5 子 Agent（sidechain）传播

`managed_agents` 被 `send_tools` 给 python_executor（`core_agent.py:736`），子 agent 调用 = 代码内跑另一个 `CoreAgent` 循环（独立 memory / observer）。要持久化子 agent 事件并正确挂 `is_sidechain` / `sidechain_parent_id`：在 `create_single_agent`（`nexent_agent.py:373`）递归构建时，把 `event_store`、`session_id`、以及"父级当前 event_id"一路注入每个子 agent；子 agent 的 `_step_stream` 同样写事件，`parent_event_id` 指向触发它的父级 tool_call 事件。

### 11.6 smolagents 耦合隔离（长期风险）

`CoreAgent` 已 override `_step_stream`，等于已在维护一份对 smolagents 内部（`memory.steps` 形状、`get_full_steps()` 格式、`write_memory_to_messages`）的耦合分叉。持久化会扩大这个面，尤其 Level 1 重建。隔离策略：

- **只把自有 `Event` 模型当真相**；写入是 `ActionStep → Event` 的单向投影，重建是 `Event → ActionStep` 的适配器，二者集中在一个模块。
- smolagents 升级只需改这一个适配器；Level 0 因几乎不依赖 step 内部细节，是升级时的安全兜底。
- 为 `ActionStep` 字段映射加版本快照测试。

### 11.7 测试策略（验收标准）

1. **往返一致**：跑一段会话 → 落库 → Level 1 重建 → `write_memory_to_messages()` 与原 run 逐条消息一致。
2. **续跑等价**：A（不中断跑到底）vs B（中途落库→新进程 Level 1 resume→续跑），最终 memory / final_answer 等价。
3. **压缩连续**：resume 后首次 `compress_if_needed` 命中增量路径（`anchor_fingerprint` 校验通过），不重复全量压缩。
4. **offload 可用**：resume 后 `reload_original_context_messages` 能按 handle 取回 blob。
5. **Level 0 退化**：去掉重型事件仍能 resume，且与旧 `add_history_to_agent` 行为一致。
6. **子 agent**：handoff 场景下父子事件树（`parent_event_id` / `sidechain_parent_id`）可完整重建。

### 11.8 工作量分解（相对 §8）

| 模块 | 归属阶段 | 说明 |
|------|---------|------|
| `Event` / `EventStore` / JSONL | P0 | 纯新增，无耦合 |
| 写入 hook（assistant/tool_call/tool_result）+ ID 穿线 | P1 | 改 `_step_stream` 与消费循环，触及三文件 |
| 截断前抓原文 + blob | P1 | 改 `_step_stream` 观察处理顺序 |
| Level 0 resume（抽象现状桩） | P1 | 重构 `add_history_to_agent` |
| `compaction_summary` / `offload_record` 写入 | P2 | 改 ContextManager / OffloadStore |
| Level 1 resume（重建器 + 压缩/offload 恢复） | P2 | 耦合最重、风险最高 |
| 子 agent 传播 | P3 | 改 `create_single_agent` 递归注入 |
| smolagents 适配器隔离 + 版本测试 | 贯穿 | 控制长期维护成本 |