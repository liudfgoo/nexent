# Benchmark 实验框架待完善项清单

> **2026-07-23 PR #3475 迁移说明**
>
> ContextManager 和 ContextItems assembly 现在是唯一生产路径，Legacy runtime 已删除。
> 当前主实验从旧 A/B/C 改为同代码 P/C：
>
> ```text
> P = context_items + passthrough
> C = context_items + adaptive_compact
> ```
>
> 历史 L 基线固定为 `32152c3bf7d43c37ff36336080d120284a42046d`，只能在独立
> worktree/依赖环境中运行。L/P 测量架构迁移，P/C 测量 adaptive compaction。
> 本文后续保留的 Legacy/A/B/C 内容属于迁移前审计历史，不再作为当前实现规范。

## 0. 当前迁移状态

| 项目 | 状态 | 当前口径 |
|---|---|---|
| 上游合并 | 已完成 | 当前分支合并包含 `59fc453c6` 的 `upstream/develop` |
| 单组 runner | 已迁移 | `--context-processing-mode passthrough/adaptive_compact` |
| Manifest | 已迁移 | schema v2；runtime、policy fingerprint、resolved hard budget |
| Comparison | 已迁移 | P/C 配对、smoke、repeat、交错顺序、manifest parity |
| ContextEvidence | 已迁移 | policy、budget、raw/final tokens、overflow、item IDs/types |
| 历史 L | 已固定 | `32152c3bf7d43c37ff36336080d120284a42046d` |
| Conversation benchmark | 待处理 | 当前 GAIA item 仍主要是 `history=[]` |

---

## 以下为迁移前审计历史

> 初版基于 2026-07-14 对 GAIA Level 1 上下文管理器实验的 readiness 审计；本版结合
> Managed/Legacy ContextRuntime、ContextManager 压缩策略和 Generic Benchmark
> 生命周期的代码分析更新。
>
> 本文档面向后续开发人员，逐项说明当前缺失内容、实现建议和验收标准。

## 1. 审计结论

当前 Generic Benchmark 已能运行 ContextManager 开/关实验，但：

```text
--enable-context-manager
```

和：

```text
--disable-context-manager
```

比较的并不只是“压缩”和“不压缩”，而是两套上下文运行路径：

```text
enable
    → ManagedContextRuntime
    → 组件化上下文组装
    → 超过预算后才进行 LLM 摘要压缩

disable
    → LegacyContextRuntime
    → 传统 system prompt + memory 组装
    → 不进行预算感知的 LLM 摘要压缩
```

即使开启 ContextManager 后 `compression_calls=0`，Managed 与 Legacy 路径仍可能在以下方面存在差异：

- system prompt 和 context component 组装；
- stable/dynamic message 划分；
- 工具 schema 的规范化和顺序；
- final-answer 上下文组装；
- observation 截断策略；
- token 估算和观测指标。

因此，不能将“CM 开启组和关闭组的结果差异”直接全部归因于压缩。

同时，当前 benchmark 每个 dataset item 都使用独立 Agent、`history=[]` 和独立
ContextManager，主要覆盖单次 run 内 current action steps 的压缩，并未充分覆盖生产环境中的
conversation-level ContextManager 复用、previous history 压缩和跨轮摘要缓存。

后续 readiness 建设应围绕三个目标：

1. 分离 Runtime/Assembly 效应与真实 Compression 效应；
2. 提高 benchmark 与生产 ContextManager 生命周期的一致性；
3. 让每个实验结论具备可复现、可配对、可归因的 trace 证据。

---

## P1 — 直接影响实验结论可信度

### P1-1. 缺少 Legacy / Managed-No-Compression / Managed-Compression 三组标准对照

**现状**：

`run_benchmark.py` 支持 `--enable-context-manager` 和 `--disable-context-manager`，但没有标准化对照流程。
过去计划只运行 baseline + CM 两组，这无法区分 Managed runtime 本身与真实摘要压缩带来的影响。

**需要实现**：

新增 `run_context_manager_comparison.py` 或等价脚本，一次执行以下三组：

| 组别 | 参数 | 测量目标 |
|---|---|---|
| A：Legacy | `--disable-context-manager` | 传统上下文基线 |
| B：Managed No Compression | `--enable-context-manager --token-threshold 1000000` | Managed runtime、组件组装和工具规范化的影响 |
| C：Managed Compression | `--enable-context-manager --token-threshold 10000` | 正常 ContextManager 总体效果 |

标准比较：

```text
A vs B：Managed/Legacy runtime 与上下文组装差异
B vs C：真实 LLM 摘要压缩影响
A vs C：ContextManager 整体产品效果
```

脚本应：

- 固定模型、temperature、max_steps、tools、prompts、dataset 和 evaluator；
- 自动生成配对 run name；
- 保存 resolved config 和代码 commit；
- 输出三组配对摘要；
- 禁止覆盖已有 run；
- 支持先 smoke test，再正式重复运行。

