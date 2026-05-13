
****************
# CLAUDE.md

## 目标

构建 Agent Context Compression Benchmark。

重点评估：

- 压缩后是否还能继续任务（continuation）
- 压缩后是否还能记住关键状态（memory retention）
- token 是否有效下降（token reduction）

不要只评估 summary 是否相似。

---

## 消息结构

Agent 历史消息结构：

```python
[
  {"role": "user", "content": taskstep},
  {"role": "assistant", "content": actionstep},
]

[(taskstep, actionstep), ...]
```

---

## Benchmark Case 结构

每个 case 是 `cases/` 下的一个独立目录，包含 `case.json` 和 `history.json`：

```
benchmark/
├── cases/                      # test_benchmark.py input
│   └── <case_id>/
│       ├── case.json           # queries, probes, checks, config
│       └── history.json        # conversation history
├── inspections/                # summary_inspector.py input (independent)
│   └── <name>/
│       ├── history.json        # conversation history
│       ├── checks.json         # [{"description": "...", "must_contain": [...]}]
│       ├── _result.json        # output: inspection result (auto-generated)
│       └── _summary.txt        # output: raw summary text (with --save-summary)
├── reports/                    # test_benchmark.py output
│   ├── <case_id>.json         # full benchmark per-case report
│   └── summary.json           # full benchmark cross-case metrics
├── agent_runner.py             # agent run + tracking utilities
├── eval_utils.py               # keyword-based evaluation
├── summary_inspector.py        # standalone summary inspection (low cost, no agent)
└── test_benchmark.py           # full benchmark runner
```

`case.json` 格式：

```json
{
  "id": "example_infra",
  "history_file": "history.json",
  "queries": [],
  "probes": [],
  "summary_checks": [],
  "task_checks": [],
  "compressed_config": {}
}
```

* `id`：case 唯一标识，也作为报告文件名
* `history_file`：对话历史文件，相对 case 目录（默认 `history.json`）
* `queries`：continuation query
* `probes`：记忆探针问题
* `summary_checks`：静态摘要检查
* `task_checks`：任务结果检查
* `compressed_config`：压缩配置覆盖

`history.json` 格式：

```json
[
  {"role": "user", "content": "..."},
  {"role": "assistant", "content": "..."}
]
```

只支持 JSON 格式，不支持 Markdown。

---

## 评估模式

每个 case 跑两组：

1. baseline（不压缩）
2. compressed（开启压缩）

核心指标：

```python
task_success_retention =
compressed_task_score / baseline_task_score

probe_retention =
compressed_probe_score / baseline_probe_score

token_reduction =
1 - compressed_tokens / baseline_tokens
```

---

## Continuation Evaluation

continuation query 模拟真实多轮 Agent。

允许：

* history 增长
* compression 持续发生
* ContextManager 跨轮复用

这是 stateful evaluation。

---

## Probe Evaluation

probe 用于检查压缩后 agent 能否**利用**残留信息回答问题。

重要规则：

* freeze 压缩后的 history snapshot（每个 probe deep copy）
* 每个 probe 独立运行
* probe 不允许修改原始 history（用 deep copy 隔离）
* probe 之间不能共享上下文

压缩只做一次，所有 probe 复用结果：

1. 先从 compressed run 的 `export_summary()` 获取 summary + compression_boundary
2. 用 `build_precompressed_history()` 构建预压缩 history：
   - 被压缩的 pairs 替换为一条 (user=summary, assistant=ack)
   - 保留的尾部 pairs 原样保留
3. 每个 probe 用预压缩 history + `compression disabled` 运行
4. 这样避免了每个 probe 重复走压缩流程（同样的输入 → 同样的压缩结果，无需重复调 LLM）

正确：

```text
compressed run → export_summary() → build_precompressed_history()
→ [probe 1: deep copy + disabled cm → evaluate → discard]
→ [probe 2: deep copy + disabled cm → evaluate → discard]
→ ...
```

错误：

```text
每个 probe 各自创建 fresh ContextManager(enabled=True)
→ 重复做同样的压缩 LLM 调用（浪费）
```

---

## 好的 Probe

probe 必须依赖历史上下文。

好的 probe：

* 用户禁止使用什么库？
* 之前找到的文件是什么？
* 哪个方案被否决？
* 当前必须遵守什么约束？

坏的 probe：

* 什么是 Docker？
* 什么是 Elasticsearch？

