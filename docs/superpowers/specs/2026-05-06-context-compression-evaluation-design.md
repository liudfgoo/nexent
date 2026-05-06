# Context Compression Evaluation Framework Design

## Overview

Build a comprehensive evaluation test suite for the Nexent `ContextManager` compression feature. The suite must simultaneously verify correctness (regression testing) and quantify effectiveness (token savings, cache hit rates, performance metrics).

## Goals

- **Correctness validation**: Ensure compression logic does not crash, cache mechanisms work as expected, and fallback strategies trigger appropriately.
- **Effectiveness quantification**: Measure token savings, compression trigger frequency, cache hit rates, incremental compression ratio, cross-run cache reuse, and timing breakdowns.
- **Extensibility**: Users can register new evaluation scenarios by providing conversation history files and query lists.
- **Reporting**: Console real-time summary plus structured JSON/CSV reports for downstream analysis.

## Non-Goals

- Do not modify `agent_context.py` core logic.
- Do not evaluate answer quality via LLM-as-judge (out of scope for this iteration).
- Do not replace existing pytest tests; instead complement them.

## Architecture

### Directory Structure

```
sdk/scripts/
├── evaluation/                    # New: effectiveness evaluation framework
│   ├── __init__.py
│   ├── scenarios.py              # Scenario definition and registry (pluggable)
│   ├── metrics.py                # Metrics collection and computation
│   ├── reporters.py              # Report generation (console + JSON/CSV)
│   ├── runner.py                 # Scenario traversal and comparison execution
│   ├── main.py                   # CLI entry point
│   └── fixtures/                 # Built-in test data (optional)
│       └── default_scenarios.yaml
├── tests/                         # Regression tests (retain/migrate existing)
│   └── test_context_manager.py   # Trimmed correctness tests
├── test_utils.py                  # Reuse existing utilities
├── test_context_manager.py        # Existing file (optional migration target)
└── history.md / small_history.md  # Existing history files
```

### Design Principles

- **Physical separation**: Evaluation layer and regression layer are isolated and have no cross-dependencies.
- **Non-invasive**: The evaluation framework only calls public interfaces of `agent_context.py`.
- **Pluggable scenarios**: Scenario definitions are decoupled from execution logic. New scenarios can be added via YAML or Python registration without modifying runner code.

## Components

### 1. scenarios.py

Defines the `Scenario` dataclass and `ScenarioRegistry`.

```python
@dataclass
class Scenario:
    name: str                          # Unique identifier
    description: str                   # Human-readable description
    history_file: Optional[str]        # Path to history markdown (relative to sdk/scripts)
    base_history: List[AgentHistory]   # Or pass history directly
    queries: List[str]                 # Queries to execute in this run
    cm_config: ContextManagerConfig    # Compression config for this scenario
    max_steps: int = 10                # Agent max steps
    expected_tags: List[str] = None    # Tags for filtering

class ScenarioRegistry:
    def register(self, scenario: Scenario) -> None: ...
    def get(self, name: str) -> Scenario: ...
    def list_all(self) -> List[Scenario]: ...
    def filter_by_tag(self, tag: str) -> List[Scenario]: ...
```

Users can register scenarios programmatically or via `fixtures/default_scenarios.yaml`:

```yaml
scenarios:
  - name: long_history_math
    history_file: "./history_long_math.md"
    queries:
      - "Continue the calculation..."
      - "Verify the result..."
    cm_config:
      enabled: true
      token_threshold: 8000
      keep_recent_pairs: 2
    tags: ["previous_run", "math"]
```

### 2. metrics.py

Collects per-run metrics and provides comparison utilities.

**Metrics collected per run:**

| Category | Metric | Description |
|----------|--------|-------------|
| Token | `raw_tokens` | Token count without compression |
| Token | `compressed_tokens` | Token count with compression |
| Token | `savings_ratio` | `(raw - compressed) / raw` |
| Compression | `compression_triggers` | Times compression was triggered |
| Compression | `cache_hits` | Cache hit count |
| Compression | `incremental_calls` | Incremental compression count |
| Compression | `fallback_triggers` | Fallback (L3 truncation) count |
| Performance | `compression_time_ms` | Time spent in compression logic |
| Performance | `inference_time_ms` | Time spent in model inference |
| Performance | `total_time_ms` | Total scenario execution time |
| Cross-run | `cross_run_cache_reuse` | Cache reuse ratio across multiple turns |
| Quality | `step_count` | Number of agent steps |
| Quality | `final_answer_length` | Length of final answer |
| Quality | `error_count` | Number of errors encountered |