运行前自动校验：

- Langfuse 连接；
- dataset 非空且 item ID 一致；
- 工具依赖服务可达；
- 三组 Agent prompt、tools 和模型配置除目标变量外保持一致。

**验收标准**：

- 一条命令完成三组运行；
- 每组 dataset item 一一配对；
- 报告分别给出 A/B、B/C 和 A/C 差异；
- 不再把 A/C 差异直接命名为 compression effect。

---

### P1-2. 缺少完整的 Resolved Experiment Manifest

**现状**：

当前 trace 记录了部分模型和 Agent 信息，但不足以复现 ContextManager 的最终生效配置。
CLI、YAML 和 SDK 默认值共同决定实际行为，SDK 默认值未来变化后，历史 run 可能无法准确复现。

**需要实现**：

每个 run 保存不可变的 resolved manifest，至少包括：

```yaml
dataset_name: gaia-level1-web-search
dataset_version: ...
dataset_item_ids: [...]
run_name: ...
code_commit: ...

context_runtime: managed
context_manager_enabled: true
token_threshold: 10000
soft_input_budget_tokens: 10000
hard_input_budget_tokens: 11000
keep_recent_steps: 4
keep_recent_pairs: 2
max_observation_length: 0
strategy: full
chars_per_token: 1.5

main_model: ...
summary_model: ...
temperature: ...
max_steps: ...
language: ...

tool_count: 6
tool_schema_hash: ...
system_prompt_hash: ...
agent_config_hash: ...
evaluator_names: [...]
evaluator_version: ...
```

还应记录：

- 模型 endpoint/provider；
- summary 是否使用主模型；
- context component 类型；
- benchmark lifecycle 模式；
- max concurrency；
- 运行时间和环境标识；
- observation policy。

**验收标准**：

- Langfuse run 或配套 artifact 中存在 resolved manifest；
- manifest 记录最终生效值，而不是只记录 CLI 输入；
- 给定 manifest 和代码 commit 可以重建同等配置。

---

### P1-3. 缺少 FinalContext、压缩摘要和上下文差异证据

**实现进展（2026-07-20）**：

- 已在每次 `step` / `final_answer` 模型调用前记录默认安全的 `agent.final_context` event；
- 已覆盖 message/tool/system/history/final-answer prompt fingerprint、role 结构、selected
  components、stable/dynamic count、stable prefix/change reasons、context overhead、
  pre/post compression token、compression records、summary fingerprint/fallback 和 observation
  truncation；
- 已增加 `context_evidence_diff.py`，按相同 item、step、purpose 报告 A/B/C 首次输入差异；
- 默认 event 不包含消息、tool schema、summary、observation 或 compression details 原文；
- 尚未实现 debug run 的脱敏完整 payload、对象存储 artifact reference 和
  soft/hard budget/overflow evidence，因此本项仍按“基础完成、增强项待补”处理。

**现状**：

当前 trace 可以看到 Agent step 和部分 token/compression 指标，但无法稳定重建每次模型调用真正收到的
`FinalContext`，因此难以证明 A/B/C 首次从哪里发生差异。

**需要实现**：

每次模型调用记录或可关联：

- 最终 messages；
- 最终 tools/schema；
- selected component types；
- stable/dynamic message count；
- stable prefix fingerprint；
- prefix change reasons；
- context overhead tokens；
- pre/post compression token；
- compression records；
- current/previous summary；
- summary 是否 fallback；
- observation 是否截断；
- purpose：`step` 或 `final_answer`。

考虑敏感数据和 trace 体积：

- 默认记录 hash、结构、token 和 artifact reference；
- debug run 可记录脱敏后的完整 payload；
- 大型 observation/summary 可保存到对象存储，在 trace 中记录引用；
- API key、认证信息和个人数据必须脱敏。

新增配对 diff 工具，按相同 item 和 step 比较：

```text
system message diff
tool schema/order diff
history message diff
summary replacement diff
observation truncation diff
final-answer prompt diff
```

**验收标准**：

- 任意失败 item 可以定位 A/B/C 首次输入差异；
- 可以证明某条事实是在工具输出、上下文组装还是摘要阶段丢失；
- 完整 payload 观测不会泄露密钥。

---

### P1-4. Benchmark 与生产 ContextManager 生命周期不一致

**现状**：

Generic Benchmark 每个 dataset item：

- 使用 `history=[]`；
- 单独创建 Agent；
- 单独创建 ContextManager；
- 运行结束后丢弃 ContextManager；
- 不跨 item 复用摘要缓存。

生产代码支持按 conversation ID 复用 ContextManager。因此当前 benchmark 主要验证单次 run 内
current action steps 的压缩，不能充分验证：

- previous conversation history 压缩；
- `keep_recent_pairs`；
- conversation-level 增量摘要；
- 跨轮 summary cache；
- run 切换时 current summary cache 清理；
- 长对话 stale state；
- conversation 清理。

