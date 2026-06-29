# Resume 测试方案

> 日期：2026-06-25
> 目标：验证从 JSONL 恢复对话后，新事件能正确追加到同一 session 的 JSONL 文件中，
> 且 step_number 连续、_step_event_map 正确、compaction 链完整。

---

## 1. 测试前提

### 已修复的 Bug

| Bug | 文件 | 修复内容 |
|-----|------|---------|
| Bug 9 | `core_agent.py:986` | `_run_stream` 中 step_number 根据 `_history_step_count` 续接 |
| Bug 10 | `run_agent.py:229-241` | resume 后调用 `_register_step_event_map` 填充映射 |

### 已知风险项（暂不修复，但测试中需观察）

| 风险 | 说明 |
|------|------|
| session_id 一致性 | resume 时 session_id 完全依赖调用方传入，无 branch 校验 |
| 缺少 "resumed" lifecycle | RunLifecyclePayload.action 支持 "resumed" 但未被使用 |

### 测试基础设施

- 测试文件：`temp_scripts/test_context_manager.py`
- 工具函数：`temp_scripts/test_utils.py`
  - `build_agent_run_info(query, history, ..., event_store, session_id)` → `AgentRunInfo`
  - `run_agent_with_tracking(agent_run_info)` → `AgentRunResult`
  - `create_event_store(persistence_dir)` → `(store, session_id)`
- `AgentRunInfo` 关键字段：`event_store`, `session_id`, `resume_from`, `resume_fidelity`

### 需要对 test_utils.py 的改动

当前 `build_agent_run_info` 不接受 `resume_from` / `resume_fidelity` 参数。
需在函数签名中添加这两个 Optional 参数，透传到 `AgentRunInfo` 构造。

---

## 2. 测试场景

### T1: 基本 Resume + 追加（验证 Bug 9/10 修复）

**目的：** 验证恢复后 step_number 续接、事件追加到同一 JSONL、seq 连续。

**步骤：**
```
1. 创建 EventStore + Session S1
2. 第一轮：传入 history + query1，运行 agent（不设 resume_from）
   → agent 产生 N 个步骤
3. 读取 events.jsonl，获取最后一条 assistant_message 事件的 event_id 作为 leaf_id
4. 第二轮：传入 event_store=S1.store, session_id=S1.sid, resume_from=leaf_id
   → 不传 history（resume 路径不使用 history）
5. agent 继续运行 M 个步骤
6. 验证：
   a. events.jsonl 中总事件数 = 第一轮事件数 + 第二轮事件数
   b. 第二轮新步骤的 step_number 从 N+1 开始（不与第一轮冲突）
   c. 第二轮事件的 seq 连续（无间隔）
   d. 两次运行共享同一 session_id
   e. runs.jsonl 中有两条 Run 记录（不同 run_id，同一 session_id）
```

**关键验证点：**
- `step_number` 续接（Bug 9）
- `_step_event_map` 中有第一轮事件的映射（Bug 10）
- EventStore seq 连续

### T2: Resume 后触发 Compaction

**目的：** 验证恢复后再次压缩时 covers_event_ids 正确引用旧步骤事件。

**步骤：**
```
1. 创建 EventStore + Session S2
2. 配置 ContextManager(token_threshold=3000, enabled=True)
3. 第一轮：传入较长的 history + query1，确保触发压缩
4. 读取 events.jsonl，获取 leaf_id
5. 第二轮：resume_from=leaf_id，继续运行 query2（足够长以再次触发压缩）
6. 验证：
   a. 第二轮的 compaction_summary 事件的 covers_event_ids 不为空
   b. covers_event_ids 中的 event_id 可在 events.jsonl 中找到对应事件
   c. 被覆盖的事件 step_number 包含第一轮的步骤（证明旧映射生效）
```

**关键验证点：**
- `_step_event_map` 在 resume 后被正确填充（Bug 10）
- `compressed_step_numbers` → `covers_event_ids` 的映射链路

### T3: 多轮连续 Resume

**目的：** 验证多次恢复+追加后 JSONL 完整性。

**步骤：**
```
1. 创建 EventStore + Session S3
2. 第一轮：query1 → 产生 N1 步 → leaf_id_1
3. 第二轮：resume_from=leaf_id_1, query2 → 产生 N2 步 → leaf_id_2
4. 第三轮：resume_from=leaf_id_2, query3 → 产生 N3 步
5. 验证：
   a. events.jsonl 包含全部 3 轮事件，seq 全程单调递增无间隔
   b. step_number 全程单调递增：1..N1, N1+1..N1+N2, N1+N2+1..N1+N2+N3
   c. runs.jsonl 有 3 条 Run 记录
   d. read_branch(leaf_id_2) 返回的事件链包含第一轮和第二轮事件
```

**关键验证点：**
- 多次 resume 后 `_history_step_count` 累积正确
- `step_number` 多次续接无冲突
- `read_branch` 沿 parent_event_id 回溯跨越多个 Run

### T4: Level 1 (faithful) Resume

**目的：** 验证高保真恢复路径的正确性。

**步骤：**
```
1. 创建 EventStore + Session S4
2. 第一轮：运行多步，触发压缩 + offload
3. 第二轮：resume_from=leaf_id, resume_fidelity="faithful"
4. 验证：
   a. agent.memory.steps 包含 SummaryTaskStep（压缩摘要步骤）
   b. OffloadStore 内部字典被重灌（可通过 reload_original_context_messages 工具调用验证）
   c. _step_event_map 包含 compaction_summary 之前的步骤映射
   d. 第二轮新步骤 step_number 正确续接
```