```python
@dataclass
class RunMetrics:
    scenario_name: str
    config_label: str              # "baseline" or "opt"
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

Generates human-readable and machine-readable reports.

```python
class ConsoleReporter:
    def print_summary(self, all_metrics: List[RunMetrics]) -> None: ...
    def print_comparison(self, baseline: RunMetrics, opt: RunMetrics) -> None: ...

class JsonReporter:
    def save(self, all_metrics: List[RunMetrics], path: str) -> None: ...

class CsvReporter:
    def save(self, all_metrics: List[RunMetrics], path: str) -> None: ...
```

**Console output example:**

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

Orchestrates scenario execution and baseline-vs-opt comparisons.

```python
class EvaluationRunner:
    def __init__(self, registry: ScenarioRegistry, collector: MetricsCollector) -> None: ...

    async def run_scenario(self, scenario: Scenario, config_label: str) -> RunMetrics:
        """Execute a single scenario (baseline or opt)."""
        ...

    async def run_comparison(self, scenario: Scenario) -> Tuple[RunMetrics, RunMetrics]:
        """Execute both baseline and opt for a scenario."""
        ...

    async def run_all(self, tag_filter: Optional[str] = None) -> List[RunMetrics]:
        """Iterate all scenarios, run baseline-vs-opt for each."""
        ...
```

**Execution logic per scenario:**
1. Construct `AgentRunInfo` with `cm_config(enabled=False)` → baseline run
2. Construct `AgentRunInfo` with `cm_config(enabled=True)` → opt run
3. Both runs use the same `run_multi_turn()` mechanism from `test_utils.py`
4. Extract compression events from `ContextManager.get_all_compression_stats()`
5. Extract quality metrics from `AgentRunResult`
6. Measure timing with `time.perf_counter()`
7. Return `RunMetrics` for both runs

## Data Flow

```
Scenario YAML / Python  →  ScenarioRegistry  →  EvaluationRunner
                                                         │
                    ┌────────────────────────────────────┼──────────────────────┐
                    │                                    │                      │
                    ▼                                    ▼                      ▼
            Baseline Run                          Opt Run                 MetricsCollector
            (cm disabled)                         (cm enabled)                   │
                    │                                    │                      │
                    ▼                                    ▼                      ▼
            AgentRunResult                    AgentRunResult +              RunMetrics
                                              CM compression stats         (per run)
                                                                              │
                                                                              ▼
                                                                       Reporters
                                                                       (console/
                                                                        JSON/CSV)
```

## Error Handling

| Scenario | Handling |
|----------|----------|
| History file missing | `ScenarioRegistry.register()` raises `ValueError` at registration time |
| Agent run exception | Caught, `error_count += 1`, `final_answer` marked as failed, continue to next scenario |
| Summary generation failure | Relies on `agent_context.py` internal L3 fallback; records `fallback_triggers += 1` |
| Per-scenario timeout | Configurable `per_scenario_timeout`; on timeout, record and skip |
| Baseline/opt comparison anomaly | Recorded independently; does not block report generation |

## Usage Patterns

### Register a new scenario (user-provided data)

```python
# In evaluation/scenarios.py or a user file
registry.register(Scenario(
    name="your_new_scene",
    description="Your scene description",
    history_file="./your_history.md",
    queries=["Question 1", "Question 2"],
    cm_config=ContextManagerConfig(enabled=True, token_threshold=5000),
    tags=["previous_run", "your_tag"]
))
```

### Run evaluations

```bash
# Run all scenarios
uv run sdk/scripts/evaluation/main.py --all

# Run scenarios by tag
uv run sdk/scripts/evaluation/main.py --tag previous_run

# Output reports
uv run sdk/scripts/evaluation/main.py --all --json report.json --csv report.csv

# Run single scenario
uv run sdk/scripts/evaluation/main.py --scenario p1_first_comp
```

### Run regression tests (independent)

```bash
uv run pytest sdk/scripts/tests/test_context_manager.py -v
```

## Migration of Existing Tests

The existing `test_context_manager.py` will be trimmed to retain only hard-assertion correctness tests:
- Cache validation logic (`_is_prev_cache_valid`, `_is_curr_cache_valid`)
- Budget trimming (`_trim_pairs_to_budget`, `_trim_actions_to_budget`)
- Fallback behavior verification
- Cross-run cache invalidation

Effectiveness tests (token savings measurement, cache hit rate validation) migrate to the new evaluation framework.

## Future Extensions

- Add LLM-as-judge quality evaluation (semantic consistency between baseline and opt answers)
- Support parallel scenario execution
- Add historical trend tracking (compare metrics across framework versions)
- Web dashboard for report visualization