**需要实现**：

提供两种 lifecycle 模式：

1. `isolated-item`：当前单题隔离模式；
2. `conversation-session`：同一 session 中运行多个 turn，并复用 ContextManager。

conversation-session dataset 应能表达：

- session ID；
- turn index；
- 每轮 user input；
- 每轮预期 milestone 或 final state；
- 哪些事实必须跨轮保留；
- session 结束后的清理要求。

**验收标准**：

- 可在 benchmark 中复现 production conversation-level ContextManager；
- trace 区分 previous/current compression；
- 可验证 `keep_recent_pairs` 和跨轮 cache；
- run manifest 明确标记 lifecycle 模式；
- isolated-item 与 conversation-session 结果不可混为同一指标。

---

### P1-6. Token Saving 指标口径不完整

**现状**：

当前 per-trace 有部分 compression token 指标，但缺少配对的全程聚合；同时本地估算 token 与 provider
API usage 容易被混用。

**需要实现**：

分别报告两个口径。

#### 逻辑上下文效率

```text
estimated_total_tokens
= estimated_main_input
+ main_output
+ estimated_compression_input
+ compression_output
```

#### API/计费效率

```text
api_total_tokens
= api_main_input
+ api_main_output
+ api_compression_input
+ api_compression_output
```

派生指标：

```text
estimated_token_saving
api_token_saving
estimated_context_reduction
compression_overhead_tokens
net_token_saving
```

要求：

- estimated 与 API usage 不混合；
- 区分 main model 与 summary model token；
- 明确 provider usage 是否包含 cached tokens；
- token saving 必须基于配对 item；
- 同时给出成功任务成本，避免以准确率下降换取 token 节省。

推荐指标：

```text
tokens_per_success
cost_per_success
accuracy_adjusted_token_saving
```

**验收标准**：

- 输出 estimated 和 API 两套 token 指标；
- 每个指标有清晰统计口径；
- 可分别比较 A/B、B/C、A/C；
- 不能用本地估算与 API usage 的差值推断 provider cache。

---

### P1-7. 缺少分解后的 Latency 指标

**现状**：

`run_benchmark.py` 没有完整记录 per-item wall-clock，也无法解释 ContextManager 额外延迟来自哪里。

**需要实现**：

至少记录：

- `total_latency`；
- `main_model_latency`；
- `compression_model_latency`；
- `tool_latency`；
- `final_answer_latency`；
- `time_to_first_token`；
- `compression_call_count`；
- `cache_hit_count`。

实验结束输出：

- avg；
- p50；
- p95；
- paired latency delta；
- 每个成功任务平均耗时。

为降低 provider 负载时间漂移的影响，重复实验应随机或交错执行 A/B/C，而不是先跑完全部 A 再跑 B/C。

**验收标准**：

- 每个 trace 有分解 latency；
- 输出 A/B、B/C、A/C 配对差异；
- 单次绝对 latency 不作为唯一结论。

---

### P1-8. Peak Context 指标需按 FinalContext 和配对 item 计算

**现状**：

当前每步存在 `estimated_context_tokens`，但没有：

- per-trace peak；
- tool schema 和动态组件口径确认；
- 配对 item 统计；
- A/B/C 分解。

**需要实现**：

在每次实际模型调用的 FinalContext 上计算：

```text
peak_context_tokens
peak_context_step
peak_pre_compression_tokens
peak_post_compression_tokens
```

逐 item 计算：

```text
peak_reduction_i = 1 - peak_target_i / peak_reference_i
```

聚合报告：

- mean；
- median；
- p25/p75；
- p95；
- overflow count；
- peak 出现步骤分布。

**验收标准**：

- 每个 trace 有真实 FinalContext peak；
- A/B 和 B/C 分开报告；
- 不直接用两个 run 的总量比代替 paired reduction。

---

### P1-9. 缺少基于 First Error 的标准失败归因

**现状**：

当前只有 evaluator 的 0/1 结果。旧方案拟使用以下简单规则：

```text
CM 失败 + baseline 成功 → compression_loss
有答案但答案错误       → model_reasoning
```

这些规则无法区分 runtime 差异、随机性、检索、工具、感知和真实上下文损失。

**需要实现**：

采用简化十类归因体系：

```text
DATA
PERCEPTION
RETRIEVAL
MODEL
TOOL_USE
TOOL
CONTEXT
SCAFFOLD
EVALUATION
INFRA
```

每条失败 trace 至少记录：

```text
first_error_step
first_error_phase
primary_category
primary_code/summary
primary_component
primary_evidence
contributing_categories
detection_gap
attribution_confidence
counterfactual_test
recommended_fix
```

自动规则只产生：

```text
candidate_failure_category
```

不能直接产生已确认根因。

确认 `CONTEXT_COMPRESSION_LOSS` 至少需要：

