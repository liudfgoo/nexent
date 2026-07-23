# Generic Benchmark：统一 ContextItems runtime 行为与差距

> 本版基于 2026-07-23、PR #3475 合入后的代码。旧 Legacy/Managed A/B/C 说明已失效。
> 历史 L 基线固定为 `32152c3bf7d43c37ff36336080d120284a42046d`。

## 当前定位

Generic Benchmark 是由 Langfuse dataset 驱动、使用简化平台配置构造器的真实 Nexent SDK
Core benchmark。它执行真实 ReAct、模型、工具、ContextManager、ContextItems assembly
和可选 adaptive compaction，但不是完整平台端到端测试。

调用链：

```text
DatasetItem
  -> generic/run_benchmark.py
  -> generic/task_adapter.py
  -> benchmark/agent_runner.py
  -> agent_run()
  -> NexentAgent / CoreAgent
  -> ManagedContextRuntime
  -> ContextManager + ContextItems + processing policy
  -> model/tools
```

ContextManager 是唯一生产上下文路径。`ContextProcessingMode` 控制：

- `passthrough`：统一 assembly，不执行 adaptive compaction；
- `adaptive_compact`：统一 assembly，并在 soft budget 以上执行压缩。

旧 `LegacyContextRuntime` 已删除；`--enable-context-manager` /
`--disable-context-manager` 仅作为兼容别名映射到上述 policy。

## 标准 P/C 实验

```text
P: context_items + passthrough
C: context_items + adaptive_compact
```

P/C 同 commit、同 ContextItems、同工具、同 prompt、同 budget，只改变 processing policy。
因此 P/C 才是当前 compression 增量效果的主要对照。

旧 L 只能在固定历史 worktree 中运行：

```text
L commit: 32152c3bf7d43c37ff36336080d120284a42046d
L vs P: 架构迁移整体影响
P vs C: adaptive compaction 增量影响
L vs C: 产品版本整体影响
```

## 已就绪能力

- 单组 `run_benchmark.py` 支持显式 `--context-processing-mode`；
- P/C comparison runner、smoke、repeat 和交错顺序；
- manifest schema v2；
- resolved processing mode、policy fingerprint、soft/hard budget；
- ContextEvidence 的 item、policy、budget、compression 和 overflow 字段；
- 每次模型调用的 content-free fingerprint evidence；
- provider prefix cache 与 ContextManager summary cache 分开统计；
- Langfuse dataset-run eventual-consistency 等待；
- P/C item-ID 完整配对；
- manifest parity 和同名保护；
- `context_evidence_diff.py` 首次输入差异定位。

## Manifest v2

关键字段：

```text
manifest_schema_version: 2
context_runtime: context_items
context_processing_mode: passthrough | adaptive_compact
adaptive_compaction_enabled: bool
context_policy_fingerprint: sha256
context_manager:
  token_threshold
  context_window_tokens
  soft_input_budget_tokens
  hard_input_budget_tokens
  keep_recent_steps
```

manifest 不再记录虚假的 `legacy/managed` runtime。compatibility CLI 名称不会改变 manifest
真实语义。

## ContextEvidence

每次模型调用的证据包括：

- `purpose`；
- selected item IDs/types；
- stable/dynamic message count；
- message/tool/system/history fingerprints；
- policy fingerprint 和 processing mode；
- raw/final token estimates；
- soft/hard budget；
- history compression 是否触发；
- representation/cache 信息；
- `compact_exhausted` / `over_hard_budget`；
- summary coverage/persist status。

`CoreAgent` 在 provider 调用前检查 `over_hard_budget`。P 组不压缩，因此长上下文可能被
hard-budget gate 阻止；这应作为实验 outcome，而不是静默排除。

## 当前主要差距

### G1. 仍是 SDK Core benchmark

benchmark 手工构造 `AgentRunInfo`，未完整经过 backend service、数据库、tenant 权限和生产
conversation manager。P/C 可以评价 SDK 策略，不代表完整平台端到端结果。

### G2. dataset item 默认 `history=[]`

现有 GAIA 主要测试单次 run 内 ActionStep 增长，不能充分覆盖：

- 多轮 conversation turns；
- history checkpoint 持久化；
- summary 跨轮加载；
- tenant/agent/request policy layering；
- conversation-level ContextManager 复用。

需要另建多轮 conversation benchmark。

### G3. ContextEvidence 已生成，但报告层仍可增强

当前 runtime evidence 和 OTel event 已存在，comparison 会验证证据合同和 manifest 配置。
后续仍应把每 item 的 overflow、compression trigger、raw/final tokens 直接汇总到 comparison
JSON/Markdown，避免只能从 trace 二次导出。

### G4. Budget 必须显式控制

正式实验应显式设置并记录：

- model context window；
- soft input budget；
- hard input budget；
- output reserve；
- token threshold。

仅设置 `token_threshold` 会使用 SDK 派生默认值，虽然 manifest 会写 resolved 值，但不如
显式实验参数清晰。

### G5. Token estimate 仍不是 provider tokenizer ground truth

ContextManager 当前主要使用字符比估算。需要结合 provider reported input tokens 分析，
不能把估算值当成精确 token 账单。

### G6. L/P/C 是跨版本比较

L 与 P/C 的 prompt assembly、ContextItems、budget gate 和 evidence schema 均不同。必须：

- 独立 worktree/虚拟环境；
- 固定依赖和 commit；
- 相同 dataset item IDs；
- 分开保存 manifest；
- 报告中明确跨版本 confounders。

不得把 L/C 差异直接归因于 compression。

## 实验验收

一次有效 P/C comparison 至少满足：

- 两组同一代码 snapshot；
- item IDs 完全一致；
- P/C processing mode 正确；
- runtime 均为 `context_items`；
- resolved hard budget 非空；
- policy fingerprint 非空；
- model/prompt/tool/evaluator 等非目标 manifest 字段一致；
- P/C ContextEvidence 均有 processing mode、policy、budget、token 和 overflow 字段；
- 报告分别展示正确率、完成率、overflow、压缩、token、延迟和 cache。

一次有效 L/P/C 报告还必须注明：

- L commit 为 `32152c3bf7d43c37ff36336080d120284a42046d`；
- P/C 当前 commit；
- L/P/C dependency/environment identity；
- L/P 是架构迁移比较，P/C 是策略比较。
