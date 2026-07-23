# Context processing P/C 对照实验

`run_context_manager_comparison.py` 是 `run_benchmark.py` 的上层编排器。PR #3475
之后 ContextManager 和 ContextItems assembly 始终启用，实验只改变 processing policy：

| 组别 | Runtime | Policy | 测量目标 |
|---|---|---|---|
| P | `context_items` | `passthrough` | 同一 assembly 下不执行自适应压缩 |
| C | `context_items` | `adaptive_compact` | 执行生产自适应压缩 |

因此：

```text
P vs C = adaptive compaction 的增量效果
```

旧版 Legacy 不在本脚本内运行。历史 L 基线固定为
`32152c3bf7d43c37ff36336080d120284a42046d`，应在独立 worktree/环境中运行，
再由离线 L/P/C 报告层合并结果。不能把 L/C 差异直接称为 compression effect。

## 推荐命令

正式实验应显式设置模型、步数、temperature 和压缩阈值：

```bash
backend/.venv/bin/python \
  sdk/benchmark/generic/run_context_manager_comparison.py \
  --dataset gaia-level1-web-search \
  --run-prefix gaia-context-20260723 \
  --repeat 3 \
  --compression-threshold 10000 \
  --runner-args \
    --agent-config path/to/exported-agent.yaml \
    --evaluators gaia_exact_match \
    --model-factory openai \
    --max-steps 20 \
    --temperature 0
```

一次 smoke：

```bash
backend/.venv/bin/python \
  sdk/benchmark/generic/run_context_manager_comparison.py \
  --dataset gaia-level1-web-search \
  --run-prefix gaia-context-smoke-20260723 \
  --repeat 1 \
  --formal-items 1 \
  --compression-threshold 10000 \
  --runner-args \
    --agent-config path/to/exported-agent.yaml \
    --evaluators gaia_exact_match \
    --max-steps 2 \
    --temperature 0
```

## 参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `--dataset` | 必填 | P/C 使用的同一 Langfuse dataset |
| `--run-prefix` | 必填 | 本次实验唯一前缀 |
| `--repeat` | `1` | 正式 P/C 重复次数 |
| `--smoke-items` | `1` | smoke 使用 dataset 前 N 项 |
| `--skip-smoke` | 关闭 | 跳过 smoke |
| `--formal-items` | 全部 | 正式阶段限制前 N 项 |
| `--compression-threshold` | `10000` | C 组 token threshold |
| `--seed` | `0` | P/C 执行顺序随机种子 |
| `--required-url NAME=URL` | 无 | 调用模型前做服务可达性预检 |
| `--python` | 当前解释器 | 子进程 Python |
| `--runner-args` | 无 | 其后参数原样传给 `run_benchmark.py` |

comparison runner 控制以下参数，不能通过 `--runner-args` 覆盖：

```text
--dataset
--run-name
--item-limit
--experiment-time
--context-processing-mode
--enable-context-manager
--disable-context-manager
--token-threshold
--soft-input-budget
--hard-input-budget
```

## Manifest 与公平性校验

每组由 `run_benchmark.py` 写入 schema v2 resolved manifest。P/C 完成后自动检查：

- dataset、item IDs、代码 commit；
- 模型、endpoint、model factory、temperature、max steps；
- tool schema hash、system prompt hash、evaluator；
- 两组均使用 `context_runtime=context_items`；
- P 为 `passthrough`，C 为 `adaptive_compact`；
- resolved hard input budget 存在；
- context policy fingerprint 存在；
- 除 processing policy 外的受控变量一致。

运行时 `ContextEvidence` 合同至少包含：

```text
processing_mode
policy_fingerprint
soft_budget / hard_budget
raw_token_estimate / final_token_estimate
history_compression_triggered
over_hard_budget / compact_exhausted
selected_item_ids / selected_item_types
```

ContextEvidence 仍通过 `agent.final_context` OpenTelemetry event 输出，可使用
`context_evidence_diff.py` 按 item、step、purpose 比较首次输入差异。

## 报告解释

P/C 每轮使用相同 item IDs，并生成二元 outcome matrix：

| 模式 | 含义 |
|---|---|
| `PP` | P、C 都通过 |
| `PF` | P 通过，C 失败，重点检查压缩信息损失 |
| `FP` | P 失败，C 通过，重点检查预算溢出或长上下文改善 |
| `FF` | 两组都失败 |

报告同时分开统计 provider prefix cache 与 ContextManager summary cache。

`passthrough` 不是旧 Legacy：它仍经过 ContextItems assembly、预算估算、工具规范化、
stable-prefix 和 hard-budget 检查。P 超过 hard budget 而失败属于新产品策略结果，必须与
答案错误、工具错误分别统计。

## L/P/C 历史实验

推荐目录：

```text
nexent/             当前代码，运行 P/C
nexent-legacy-l/    固定在 32152c3bf7d43c37ff36336080d120284a42046d，运行 L
```

L 使用旧版 `--disable-context-manager`。P/C 使用当前代码。三个环境分别保存：

- code commit 和 source snapshot；
- Python/dependency lock；
- model、provider、tool schema；
- dataset item IDs；
- prompt/config hashes。

比较口径：

```text
L vs P = #3475 上下文架构迁移的整体影响
P vs C = 新架构内部 adaptive compaction 的增量效果
L vs C = 产品版本整体效果，不作单变量归因
```