1. A/B/C 配对运行；
2. B 成功、C 失败，或 B/C 在关键 milestone 上发生稳定差异；
3. 定位首次不同的 step；
4. 证明正确事实在 C 的摘要或组装阶段丢失/失真；
5. 通过保护事实、提高阈值或关闭压缩恢复成功。

失败归因应由独立 trace analyzer 完成，不应放入 `gaia_exact_match.py`。Evaluator 只负责答案正确性。

参考：

- `agent-benchmark-attribution-full.md`
- `agent-benchmark-attribution-simple.md`

**验收标准**：

- 每个失败 trace 有 First Error 和证据；
- 主因、促成因素、检测缺口分开；
- 支持人工复核和反事实验证；
- 汇总报告不使用“有答案但错误=模型推理”作为兜底。

---

### P1-10. 缺少 Hard Budget、压缩失败和 Provider Overflow 观测

**现状**：

默认 hard budget 为 `token_threshold * 1.1`。压缩后仍超过 hard budget 时，当前代码只记录 warning，
不会强制删除更多上下文或阻止模型调用。

**需要实现**：

每一步记录：

```text
soft_budget_tokens
hard_budget_tokens
history_budget_tokens
context_overhead_tokens
pre_compression_tokens
post_compression_tokens
soft_budget_exceeded
hard_budget_exceeded
compression_attempted
compression_failed
fallback_compaction_used
provider_context_overflow
```

区分：

- Managed 已压缩但仍超限；
- 摘要模型失败；
- fallback compaction；
- provider 拒绝请求；
- Legacy 原始上下文超限。

**验收标准**：

- 所有 overflow/预算违规可在 trace 和 run summary 中查询；
- hard budget warning 不再只存在于本地日志；
- 可以统计 overflow avoidance rate。

---

### P1-11. `code_commit` 无法代表实验实际源码快照

**现状**：

Resolved manifest 当前使用 `git rev-parse HEAD` 记录 `code_commit`，A/B/C 结束后要求该字段完全一致。
这可以发现分支切换或新 commit，但无法区分以下两种情况：

1. 实验期间实际源码内容发生变化；
2. 工作区已有修改保持不变，只是在实验期间将相同内容提交到 Git，导致 `HEAD` 改变。

2026-07-20 的 reasoning A/B/C 实验中，A 在 fingerprint 修复提交前创建 manifest，B/C 在提交后创建
manifest。该 commit 只提交了已在工作区运行的修复，源码内容很可能未变化，但最终 parity 仍因
`code_commit` 不同而失败。另一方面，当前 manifest 也没有记录 dirty worktree，因此仅凭 commit 无法证明
实际执行内容可复现。

**需要实现**：

comparison 启动时通过独立临时 Git index 计算一次 tracked 工作区实际内容的 tree hash，且不修改真实
index、工作区或 commit：

```text
temporary index
    -> git read-tree HEAD
    -> git add -u
    -> git write-tree
    -> source_tree_hash
```

在每组启动前和结束后重新计算并与初始值比较；实际内容一旦变化应立即终止，避免全部实验完成后才失败。
每个 manifest 至少增加：

```yaml
code_commit: ...
source_tree_hash: ...
tracked_worktree_dirty: true
relevant_untracked_files: [...]
source_snapshot_method: temporary_index_write_tree_v1
```

最终 parity 以 `source_tree_hash` 一致为硬条件。`code_commit` 不同时保留 provenance warning，但如果实际
tree hash 一致，不应单独判定 A/B/C 无效。

未跟踪文件不会进入 Git tree hash。应单独检查可能参与执行的未跟踪源码和配置文件；对于
`sdk/**/*.py`、benchmark evaluator/config 等相关路径，可选择拒绝运行，或记录稳定内容 hash。运行期间产生的
artifact、日志和报告目录必须排除，避免观测输出本身改变源码快照。

**成本与边界**：

- 不复制仓库、不创建 commit，不修改真实 Git index；
- 单次主要成本是扫描 tracked 文件和哈希实际变化，预计亚秒到数秒；
- 每组前后检查的总成本相对长时间 benchmark 可忽略；
- dataset、环境变量、模型 endpoint 和 resolved config 仍由 manifest 独立记录，不能由源码 tree hash 代替。

**验收标准**：

- 只提交未变化的工作区内容时，A/B/C 不因 `code_commit` 变化而误判失败；
- 任一 tracked 源码实际变化时，在下一检查点立即终止实验；
- manifest 同时保留 commit provenance、实际源码 tree hash、dirty 状态和相关 untracked 风险；
- 临时快照计算不改变用户工作区、暂存区或运行产物。

---

## P2 — 影响实验完整性和可操作性

### P2-1. 缺少同 Runtime 的 Recent-N Baseline

**现状**：

当前不支持只保留最近 N 步、不做 LLM 摘要的简单截断基线。

**设计原则**：