**关键验证点：**
- Level 1 重构完整性
- OffloadStore 重灌 + blob 懒加载
- `_step_event_map` 对重构步骤的覆盖

### T5: Resume Fallback（异常路径）

**目的：** 验证 resume 失败时降级到 history 的行为。

**步骤：**
```
1. 创建 EventStore + Session S5
2. 第一轮：正常运行
3. 第二轮：传入一个不存在的 resume_from UUID
4. 验证：
   a. 不崩溃（try/except 捕获异常）
   b. 降级到 add_history_to_agent（使用传入的 history）
   c. 日志中有 warning："Event resume failed, falling back to history"
   d. agent 正常运行并产生结果
```

---

## 3. 辅助函数设计

### `find_leaf_event_id(store, session_id) -> str`

从 EventStore 中读取 session 的所有事件，返回最后一条 `assistant_message`（`is_final_answer=True`）的 `event_id`。用于确定 resume_from 的值。

```python
def find_leaf_event_id(store, session_id) -> str:
    from uuid import UUID
    sid = UUID(session_id) if isinstance(session_id, str) else session_id
    branch = store.read_session(sid)
    for ev in reversed(branch.events):
        if ev.type.value == "assistant_message" and getattr(ev.payload, 'is_final_answer', False):
            return str(ev.event_id)
    # fallback: last event
    return str(branch.events[-1].event_id)
```

### `validate_step_continuity(events) -> dict`

验证事件列表中 step_number 和 seq 的连续性，返回检查结果。

```python
def validate_step_continuity(events) -> dict:
    seqs = [ev.seq for ev in events]
    step_indices = [ev.step_index for ev in events if ev.step_index is not None]
    return {
        "seq_continuous": seqs == list(range(seqs[0], seqs[-1] + 1)),
        "step_number_monotonic": step_indices == sorted(step_indices),
        "step_number_no_dup_in_run": ...,  # 同一 run_id 内无重复 step_index
        "total_events": len(events),
        "unique_step_indices": len(set(step_indices)),
    }
```

### 对 `build_agent_run_info` 的改动

添加 `resume_from` 和 `resume_fidelity` 参数：

```python
def build_agent_run_info(
    ...
    event_store=None,
    session_id=None,
    resume_from=None,          # 新增
    resume_fidelity=None,      # 新增
) -> AgentRunInfo:
    ...
    return AgentRunInfo(
        ...
        event_store=event_store,
        session_id=session_id,
        resume_from=resume_from,          # 新增
        resume_fidelity=resume_fidelity,  # 新增
    )
```

---

## 4. 测试执行顺序

```
T1 (基本 resume) → 确认 Bug 9/10 修复生效
  ↓
T2 (resume + compaction) → 确认 covers_event_ids 正确
  ↓
T3 (多轮 resume) → 确认累积正确性
  ↓
T4 (Level 1 resume) → 确认高保真路径
  ↓
T5 (resume fallback) → 确认异常降级
```

T1 是阻断性测试——如果不通过，后续场景无意义。

---

## 5. 运行方式

在项目虚拟环境中执行：

```bash
cd D:\WeLink_data_files\z50055340\ReceiveFiles\nexent\nexent_log\nexent
backend\.venv\Scripts\python.exe -m sdk.nexent.core.agents.temp_scripts.test_context_manager
```

或直接在 `test_context_manager.py` 的 `__main__` 中添加测试入口。

---

## 6. 注意事项

1. **LLM 调用成本**：每个测试场景都会调用真实 LLM，需控制 `max_steps` 避免过多调用。
   T1/T5 用 `max_steps=3`，T2/T3 用 `max_steps=10`，T4 用 `max_steps=10`。

2. **persistence 目录清理**：当前测试不清理 persistence/ 目录，多次运行会累积。
   每个测试用独立的 `persistence_dir` 避免干扰。

3. **resume_from 精确性**：leaf_id 必须是 `assistant_message(is_final_answer=True)` 的事件 ID，
   这样 `read_branch` 回溯时能覆盖完整的一轮对话。如果误选中间事件，可能截断。

4. **ContextManager 跨 Run 复用**：T2 中需要确保同一 ContextManager 在两次运行间复用，
   否则压缩缓存状态丢失。这与现有 `run_multi_turn` 中的 `shared_cm` 模式一致。

5. **history 参数**：resume 路径不使用 `history`，但仍需传入空列表 `[]` 以满足 `AgentRunInfo` 的必填约束。
   如果传入了非空 history 且 `resume_from` 也设置了，resume 路径优先（`_finalize_persistence` 先检查 `resume_from`）。

6. **shared_cm 在 resume 中的问题**：resume 时，ContextManager 内部的 `_previous_summary_cache`
   是从第一轮运行保留的。但 resume 路径通过 reconstructor 恢复 memory，reconstructor 会设置
   `agent._history_step_count`。ContextManager 的 `compress_if_needed` 使用
   `_history_step_count` 来区分 history 步骤和当前 run 步骤，所以两者需一致。
   Level 1 重构中 `_restore_compaction` 会将 SummaryTaskStep 插入 memory，
   这与 CM 内部的 cache 状态需要协调——这是一个需要观察的潜在问题。