后者测的是模型知识，不是 memory retention。

---

## Probe 构建原则：只指向被压缩的内容

probe 的核心目的是检测 memory retention，即"压缩掉的信息 agent 是否还能回答"。
因此 **probe 应该只问被压缩区域中的信息**，而不是保留在尾部 steps 中的信息。

压缩边界是时间性的：`keep_recent_pairs=N` 意味着最后 N 对原样保留，
前面的全部进入 summary。因此：

* **probe 应该只问 history 前半部分（early pairs）中的细节**
* 如果 probe 问的是 recent pairs 中的信息，agent 不需要 summary 就能回答，
  probe 失效——测不出 memory retention

构建 probe 时无需提前知道压缩器具体保留了什么，只需确保 probe 依赖的信息
来自 early history（必定被压缩的区域）。

**验证 probe 设计**：用 `export_summary()` 的 `compression_boundary` 字段
确认哪些 pairs 被压缩、哪些被保留。如果 probe 的答案在 summary 里根本没有，
这是压缩器的问题（归入 Static Inspection 层面），不是 agent 的问题。

---

## Static Summary Inspection vs Probe Eval

两者测的是不同的故障模式：

| | Probe Eval | Static Summary Inspection |
|--|-----------|--------------------------|
| 输入 | 完整压缩上下文（summary + 保留的尾部 steps + system prompt） | 仅 summary 文本 |
| 执行方式 | 让 agent 回答问题（跑 LLM） | 直接检查 summary 文本是否包含关键信息 |
| 测的是什么 | 压缩后 agent **能否利用**残留信息工作 | 压缩器**是否选择保留**了关键信息 |
| 失败含义 | summary 里有但 agent 没用上 → 检索/利用能力问题 | summary 里就没有 → 压缩器丢失了 |

**两个不同的故障模式**：
1. 压缩器保留了，但 agent 回答时没能利用 → **Probe Eval** 会发现，Inspection 不会
2. 压缩器根本没保留 → 两者都会发现，但应归因到 Inspection 层面

---

## Static Summary Inspection

直接检查 compressed summary 是否还包含关键信息。

### 在线方案

在 agent 运行后导出压缩状态：

```python
compressed_state = shared_cm.export_summary()
# compressed_state 包含:
#   previous_summary / current_summary: 压缩后的摘要文本
#   compression_boundary: 哪些 pairs/steps 被压缩 vs 保留
#   previous_cache_info / current_cache_info: 缓存元信息

# 检查摘要是否包含关键信息
for check in summary_checks:
    eval_text(compressed_state["previous_summary"], check)
```

### 离线方案

脱离 agent 运行，直接用相同的 prompt 和 schema 压缩纯文本 pairs：

```python
from nexent.core.agents.agent_context import compress_history_offline

# pairs: 纯文本 (user, assistant) 对，无需 AgentMemory / ActionStep
result = compress_history_offline(
    pairs=[("用户说了什么", "助手做了什么"), ...],
    model=llm_model,
    config=ContextManagerConfig(),  # 使用与 in-agent 相同的 prompt/schema
)
# result["summary"]: 压缩后的摘要
# result["is_incremental"]: 是否使用了增量压缩
# result["is_fallback"]: LLM 是否失败并使用了 fallback
# result["input_text"]: 喂给 LLM 的原始文本（用于调试）

# 然后对 summary 做 inspection 检查
eval_text(result["summary"], {"must_contain": ["关键文件名"]})
```

离线方案的优势：
- 不需要跑 agent，只需一次 LLM 调用做压缩
- 不依赖 AgentMemory、ActionStep 等运行时对象
- 适合批量评估不同 prompt/schema 对压缩质量的影响

---

## 核心原则

Benchmark 真正要回答：

> 压缩后，Agent 是否还能继续工作，并记住关键状态？

## 总结

| 类型                        | 目的                          | 输入                       | 执行方式              |
| ------------------------- | --------------------------- | ------------------------ | ----------------- |
| Continuation Eval         | 测 agent 能不能继续工作             | 完整压缩上下文 + 新 query        | 跑 agent 多轮，共享 cm  |
| Probe Eval                | 测 agent 能否利用压缩后残留信息回答问题     | 压缩后上下文 + probe question  | 跑 agent 单轮，独立 cm  |
| Static Summary Inspection | 测压缩器是否选择保留了关键信息             | 仅 summary 文本             | 直接文本检查，不需要跑 agent |