# Context 压缩效果评估框架设计

## 概述

为 Nexent 的 `ContextManager` 压缩功能构建一套全面的评估测试集。该测试集需要同时验证正确性（回归测试）和量化效果（token 节省、缓存命中率、性能指标）。

## 目标

- **正确性验证**：确保压缩逻辑不会崩溃，缓存机制按预期工作，fallback 策略在适当时候触发。
- **效果量化**：测量 token 节省量、压缩触发频率、缓存命中率、增量压缩占比、跨 run 缓存复用率，以及各阶段耗时分解。
- **可扩展性**：用户可以通过提供对话历史文件和 query 列表来注册新的评估场景。
- **报告输出**：控制台实时摘要，以及结构化的 JSON/CSV 报告供后续分析。

## 非目标

- 不修改 `agent_context.py` 核心逻辑。
- 本轮不涉及通过 LLM-as-judge 评估答案质量。
- 不替换现有的 pytest 测试，而是作为补充。

## 架构

### 目录结构

```
sdk/scripts/
├── evaluation/                    # 新增：效果评估框架
│   ├── __init__.py
│   ├── scenarios.py              # 场景定义与注册（可插拔）
│   ├── metrics.py                # 指标收集与计算
│   ├── reporters.py              # 报告生成（控制台 + JSON/CSV）
│   ├── runner.py                 # 场景遍历与对比执行
│   ├── main.py                   # CLI 入口
│   └── fixtures/                 # 内置测试数据（可选）
│       └── default_scenarios.yaml
├── tests/                         # 回归测试（保留/迁移现有）
│   └── test_context_manager.py   # 精简后的正确性测试
├── test_utils.py                  # 复用现有工具
├── test_context_manager.py        # 现有文件（可选迁移目标）
└── history.md / small_history.md  # 现有历史文件
```

### 设计原则

- **物理隔离**：评估层与回归层相互隔离，无交叉依赖。
- **非侵入式**：评估框架仅调用 `agent_context.py` 的公开接口。
- **可插拔场景**：场景定义与执行逻辑解耦。新场景可通过 YAML 或 Python 注册，无需修改 runner 代码。

## 组件

### 1. scenarios.py

定义 `Scenario` 数据类和 `ScenarioRegistry`。

```python
@dataclass
class Scenario:
    name: str                          # 唯一标识
    description: str                   # 场景说明
    history_file: Optional[str]        # 历史文件路径（相对于 sdk/scripts）
    base_history: List[AgentHistory]   # 或直接传入历史
    queries: List[str]                 # 本次要执行的 queries
    cm_config: ContextManagerConfig    # 该场景的压缩配置
    max_steps: int = 10                # Agent 最大步数
    expected_tags: List[str] = None    # 标签（用于筛选）

class ScenarioRegistry:
    def register(self, scenario: Scenario) -> None: ...
    def get(self, name: str) -> Scenario: ...
    def list_all(self) -> List[Scenario]: ...
    def filter_by_tag(self, tag: str) -> List[Scenario]: ...
```

用户可以通过代码或 `fixtures/default_scenarios.yaml` 注册场景：

```yaml
scenarios:
  - name: long_history_math
    history_file: "./history_long_math.md"
    queries:
      - "继续计算..."
      - "验证结果..."
    cm_config:
      enabled: true
      token_threshold: 8000
      keep_recent_pairs: 2
    tags: ["previous_run", "math"]
```

### 2. metrics.py

收集每次运行的指标，并提供对比工具。

**每次运行收集的指标：**

| 类别 | 指标 | 说明 |
|------|------|------|
| Token | `raw_tokens` | 未压缩时的 token 数 |
| Token | `compressed_tokens` | 压缩后的 token 数 |
| Token | `savings_ratio` | `(raw - compressed) / raw` |
| 压缩事件 | `compression_triggers` | 压缩触发次数 |
| 压缩事件 | `cache_hits` | 缓存命中次数 |
| 压缩事件 | `incremental_calls` | 增量压缩次数 |
| 压缩事件 | `fallback_triggers` | Fallback（L3 truncation）次数 |
| 性能 | `compression_time_ms` | 压缩逻辑耗时 |
| 性能 | `inference_time_ms` | 模型推理耗时 |
| 性能 | `total_time_ms` | 场景总执行耗时 |
| 跨 Run | `cross_run_cache_reuse` | 多轮场景下的缓存复用率 |
| 质量 | `step_count` | Agent 执行步数 |
| 质量 | `final_answer_length` | 最终答案长度 |
| 质量 | `error_count` | 遇到的错误次数 |

```python
@dataclass
class RunMetrics:
    scenario_name: str
    config_label: str              # "baseline" 或 "opt"
    raw_tokens: int
    compressed_tokens: int
    compression_triggers: int
    cache_hits: int
    incremental_calls: int
    fallback_triggers: int
    compression_time_ms: float
    inference_time_ms: float
    total_time_ms: float
    step_count: int
    final_answer_length: int
    error_count: int
    cross_run_cache_reuse: float

class MetricsCollector:
    def start_run(self, scenario: Scenario, config_label: str) -> None: ...
    def record_compression(self, stats: dict) -> None: ...
    def record_timing(self, phase: str, elapsed_ms: float) -> None: ...
    def finish_run(self, result: AgentRunResult) -> RunMetrics: ...
```

### 3. reporters.py

生成人类可读和机器可读的报告。