Recent-N 的目标是隔离：

```text
LLM 摘要压缩 vs 简单截断
```

因此应保持相同 Managed runtime、system prompt assembly、tool ordering、final-answer assembly 和 metrics，
只替换历史缩减策略。

**需要实现**：

在 Managed ContextRuntime 中提供明确策略：

```text
no_compression
recent_only
llm_summary
```

不建议只在 `task_adapter.py` 中预裁剪 history，因为这会同时改变其他上下文行为。

当前相关文件：

- `sdk/nexent/core/agents/summary_config.py`
- `sdk/nexent/core/agents/agent_context/manager.py`
- `sdk/nexent/core/context_runtime/managed/runtime.py`
- `sdk/nexent/core/context_runtime/legacy/runtime.py`
- `sdk/benchmark/generic/run_benchmark.py`

**验收标准**：

- 可运行 `recent_only`；
- recent-only 与 llm-summary 使用相同 Managed assembly；
- trace 明确记录策略；
- 能比较相同预算下的准确率、token、latency 和信息保留。

---

### P2-2. 缺少重复运行、配对统计和置信区间

**现状**：

当前每个配置只运行一次，无法区分稳定差异和 Agent/搜索随机性。

**需要实现**：

新增 `--repeat N`：

- 每次使用独立 run name；
- A/B/C 使用相同 item 顺序或受控随机顺序；
- 保存 repeat index 和 random seed（如适用）；
- 先运行 smoke test，再运行正式重复实验。

报告：

- mean accuracy；
- standard deviation；
- bootstrap 或适用的置信区间；
- paired item difference；
- `pass@k`；
- `pass^k`；
- answer consistency；
- tool path consistency。

对于同一批 item 的 A/B/C 比较，应优先使用配对统计，而不是把三个 run 当作独立样本。

**验收标准**：

- `--repeat 3` 自动生成三轮；
- 可输出配对置信区间和可靠性指标；
- 不只报告单次平均准确率。

---

### P2-3. Smoke Test 需要区分 Managed Path 与真实 Compression

**现状**：

旧方案要求“CM 开启时压缩指标有值”，但 ContextManager 开启且未超过阈值时
`compression_calls=0` 是正常行为。

**需要实现**：

#### Managed Path Smoke

使用高阈值验证：

- `context_manager_enabled=true`；
- runtime 为 Managed；
- selected context components 非空；
- system prompt 未丢失；
- tool schema 正常；
- final context 可调用；
- `compression_calls=0` 允许通过。

#### Compression Smoke

使用专门的长上下文 fixture 或低测试阈值验证：

- 真实 compression call 发生；
- summary 生成；
- recent steps 保留；
- 后续 step 出现 summary cache hit；
- compression token/ratio 完整；
- final answer 路径可使用压缩上下文。

#### Legacy Smoke

验证：

- runtime 为 Legacy；
- compression 指标为 0；
- system prompt、tools 和 memory 正常；
- 100000 字符 observation 限制可被观测。

**验收标准**：

- 三类 smoke 可以独立运行；
- 不依赖随机 GAIA 题恰好触发压缩；
- smoke failure 有明确诊断。

---

### P2-4. CLI ContextManager 参数校验不足

**现状**：

- `--enable-context-manager` 和 `--disable-context-manager` 可同时传入；
- 同时传入时 enable 因 `if/elif` 优先而生效；
- CM 关闭时仍可传 `--token-threshold` 等参数，但这些参数对 Legacy 路径不生效；
- 缺少数值范围校验。

**需要实现**：

- 使用 argparse mutually exclusive group；
- CM 关闭时传入 CM-only 参数给出错误或明确 warning；
- 校验 `token_threshold > 0`；
- 校验 `keep_recent_steps >= 0`；
- 校验 `keep_recent_pairs >= 0`；
- 校验 `max_observation_length >= 0`；
- 启动时打印 `context_runtime` 和全部 resolved CM config。

**验收标准**：

- 冲突参数无法启动；
- 无效数值无法启动；
- ignored 参数不会静默存在。

---

### P2-5. `max_concurrency` 参数未实现

**现状**：

`run_benchmark.py` 接收 `--max-concurrency`，实际仍通过普通 `for` 循环逐项运行。

**需要实现**：

- 使用受控并发；
- 每个 item 保持独立 Agent/ContextManager；
- Langfuse trace/score 写入线程安全；
- 日志按 item 隔离；
- 支持 provider rate-limit/backoff；
- 记录实际并发度；
- 避免外部工具共享可变 session。

短期不实现时，应在 CLI help 标记 `(not implemented)`，而不是接受参数后静默串行。

**验收标准**：

- `--max-concurrency 4` 可观测到最多 4 个并行 item；
- 总耗时显著低于串行，但不要求严格达到 1/4；
- 并发和串行的数据完整性与评分一致；
- 无 context 或 trace 串扰。

