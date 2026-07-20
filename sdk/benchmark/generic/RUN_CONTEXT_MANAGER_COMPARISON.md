# ContextManager A/B/C 对照实验使用说明

`run_context_manager_comparison.py` 是 Generic Benchmark 的上层实验编排器。它调用
`run_benchmark.py`，按相同数据集和 Agent 配置执行以下三组实验：

| 组别 | Runtime | 默认配置 | 测量目标 |
|---|---|---|---|
| A | Legacy | `--disable-context-manager` | 传统上下文基线 |
| B | Managed | `--token-threshold 1000000` | Managed runtime 和 assembly 效应 |
| C | Managed | `--token-threshold 10000` | 正常 ContextManager 整体效果 |

标准比较口径：

```text
A vs B：Managed/Legacy runtime 与上下文组装差异
B vs C：真实 LLM 摘要压缩影响
A vs C：ContextManager 整体产品效果
```

不能把 A/C 差异直接称为 compression effect。

## 与 run_benchmark.py 的关系

两个脚本不是替代关系：

```text
run_context_manager_comparison.py
    ├── 调用 run_benchmark.py 执行 A
    ├── 调用 run_benchmark.py 执行 B
    ├── 调用 run_benchmark.py 执行 C
    └── 校验 manifest 并生成配对报告
```

`run_benchmark.py` 仍负责单个 run 的 Agent 执行、评分、Langfuse trace 和 resolved
manifest。单组实验、dataset 上传和 rescore 仍直接使用 `run_benchmark.py`。

## 推荐命令

正式实验应显式指定 `max-steps`、`temperature` 和两个阈值，避免依赖 YAML、代码默认值或未来默认值：

```bash
backend/.venv/bin/python \
  sdk/benchmark/generic/run_context_manager_comparison.py \
  --dataset gaia-level1-web-search \
  --run-prefix gaia-cm-20260720 \
  --repeat 3 \
  --no-compression-threshold 1000000 \
  --compression-threshold 10000 \
  --required-url data-process=http://localhost:5010/health \
  --runner-args \
    --agent-config path/to/gaia-agent.yaml \
    --evaluators gaia_exact_match \
    --max-steps 20 \
    --temperature 0
```

如果 Agent 不依赖 data-process，或者暂时不需要服务预检，可以省略 `--required-url`。

## Comparison runner 参数

### 必填参数

| 参数 | 说明 |
|---|---|
| `--dataset` | Langfuse dataset 名称。A/B/C 必须使用同一个 dataset |
| `--run-prefix` | 本次对照实验的名称前缀，用于生成所有 run name 和报告文件名 |

### 重复与 Smoke

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `--repeat` | `1` | 正式 A/B/C 实验的重复轮数，必须大于 0 |
| `--smoke-items` | `1` | smoke 阶段每组运行的前 N 个 dataset item |
| `--skip-smoke` | 关闭 | 跳过 smoke，直接运行正式实验 |
| `--formal-items` | 全部 | 正式实验每组只运行前 N 个 item；不传则运行完整 dataset |
| `--seed` | `0` | A/B/C 交错执行顺序使用的随机种子 |

`--repeat 3` 表示正式实验执行三轮：

```text
smoke：A + B + C                         3 个 run
formal repeat 1：A + B + C              3 个 run
formal repeat 2：A + B + C              3 个 run
formal repeat 3：A + B + C              3 个 run
总计                                    12 个 run
```

使用 `--skip-smoke --repeat 3` 时，总计为 9 个 run。

每轮的 A/B/C 顺序会按 `--seed` 受控打乱，以降低 provider 负载随时间漂移造成的偏差。实际执行顺序会写入报告。

### ContextManager 阈值

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `--no-compression-threshold` | `1000000` | B 组阈值；用于尽量避免触发摘要压缩 |
| `--compression-threshold` | `10000` | C 组正常压缩阈值 |

`1000000` 只是实验约定的高阈值，不是“绝对禁止压缩”。如果单次上下文超过该值，B 组仍可能触发压缩。
正式报告应检查 B 组的 `compression_calls`；若大于 0，该轮不能作为严格的 No-Compression 基线。

建议正式命令始终显式写出两个阈值，即使当前使用默认值。