```python
class ConsoleReporter:
    def print_summary(self, all_metrics: List[RunMetrics]) -> None: ...
    def print_comparison(self, baseline: RunMetrics, opt: RunMetrics) -> None: ...

class JsonReporter:
    def save(self, all_metrics: List[RunMetrics], path: str) -> None: ...

class CsvReporter:
    def save(self, all_metrics: List[RunMetrics], path: str) -> None: ...
```

**控制台输出示例：**

```
┌─────────────────────┬──────────┬──────────┬──────────┬─────────┬──────────┐
│ Scenario            │ Config   │ Raw Tok  │ Comp Tok │ Savings │ Triggers │
├─────────────────────┼──────────┼──────────┼──────────┼─────────┼──────────┤
│ p1_first_comp       │ baseline │    12450 │    12450 │   0.0%  │        0 │
│ p1_first_comp       │ opt      │    12450 │     4200 │  66.3%  │        2 │
│ p2_inc_comp         │ baseline │     8900 │     8900 │   0.0%  │        0 │
│ p2_inc_comp         │ opt      │     8900 │     3100 │  65.2%  │        3 │
└─────────────────────┴──────────┴──────────┴──────────┴─────────┴──────────┘
```

### 4. runner.py

编排场景执行和 baseline-vs-opt 对比。

```python
class EvaluationRunner:
    def __init__(self, registry: ScenarioRegistry, collector: MetricsCollector) -> None: ...

    async def run_scenario(self, scenario: Scenario, config_label: str) -> RunMetrics:
        """运行单个场景（baseline 或 opt）。"""
        ...

    async def run_comparison(self, scenario: Scenario) -> Tuple[RunMetrics, RunMetrics]:
        """对一个场景分别运行 baseline 和 opt。"""
        ...

    async def run_all(self, tag_filter: Optional[str] = None) -> List[RunMetrics]:
        """遍历所有场景，逐个执行 baseline vs opt 对比。"""
        ...
```

**每个场景的执行逻辑：**
1. 构造 `AgentRunInfo` 并设置 `cm_config(enabled=False)` → baseline 运行
2. 构造 `AgentRunInfo` 并设置 `cm_config(enabled=True)` → opt 运行
3. 两次运行都使用 `test_utils.py` 中的 `run_multi_turn()` 机制
4. 从 `ContextManager.get_all_compression_stats()` 提取压缩事件指标
5. 从 `AgentRunResult` 提取质量指标
6. 使用 `time.perf_counter()` 测量各阶段耗时
7. 返回两次运行的 `RunMetrics`

## 数据流

```
场景 YAML / Python 代码  →  场景注册表  →  评估执行器
                                                  │
                        ┌─────────────────────────┼──────────────────────┐
                        │                         │                      │
                        ▼                         ▼                      ▼
                Baseline 运行              Opt 运行                 指标收集器
                (禁用压缩)                 (启用压缩)                       │
                        │                         │                      │
                        ▼                         ▼                      ▼
                Agent 运行结果           Agent 运行结果 +              运行指标
                                         压缩统计信息                 (每次运行)
                                                                         │
                                                                         ▼
                                                                  报告生成器
                                                                  (控制台/
                                                                   JSON/CSV)
```

## 错误处理

| 场景 | 处理方式 |
|------|----------|
| 历史文件不存在 | `ScenarioRegistry.register()` 在注册时抛出 `ValueError` |
| Agent 运行异常 | 捕获异常，`error_count += 1`，`final_answer` 标记为失败，继续执行下一个场景 |
| Summary 生成失败 | 依赖 `agent_context.py` 内部 L3 fallback；记录 `fallback_triggers += 1` |
| 单场景超时 | 可配置 `per_scenario_timeout`；超时后记录并跳过 |
| Baseline/opt 对比异常 | 独立记录，不阻塞报告生成 |

## 使用方式

### 注册新场景（用户提供数据）

```python
# 在 evaluation/scenarios.py 或用户文件中
registry.register(Scenario(
    name="your_new_scene",
    description="场景描述",
    history_file="./your_history.md",
    queries=["问题1", "问题2"],
    cm_config=ContextManagerConfig(enabled=True, token_threshold=5000),
    tags=["previous_run", "your_tag"]
))
```

### 运行评估

```bash
# 运行所有场景
uv run sdk/scripts/evaluation/main.py --all

# 按标签运行场景
uv run sdk/scripts/evaluation/main.py --tag previous_run

# 输出报告
uv run sdk/scripts/evaluation/main.py --all --json report.json --csv report.csv

# 运行单个场景
uv run sdk/scripts/evaluation/main.py --scenario p1_first_comp
```

### 运行回归测试（独立执行）

```bash
uv run pytest sdk/scripts/tests/test_context_manager.py -v
```

## 现有测试迁移

现有的 `test_context_manager.py` 将精简为仅保留硬断言的正确性测试：
- 缓存验证逻辑（`_is_prev_cache_valid`、`_is_curr_cache_valid`）
- 预算裁剪（`_trim_pairs_to_budget`、`_trim_actions_to_budget`）
- Fallback 行为验证
- 跨 run 缓存失效验证

效果测试（token 节省量测量、缓存命中率验证）迁移到新的评估框架中。

## 未来扩展

- 添加 LLM-as-judge 质量评估（对比 baseline 和 opt 答案的语义一致性）
- 支持并行场景执行
- 添加历史趋势追踪（对比不同版本框架的指标变化）
- Web 报告可视化仪表盘