---

### P2-6. Provider Prefix Cache 指标缺失且需与 Summary Cache 区分

**实现进展（2026-07-20）**：

- 已复用 provider adapter 对 `cached_tokens` 等明确 usage 字段的解析；
- 已用 DeepSeek 官方 `deepseek-v4-flash` 实测确认响应同时包含
  `prompt_tokens_details.cached_tokens` 和 `prompt_cache_hit_tokens` /
  `prompt_cache_miss_tokens`，并支持两种解析路径；
- 单次主模型调用通过 benchmark token event 输出 provider cache status、metrics source、
  hit、cached/uncached input tokens；
- 单 item、run 和 A/B/C comparison 分别汇总 provider prefix cache；
- ContextManager 本地缓存已在输出层明确命名为 `summary_cache_hits` /
  `summary_cache_types`，不再与 provider cache 混用；
- summary cache 只统计 previous/current summary reuse，`stable_bypass` 不作为 summary hit；
- `unsupported`、`unavailable` 和明确返回指标后的零命中具有不同语义，不使用
  estimated/API token 差值推断 cache hit。

**现状**：

SDK provider adapter 已采集 provider 返回的 cache usage；benchmark 现已完成单次调用到
comparison artifact 的汇总链。仍需用目标 provider 的真实实验验证 Langfuse usage
字段和 `metrics_source`。

**需要实现**：

若 provider 明确返回：

- `cached_tokens`；
- `prompt_tokens_details`；
- 等价 cache metadata；

则计算：

```text
provider_prefix_hit_rate
provider_cached_input_ratio
provider_cached_tokens
```

同时保留：

```text
summary_cache_hits
summary_cache_types
```

如果 provider 不支持，应标记：

```text
unsupported
```

不能使用：

```text
total_input_tokens - total_api_input_tokens
```

推断 cached tokens。该差异还可能来自 tokenizer、估算系数、工具 schema、provider usage 口径等。
可将其单独命名为：

```text
token_accounting_delta
```

**验收标准**：

- provider cache 与 summary cache 使用不同字段；
- 不支持时明确报告，而不是估算为 cache hit；
- 指标文档说明 provider 口径。

---

### P2-7. 缺少自动化 Run Integrity 检查

**现状**：

实验结束主要输出 Total/Passed/Failed，无法自动确认：

- 所有 dataset item 均有 trace；
- trace 均已正确 link 到 run；
- expected output、final answer 和 score 完整；
- 是否存在重复或丢失 item；
- manifest 与实际 trace 配置一致。

当前 A/B/C 配对汇总会校验三组 dataset item ID 集合完全一致，避免缺失 item 被交集逻辑静默忽略；
其余完整性检查尚未实现。

**需要实现**：

每次 run 结束生成 integrity report：

```text
expected_item_count
linked_trace_count
unique_item_count
missing_item_ids
duplicate_item_ids
missing_scores
empty_outputs
trace_errors
config_mismatches
run_complete
```

只有 integrity 通过的 run 才进入准确率和归因统计。

**验收标准**：

- 运行结束自动检查完整性；
- 缺少任一 item 时 run 标记为 incomplete；
- incomplete run 不被默认纳入正式结论。

---

### P2-8. 缺少统一的对照实验汇总报告

**需要实现**：

自动生成包含以下内容的 Markdown/JSON 报告：

- 三组 resolved manifest diff；
- run integrity；
- accuracy/F1；
- paired item outcome matrix；
- token 和 latency；
- peak context；
- compression calls/cache；
- hard-budget/overflow；
- observation truncation；
- first-error failure breakdown；
- attribution confidence；
- 反事实验证结果；
- 成本和成功率权衡；
- 实验限制。

推荐 outcome matrix：

| Legacy A | Managed-No-Compression B | Managed-Compression C | 初步解释 |
|---|---|---|---|
| Pass | Pass | Pass | 三组均稳定 |
| Fail | Pass | Pass | Managed assembly 可能有益 |
| Pass | Fail | Fail | Managed assembly 差异或随机性 |
| Pass | Pass | Fail | compression-loss 候选，需 trace/反事实确认 |
| Fail | Fail | Pass | compression 可能降低干扰 |
| Mixed | Mixed | Mixed | 高随机性，不能单次归因 |

**验收标准**：

- 一条命令读取三个配对 run 并生成报告；
- 自动结论仅使用“候选”措辞；
- 根因确认仍依赖 First Error 证据和反事实实验。

---

### P2-9. Generic Benchmark 未覆盖生产 `RunSkillScriptTool` 装配路径

**现状**：

生产 Agent 在 `backend/agents/create_agent_info.py` 中会自动加入以下 builtin skill tools：

```text
run_skill_script
read_skill_md
read_skill_config
write_skill_file
```