### 外部服务预检

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--required-url NAME=URL` | 无 | 在任何 Agent/LLM 调用前检查指定服务，可以重复传入 |

示例：

```bash
--required-url data-process=http://localhost:5010/health \
--required-url search=http://localhost:5050/health
```

当前实现不会从 Agent YAML 自动发现所有工具依赖。只有显式传入的 URL 会被提前检查。

不传 `--required-url` 时：

- comparison runner 不做工具服务健康检查；
- Agent 仍会按工具配置正常启动；
- 如果实际没有调用依赖该服务的工具，实验可能正常完成；
- 如果调用工具时服务不可达、配置缺失或返回错误，错误会在对应 item 的运行过程中出现；
- 某些工具可能在构造阶段发现配置缺失，另一些工具则延迟到首次调用时失败。

因此，`--required-url` 的作用是 fail fast：在花费模型调用成本、产生部分 run 之前发现已知服务故障。
它不负责把 URL 注入工具；工具实际使用的 endpoint 仍来自 Agent/tool 配置或环境变量。

当前 HTTP 预检规则：

- 无法建立连接或请求超时：失败；
- HTTP 5xx：失败；
- HTTP 2xx、3xx、4xx：视为服务可达。

4xx 被视为“可达”，是因为部分服务的探测地址可能需要认证或不提供专门的 health endpoint。

### 执行环境

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--python` | 当前 Python 解释器 | 调用 `run_benchmark.py` 使用的 Python 路径 |
| `--runner-args ...` | 空 | 其后的参数原样传给每一个 `run_benchmark.py` 子进程，必须放在命令末尾 |

推荐从仓库根目录使用：

```text
--python backend/.venv/bin/python
```

如果本身已经用 `backend/.venv/bin/python` 启动 comparison runner，则无需再次指定。

## runner-args 常用参数

以下参数来自 `run_benchmark.py`，会对 A/B/C 三组保持一致：

| 参数 | 默认值或来源 | 建议 |
|---|---|---|
| `--agent-config` | 无 | 正式实验显式指定 |
| `--evaluators` | `exact_match` | GAIA 使用 `gaia_exact_match` |
| `--max-steps` | CLI → YAML `agent_config.max_steps` → `10` | 正式实验显式指定 |
| `--temperature` | CLI → 手工 YAML `agent_config.temperature` → `0.1` | 正式实验显式指定，确定性实验推荐 `0` |
| `--language` | `en` | 按实验 prompt 显式指定 |
| `--system-prompt-file` | 无 | 使用预渲染 system prompt 时指定 |
| `--max-concurrency` | `1` | 当前 `run_benchmark.py` 尚未真正并发，建议保持 `1` |
| `--keep-recent-steps` | SDK 默认 `4` | 需要控制压缩策略时显式指定 |
| `--keep-recent-pairs` | SDK 默认 `2` | conversation benchmark 时显式指定 |
| `--max-observation-length` | Managed 默认 `0` | 注意 Legacy 仍固定使用 100000 字符限制 |

标准 `export_agent_config.py` 当前不会导出 `temperature`。因此使用标准导出 YAML 且命令行不传
`--temperature` 时，实际值为代码默认 `0.1`。只有手工在 YAML 的 `agent_config` 中增加
`temperature`，才会走 YAML 覆盖逻辑。

以下参数由 comparison runner 控制，不能放进 `--runner-args`：

```text
--dataset
--run-name
--enable-context-manager
--disable-context-manager
--token-threshold
--item-limit
--experiment-time
```

如果传入这些参数，comparison runner 会在启动实验前报错。

## 自动生成的 Run Name

格式：

```text
{run-prefix}-{phase}-r{repeat}-{group}-{label}
```

例如：

```text
gaia-cm-20260720-smoke-r01-a-legacy
gaia-cm-20260720-smoke-r01-b-managed-no-compression
gaia-cm-20260720-smoke-r01-c-managed-compression
gaia-cm-20260720-formal-r01-a-legacy
gaia-cm-20260720-formal-r01-b-managed-no-compression
gaia-cm-20260720-formal-r01-c-managed-compression
```

同名的本地 manifest、Langfuse dataset run 或 comparison report 已存在时，脚本拒绝覆盖。

## 运行前检查

在执行第一个 Agent 之前，脚本检查：

- Langfuse 鉴权；
- dataset 存在且非空；
- dataset item ID 无重复；
- 所有显式声明的 `--required-url` 可达；
- 计划使用的本地 manifest 名称不存在；
- 计划使用的 Langfuse run name 不存在；
- comparison report 文件名不存在。

## 每轮完成后的配置一致性检查

每轮 A/B/C 完成后，脚本读取 resolved manifest，检查以下非目标变量一致：