其中 `RunSkillScriptTool` 用于执行已安装 Skill 中预先编写、受控、可复用的 Python/Shell 脚本。它不是任意命令
执行器：脚本路径必须位于指定 Skill 目录内，并可按 agent、tenant 和 version 限制 Skill 可见性。

Generic Benchmark 当前只读取 Agent YAML 的 `tools:`，通过 `build_tools_from_yaml()` 重建
`ToolConfig`，随后直接调用 `make_nexent_task()`。该路径不会经过生产
`backend/agents/create_agent_info.py`，因此：

- 不会自动加入 builtin skill tools；
- 不会按 agent/tenant/version 发现可用 Skill；
- YAML 中的 `skills:` 尚未形成完整的提示注入和执行链路；
- 当前 GAIA 实验没有使用 `RunSkillScriptTool`，历史分数不受生产自动注入行为影响；
- Benchmark 与生产 Agent 的实际工具集合可能不一致。

**对 GAIA 的潜在影响**：

`RunSkillScriptTool` 本身不会提高分数；收益来自与其配套、通用且无答案泄漏的 Skill。例如：

```text
spreadsheet-analysis/
├── SKILL.md
└── scripts/
    ├── inspect_workbook.py
    ├── read_range.py
    ├── find_by_color.py
    ├── aggregate.py
    └── solve_grid.py
```

这类 Skill 可以将当前由 Agent 通过 Terminal 临时编写的 openpyxl、聚合、颜色查找和 BFS 脚本变成经过测试的确定性
能力，减少路径猜测、脚本调试、输出截断和 max-steps 耗尽。

如果只无条件增加 builtin tools、但没有适用 Skill，也可能产生负收益：

- tool schema 增大并占用上下文；
- Tool 选择空间扩大，增加执行方差；
- Agent 尝试不存在的 Skill/脚本并浪费步骤；
- A/B/C 工具集合变化后，ContextManager 归因失效；
- Benchmark 专用脚本可能引入数据集答案泄漏。

**需要实现**：

不要让 Generic Benchmark 静默复制生产自动注入行为。增加显式、可审计的 benchmark 配置，例如：

```yaml
benchmark:
  enable_builtin_skill_tools: true
  local_skills_dir: sdk/benchmark/generic/skills

skills:
  - name: spreadsheet-analysis
```

建议抽取不依赖生产数据库的公共 builder：

```python
build_builtin_skill_tool_configs(
    local_skills_dir,
    agent_id,
    tenant_id,
    version_no,
)
```

由生产和 benchmark 共同调用。Benchmark 侧还需：

1. 将 `skills:` 传入 `make_nexent_task()` 和 `build_agent_run_info()`；
2. 在 system prompt 中只展示本次允许使用的 Skill；
3. 默认只加入 `read_skill_md`、`read_skill_config` 和 `run_skill_script`；
4. Benchmark 默认不加入 `write_skill_file`，避免测评运行修改 Skill；
5. 记录 Tool schema、SKILL.md 和每个脚本的内容 hash；
6. trace 记录实际调用的 skill name、script path、params、exit status、latency 和输出 fingerprint；
7. 对路径穿越、脚本超时、输出大小和子进程资源设置限制；
8. 明确禁止 Skill 包含 dataset item ID、gold answer 或题目专用答案映射。

**推荐对照实验**：

| 组别 | 能力 | 测量目标 |
|---|---|---|
| G0 | Terminal + ReadFile | 当前文件处理基线 |
| G1 | G0 + builtin skill tools，无可用 Skill | 单纯增加 tool schema/选择空间的影响 |
| G2 | G1 + 通用 Spreadsheet Skill | 确定性 Excel 脚本的真实收益 |
| G3 | 原生 `AnalyzeSpreadsheetTool` | 比较 Skill 方案与正式结构化 Tool |

G0/G1/G2/G3 应固定模型、prompt、max steps、ContextManager、dataset 和 evaluator，并至少重复 3 次。只有实际调用
新增 Skill 且 First Error 随之消失的题，才计入 Skill 的直接收益。

**验收标准**：

- Generic Benchmark 可通过显式开关启用/关闭 builtin skill tools；
- 关闭时保持现有工具集合和行为不变；
- 开启时的 tool assembly 与生产共用同一 builder；
- `skills:` 能形成发现、提示、权限和执行闭环；
- manifest 记录 Skill、脚本和 builtin tool hash；
- trace 可以定位每次 Skill/脚本调用；
- A/B/C ContextManager 实验中的 tools/skills/hash 完全一致；
- 无 Skill 的 G1 与基线单独比较，不把 tool schema 影响误记为 Skill 收益；
- 通用 Skill 通过答案泄漏检查；
- Spreadsheet Skill 在正式 25 题上有重复、配对的收益报告。

---

## 3. 当前已就绪项

以下项目已具备基础能力，但部分需要在上述整改中增强。

| 项目 | 状态 | 说明 |
|---|---|---|
| A/B/C 三组标准对照 | 已就绪 | `run_context_manager_comparison.py` 支持 managed/legacy/compression 三组、smoke、repeat、交错顺序、同名保护、manifest parity 和配对 outcome 报告 |
| Resolved Experiment Manifest | 基础就绪 | 每个新 run 生成不可覆盖的 JSON manifest，并在 trace metadata 记录 manifest hash/reference；已覆盖最终 CM 配置、dataset item IDs、commit、模型 endpoint、prompt/tool/agent hash、lifecycle 和 observation policy。尚缺 Langfuse artifact 上传及 run integrity 反向核验 |
| CM CLI 参数校验 | 已就绪 | enable/disable 已互斥；threshold、keep recent 和 observation limit 已做范围校验；Legacy 路径不再静默接受 CM-only 参数 |
| MinIO GAIA 数据 | 已就绪 | 附件已上传并可由 Agent 访问 |
| Docker 工具服务 | 已就绪 | data-process、SSH 等基础服务可用 |
| Langfuse 连接 | 已就绪 | dataset、trace 和 score 链路可用 |
| Agent 配置 YAML | 已就绪 | tools 和 prompts 可导出并重建 |
| YAML tools 重建 | 已就绪 | ToolConfig 和 Analyze* metadata 可注入 |
| CM CLI 基础参数 | 已就绪 | 支持 enable/disable、threshold、keep recent 等，并已完成互斥、范围和 Legacy ignored-argument 校验 |
| Context components | 基础就绪 | Managed 路径可构造组件；尚缺 FinalContext parity/diff |
| Custom prompt CM | 已就绪 | custom prompt 可包装为稳定 component |
| Compression 指标链路 | 基础就绪 | calls/tokens/cache/ratio 已有；尚缺预算、overflow 和完整聚合 |
| Temperature 覆盖链 | 已就绪 | CLI/YAML/default 优先级可用 |
| GAIA 评分器 | 已就绪 | 支持数字、字符串和列表比较 |
| Context runtime 分流 | 已就绪 | Managed 和 Legacy 在 Agent 构造时明确选择 |

---

## 4. 建议整改顺序

### 阶段 1：先保证实验可解释

| 顺序 | 工作项 |
|---:|---|
| 1 | P1-2 Resolved Experiment Manifest |
| 2 | P1-1 A/B/C 三组标准对照 |
| 3 | P1-3 FinalContext/summary/compression evidence |
| 4 | P2-7 Run Integrity |

### 阶段 2：补齐关键指标和归因

| 顺序 | 工作项 |
|---:|---|
| 5 | P1-6 Token 指标双口径 |
| 6 | P1-7 分解 latency |
| 7 | P1-8 Peak Context |
| 8 | P1-10 Budget/overflow |
| 9 | P1-9 First Error 失败归因 |

### 阶段 3：补齐生产覆盖与可靠性

| 顺序 | 工作项 |
|---:|---|
| 10 | P1-4 Conversation lifecycle benchmark |
| 11 | P2-1 Recent-N 同 runtime baseline |
| 12 | P2-2 重复运行和配对置信区间 |
| 13 | P2-3 三类 smoke test |
| 14 | P2-8 自动对照报告 |

### 阶段 4：工程效率和扩展指标

| 顺序 | 工作项 |
|---:|---|
| 15 | P2-4 CLI 校验 |
| 16 | P2-5 并发执行 |
| 17 | P2-6 Provider prefix cache |
| 18 | P2-9 生产等价的 builtin skill tools / RunSkillScriptTool 装配 |

---

## 5. 最终验收目标

ContextManager benchmark 达到正式结论标准时，应满足：

1. 同一数据集可以一键运行 A/B/C 三组；
2. 每组拥有完整 resolved manifest；
3. dataset item 和 trace 一一配对且完整；
4. 可以重建或引用每一步最终模型输入；
5. Managed assembly 与 compression effect 可分离；
6. estimated token 与 API token 口径分离；
7. latency、peak context、budget 和 overflow 可观测；
8. 失败归因基于 First Error，而不是最终答案启发式；
9. compression loss 必须经过 trace 证据和反事实确认；
10. 单题与 conversation-session 生命周期分别评估；
11. 重复运行可以报告可靠性和配对置信区间；
12. 自动报告同时展示准确率、资源效率和失败原因；
13. incomplete run 不进入正式统计；
14. 所有结论可以追溯到代码版本、配置和 trace 证据。
15. 启用 Skill 的实验可追溯 Tool/Skill/脚本 hash，并与生产使用同一 builtin tool builder。

只有满足以上条件，才能对以下问题给出可信回答：

```text
ContextManager 是否提高了准确率？
收益来自 Managed assembly 还是摘要压缩？
节省了多少上下文和实际 API 成本？
增加了多少延迟？
哪些任务因摘要受益，哪些任务因信息损失失败？
其行为是否覆盖生产环境中的长对话和跨轮记忆？
```