- dataset 和 item IDs；
- code commit；
- lifecycle mode；
- main/summary model 和 endpoint；
- temperature、max_steps、language、max_concurrency；
- tool count 和 tool schema hash；
- system prompt hash；
- evaluator 配置。

允许变化的目标字段包括：

- `context_runtime`；
- `context_manager_enabled`；
- `context_manager.token_threshold`；
- context component types；
- observation policy。

如果非目标字段不一致，脚本停止并报告 manifest parity failure。

## FinalContext 首次差异

SDK 会在每次实际模型调用前写入 `agent.final_context` span event。默认只记录 hash、
message role 结构、组件、token、summary fallback、compression record 计数信息和 observation
截断标志，不记录原始 prompt、tool schema、summary 或 observation。

将 A/B/C 的 event attributes 导出为 JSON 数组，并为每行补充 `item_id`、`step_number`
和 `purpose` 后，可定位每对 run 的首次输入差异：

```bash
python sdk/benchmark/generic/context_evidence_diff.py \
  --group A=/tmp/a-final-context.json \
  --group B=/tmp/b-final-context.json \
  --group C=/tmp/c-final-context.json
```

输出区分 system message、tool schema/order、history、summary replacement、observation
truncation 和 final-answer prompt；若一侧缺少对应模型调用，则报告
`model_call_presence_diff`。

## 输出文件

单个 run 的 manifest：

```text
sdk/benchmark/generic/artifacts/manifests/{run-name}.manifest.json
```

整个 A/B/C 实验的报告：

```text
sdk/benchmark/generic/artifacts/comparisons/{run-prefix}.comparison.json
sdk/benchmark/generic/artifacts/comparisons/{run-prefix}.comparison.md
```

JSON 包含：

- dataset item IDs；
- 三组阈值；
- 每轮 run name；
- 每轮实际执行顺序；
- manifest parity 结果；
- 每个配对 item 的 A/B/C Pass/Fail；
- outcome matrix；
- A/B/C provider prefix cache hit rate、cached tokens 和 cached input ratio；
- 与 provider cache 分开展示的 ContextManager summary cache hits/types。

Markdown 输出八种结果组合：

```text
PPP PPF PFP PFF FPP FPF FFP FFF
```

其中 `PPF` 表示 A、B 成功而 C 失败，只能作为 compression-loss 候选，不能在缺少
FinalContext diff 和反事实验证时直接确认根因。

Provider prefix cache 只使用 provider 明确返回的 `cached_tokens` 或等价 usage 字段。
报告中的 `N/A` 表示 `unsupported` 或 `unavailable`，不等于已支持但命中率为 0%。

## 常用运行方式

### 只验证完整流程

```bash
backend/.venv/bin/python \
  sdk/benchmark/generic/run_context_manager_comparison.py \
  --dataset gaia-level1-web-search \
  --run-prefix gaia-cm-smoke-20260720 \
  --repeat 1 \
  --formal-items 1 \
  --no-compression-threshold 1000000 \
  --compression-threshold 10000 \
  --runner-args \
    --agent-config path/to/gaia-agent.yaml \
    --evaluators gaia_exact_match \
    --max-steps 20 \
    --temperature 0
```

该命令默认仍会执行 smoke 和 formal，各三组，共 6 个单 item run。如果只需要一轮三组：

```bash
--skip-smoke --repeat 1 --formal-items 1
```

### 三轮正式实验

```bash
backend/.venv/bin/python \
  sdk/benchmark/generic/run_context_manager_comparison.py \
  --dataset gaia-level1-web-search \
  --run-prefix gaia-cm-formal-20260720 \
  --repeat 3 \
  --no-compression-threshold 1000000 \
  --compression-threshold 10000 \
  --runner-args \
    --agent-config path/to/gaia-agent.yaml \
    --evaluators gaia_exact_match \
    --max-steps 20 \
    --temperature 0
```

## 当前限制

- `required-url` 依赖需要人工声明，尚未从 Agent YAML 自动发现；
- B 组依靠高阈值实现 No-Compression，运行后仍需确认 `compression_calls=0`；
- Legacy 与 Managed observation 截断策略仍不同；
- `max_concurrency` 参数尚未实现真正的 item 并发；
- 当前报告输出逐 repeat 配对结果，跨 repeat 的置信区间属于后续 P2-2；
- First Error 和 FinalContext diff 属于后续 P1-3/P1-9。
