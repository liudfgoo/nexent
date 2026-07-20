# Generic Benchmark：真实 SDK 行为与当前差距审计

> 本版基于 2026-07-20 的代码重新审计，已纳入 `run_context_manager_comparison.py`、
> resolved experiment manifest、CLI 校验和 smoke item limit。
>
> 更完整的 ContextManager 实验 readiness 清单见
> `doc/working/benchmark-generic-opentelemetry-design/benchmark-readiness-gaps-and-next-steps.md`。

## 1. 结论

Generic Benchmark 没有重新实现 Nexent Agent 或 ContextManager。它用 benchmark 侧代码构造
`AgentRunInfo`，然后调用真实 Nexent SDK 执行 Agent、模型、工具、ContextRuntime 和压缩流程。

当前最准确的定位是：

> 由 Langfuse dataset 驱动、使用简化平台配置构造器的 Nexent SDK Core benchmark。

它已经具备：

- 真实 SDK Agent/ReAct/工具执行；
- Legacy 和 Managed ContextRuntime 分流；
- 真实 ContextManager 压缩；
- 基础 compression metrics；
- resolved manifest；
- Legacy / Managed-No-Compression / Managed-Compression 三组编排；
- smoke、repeat、配对 item outcome 和 manifest parity 检查。

但它仍不是 Nexent 平台端到端 benchmark，也还不能单靠现有 trace 对 compression loss 作严格归因。
主要缺口是：

- 每个 dataset item 仍是独立空历史会话；
- sub-agents 和 skills 虽被导出，但没有进入运行；
- ContextManager 只有部分参数可配置；
- FinalContext、summary、compression records 和 budget/overflow 证据未写入 trace；
- Legacy 与 Managed observation policy 不一致；
- run integrity 检查不完整；
- `max_concurrency` 仍未实现。

## 2. 实际调用链

单组运行：

```text
Langfuse DatasetItem
  -> generic/run_benchmark.py
  -> generic/task_adapter.py
  -> benchmark/agent_runner.py 构造 AgentRunInfo
  -> nexent.core.agents.run_agent.agent_run()
  -> NexentAgent.create_single_agent()
  -> CoreAgent.run()
  -> ManagedContextRuntime / LegacyContextRuntime
  -> ContextManager 组装、预算控制和可选压缩
  -> OpenAIModel 和真实工具调用
```

A/B/C 对照：

```text
run_context_manager_comparison.py
  -> preflight
  -> 多次调用 run_benchmark.py
       A: Legacy
       B: Managed + 高阈值
       C: Managed + 正常阈值
  -> resolved manifest parity
  -> paired outcome report
```

## 3. 已确认的真实 SDK 行为

### R1. Agent 创建和 ReAct 执行循环

`task_adapter.py` 最终调用 `run_agent_with_tracking()`；后者消费真实 `agent_run()` 消息流。
`NexentAgent` 创建真实 `CoreAgent` 和模型，并执行 step、代码、工具、observation 和 final answer。
Agent 输出不是 benchmark mock 或预制结果。

相关文件：

- `sdk/benchmark/generic/task_adapter.py`
- `sdk/benchmark/agent_runner.py`
- `sdk/nexent/core/agents/run_agent.py`
- `sdk/nexent/core/agents/nexent_agent.py`
- `sdk/nexent/core/agents/core_agent.py`

### R2. ContextRuntime 选择

`NexentAgent.create_single_agent()` 根据 `ContextManagerConfig.enabled` 选择：

- 开启：`ContextManager` + `ManagedContextRuntime`；
- 关闭：`LegacyContextRuntime`。

因此 `--enable-context-manager` 和 `--disable-context-manager` 控制的是真实 SDK runtime，
不是 benchmark 内的模拟分支。

### R3. 每一步和 Final Answer 的上下文组装

`CoreAgent` 在模型调用前执行：

- step：`context_runtime.prepare_step()`；
- 达到最大步数后的 final answer：`context_runtime.prepare_final_answer()`。

返回的 `FinalContext.messages` 和 `FinalContext.tools` 直接进入模型调用。Managed 路径由
`ContextManager.assemble_final_context()` 组装，Legacy 路径由原始 memory 组装。

### R4. ContextManager 压缩

Managed 路径达到阈值时，执行真实 SDK 压缩逻辑，包括：

- previous/current history 分区；
- `keep_recent_steps` 和 `keep_recent_pairs`；
- LLM summary；
- incremental summary；
- summary cache；
- 压缩失败后的 fallback；
- 最终上下文重组。

### R5. Observer 和基础指标

Benchmark 从 `MessageObserver` 消费：

- `step_count`；
- `model_output`；
- `execution_logs`；
- `final_answer`；
- `token_count`；
- error。

当前能写入 step/trace 的 compression 基础指标包括：

- calls；
- summary input/output tokens；
- cache hits 和 cache types；
- compression ratio；
- uncompressed estimated tokens。

### R6. A/B/C 标准编排

`run_context_manager_comparison.py` 已提供：

- A：Legacy；
- B：Managed + 默认 `token_threshold=1000000`；
- C：Managed + 默认 `token_threshold=10000`；
- smoke 和正式 repeat；
- A/B/C 受控随机交错顺序；
- 相同 dataset 前 N item smoke；
- 自动配对 run name；
- 本地 manifest、Langfuse run 和报告同名保护；
- primary evaluator 的 paired Pass/Fail outcome matrix。

比较口径已经分开：

```text
A vs B：runtime/assembly effect
B vs C：compression effect
A vs C：ContextManager overall effect
```

B 组使用高阈值只是“预期不压缩”，不是算法级 `no_compression` 模式。正式分析仍必须确认
`compression_calls=0`。

### R7. Resolved Experiment Manifest

每个新 run 会生成本地、不可覆盖的 JSON manifest，并在 Langfuse trace metadata 中记录
manifest hash 和本地 path。当前内容包括：

- dataset name、item IDs 和可用时的 dataset version；
- run name 和 Git commit；
- ContextRuntime 和完整 `ContextManagerConfig` dataclass；
- model、endpoint、temperature、max steps 和 language；
- tool count/schema hash；
- system prompt hash；
- agent config hash；
- evaluator 名称；
- lifecycle 和 observation policy；
- 启动时间和环境标识。

Manifest 会清理 endpoint 凭据、query string 和常见敏感字段。

### R8. CLI 安全校验和确定性 Smoke

当前已有：

- enable/disable CM 互斥；
- threshold 必须大于 0；
- keep recent 和 observation limit 不得为负数；
- Legacy 不接受 CM-only 参数；
- `max_concurrency` 和 `item-limit` 必须大于 0；
- `--item-limit` 对 dataset 前 N 项进行确定性 smoke；
- comparison run 固定 system prompt 模板使用的实验时间。

## 4. Benchmark 模拟或简化项

以下编号作为后续整改的稳定引用。

### G1. 平台侧 AgentRunInfo 仍由 benchmark 手工构造

**状态：待处理**

`agent_runner.py` 直接从环境变量创建 `ModelConfig`，并手工创建 `AgentConfig`、
`MessageObserver` 和 `AgentRunInfo`。它没有经过：

- backend service；
- 数据库模型配置解析；
- tenant/user 权限；
- 模型容量解析；
- production conversation/run manager；
- 完整平台观测和异常映射。

因此当前测试目标仍是 SDK Core，而不是 Nexent 平台端到端行为。

### G2. 导出的 YAML Agent 配置仅部分应用

**状态：部分完成**

已经应用：

- prompts；
- `max_steps`；
- `enable_context_manager`；
- tools；
- 手工添加时的 `temperature`。

tools 已通过 `build_tools_from_yaml()` 重建为 `ToolConfig`。Analyze* 工具可从环境变量构造
MinIO、VLM、LLM 和 data-process metadata；GAIA 文件可注入 S3 URL。

仍未应用：

- `sub_agents`；
- `skills`；
- `provide_run_summary`；
- `verification_config`；
- 其他导出或生产 Agent 配置。

依赖 KB、memory 等外部 metadata 的部分工具会被跳过并打印 warning。因此不能再描述成
“无工具”，但也不能描述成“完整复现导出 Agent”。

### G3. ContextManager 参数只开放了子集

**状态：部分完成**

当前 CLI 可以控制：

- `enabled`；
- `token_threshold`；
- `keep_recent_steps`；
- `keep_recent_pairs`；
- `max_observation_length`。

其余字段使用 SDK 默认值，尚不能从 YAML/CLI 完整控制，例如：

- soft/hard input budget；
- summary input/reduce budget；
- `chars_per_token`；
- strategy；
- component budgets；
- component injection flags；
- max memory step/chunk；
- summary prompt/schema；
- 独立 summary model。

Manifest 会记录最终 dataclass 值，这提高了可追溯性，但不等于这些字段已经可配置。

### G4. 每个 dataset item 仍是空历史独立会话

**状态：待处理**

`task_adapter.py` 两条 Agent 构造路径均固定传入 `history=[]`，每个 item 重新创建 Agent 和
ContextManager。当前只覆盖单次 run 内 action steps 增长，不能完整验证：

- previous conversation history 压缩；
- conversation-level ContextManager 复用；
- 跨请求 incremental summary/cache；
- `keep_recent_pairs` 的真实多轮行为；
- run 切换和 conversation 清理；
- stale state。

Manifest 正确标记当前 lifecycle 为 `isolated-item`，但尚无 `conversation-session` runner。

### G5. Context components 只有基础生产模拟

**状态：部分完成**

Managed 模板路径会调用 `build_context_components()`，custom system prompt 路径会构造
`SystemPromptComponent`。因此 system prompt 不再因空 components 被剥离，基础 prompts 和
tools 可以形成 component。

尚未完整覆盖 production 动态组件：

- long-term memory；
- knowledge base summary；
- 实际 skills；
- sub-agent definitions；
- external A2A agents；
- tenant/user app context；
- production relevance、priority 和 budget 输入。

当前 manifest 记录 component type，但每步 `FinalContext.evidence` 尚未进入 benchmark trace。

历史上已修复的 SDK 问题：

- Managed 路径 components 为空时 system prompt 被剥离；
- custom system prompt 路径未正确启用 Managed runtime。

### G6. Custom system prompt 的 ContextManager 配置漏传

**状态：已修复**

`task_adapter.py` 的 custom prompt 分支已经传入 `context_manager_config`，
`build_agent_run_info_with_custom_prompt()` 在 CM 开启时创建 `SystemPromptComponent`。

以下组合现在会使用 Managed runtime：

```bash
--system-prompt-file path/to/prompt.txt --enable-context-manager
```

### G7. Compression metrics 已有，但归因证据仍不完整

**状态：部分完成**

已记录：

- per-step compression calls/tokens/cache/ratio；
- trace 聚合 compression calls/tokens/cache；
- estimated uncompressed tokens；
- 部分 trace score。
- 每次实际模型调用的 `agent.final_context` 事件，包括 purpose、message/tool/system/history/final-answer
  prompt fingerprint、message role 结构、selected component types、stable/dynamic message count；
- stable prefix fingerprint/change reasons、context overhead、pre/post compression tokens；
- compression records（不包含自由格式 details）、summary fingerprint/fallback 和 observation truncation evidence。
- provider 明确返回的 prefix-cache cached/uncached input tokens、call hit 和 metrics source；
- provider prefix cache 与 ContextManager summary cache 分字段进入 item/run/comparison 报告。

尚未稳定记录或关联：

- debug run 的完整脱敏 `FinalContext` 或外部 artifact reference；
- summary 和大型 observation 的外部 artifact；
- compression boundary；
- soft/hard budget 和 overflow；

`context_evidence_diff.py` 可以按 item、step 和 purpose 对齐 A/B/C 导出的
`agent.final_context` 属性，报告首次 system/tool/history/summary/truncation/final-answer
prompt 差异。默认观测不包含消息、summary、tool schema 或 compression details 原文。

### G8. 配置覆盖和复现链仍不完整

**状态：部分完成**

已有改进：

- `temperature` 优先级为 CLI → 手工 YAML → `0.1`；
- 标准导出 YAML 不包含 temperature 的事实已在文档中说明；
- comparison runner 建议显式指定 `max_steps`、temperature 和两个 threshold；
- manifest 记录 resolved CM config、hash、commit 和 dataset item IDs；
- A/B/C 完成后检查非目标 manifest 字段一致。

仍有缺口：

- `export_agent_config.py` 不导出 temperature 和完整 CM config；
- 多个导出字段没有进入执行路径；
- manifest 在第一个 item 执行后才生成；
- 第一个 task 在构造配置前异常时，manifest 可能缺少有效 model/prompt/agent 字段；
- manifest 只保存本地 artifact，Langfuse 中只有本地 path/hash；
- evaluator version 目前只写 `"code_commit"`；
- summary model 当前按“使用主模型”记录，没有独立解析链；
- 没有运行结束后的 trace→manifest 反向一致性检查。

### G9. `max_concurrency` 参数仍没有实际并发效果

**状态：待处理**

`run_experiment()` 接收并记录 `max_concurrency`，但仍通过普通 `for` 循环逐项调用 `task_fn`。
因此：

- `--max-concurrency 4` 仍是串行；
- manifest 中的值是请求值，不是实际观测并发度；
- 当前不能用它进行吞吐或并发安全性结论。

短期至少应在 CLI help 明确标注未实现，长期应实现受控并发、限流和 trace 隔离。

### G10. Legacy 与 Managed observation policy 不一致

**状态：待处理**

当前：

- Legacy 固定在 100000 字符保留首尾；
- Managed 默认 `max_observation_length=0`，即不预截断；
- Managed 的 CLI limit 不作用于 Legacy。

Manifest 会记录差异，但没有统一 runtime-independent policy，也没有逐 step truncation 指标。
因此受超长 observation 影响的 A/B/C item 不能把 A/C 差异直接归因于 compression。

### G11. Run integrity 和配对完整性检查仍不充分

**状态：部分完成**

已有：

- dataset 非空和 item ID 唯一检查；
- A/B/C run name 防覆盖；
- 每轮 manifest 非目标字段 parity；
- A/B/C 配对前要求三组 item ID 集合完全一致。

缺少：

- expected item count 与 linked trace count 对账；
- missing/duplicate trace；
- missing score；
- empty output；
- trace error；
- manifest 与每条 trace 的 resolved config 对账；
- incomplete run 状态。

当前轻量校验可以阻止某组缺失或多出 item 时继续配对，但不检查 trace/score/empty output 等完整性，
因此 comparison report 仍不能单独证明整个 run 完整。

### G12. 工具依赖预检需要人工声明

**状态：部分完成**

Comparison runner 支持重复传入：

```text
--required-url NAME=URL
```

它会在 Agent/LLM 调用前检查 URL 连接和 HTTP 5xx，从而提前发现已知服务故障。

但当前不会：

- 从 Agent YAML 自动发现所有依赖；
- 验证 URL 与工具实际使用 endpoint 一致；
- 检查非 HTTP 依赖；
- 检查认证后的真实工具操作；
- 证明服务在整个实验期间持续健康。

不传时，服务问题可能直到工具构造或首次调用才出现。

## 5. 当前实验可以和不可以声称什么

### 可以声称

> 使用简化配置构造器和 Langfuse dataset 驱动真实 Nexent SDK CoreAgent，真实执行单 Agent
> ReAct、模型调用、已加载工具、Legacy/Managed 上下文组装及可选 ContextManager 压缩。

使用 comparison runner 且 manifest parity 通过时，还可以声称：

> A/B/C 使用相同 dataset item、代码 commit、模型、system prompt hash、tools、temperature、
> max steps 和 evaluator 配置；A/B 用于观察 runtime/assembly 差异，B/C 用于观察压缩候选效应。

### 不可以声称

- 完整复现 Nexent 平台端到端 Agent 创建与配置加载；
- 已覆盖 conversation-level ContextManager；
- 已执行 YAML 中的 sub-agents 和 skills；
- B 组一定没有压缩，除非实际确认 `compression_calls=0`；
- A/C 差异等于 compression effect；
- 某次失败已经确认是 compression loss；
- manifest 存在就代表 run 完整；
- `max_concurrency` 已实现并发；
- 当前 token 指标等同于完整 provider 计费成本。

## 6. 当前优先整改顺序

完整顺序以 readiness 文档为准。基于本次审计，紧接着应优先处理：

| 顺序 | 对应缺口 | 工作项 |
|---:|---|---|
| 1 | G7 | FinalContext、summary、compression records 和 diff evidence |
| 2 | G10 | runtime-independent observation policy 和 truncation evidence |
| 3 | G11 | run integrity report，incomplete run 禁止进入配对统计 |
| 4 | G3 | 完整 resolved ContextManager 配置入口 |
| 5 | G4 | conversation-session lifecycle |
| 6 | G2/G5 | sub-agents、skills 和动态 context components |
| 7 | G8 | export→run 字段映射和 manifest artifact/integrity |
| 8 | G9 | 实现并发或移除误导参数 |
| 9 | G12 | 从工具配置自动发现依赖并预检 |
| 10 | G1 | 增加平台集成模式，与 SDK Core 模式分开 |

## 7. 整改状态汇总

| 编号 | 状态 | 当前结论 |
|---|---|---|
| G1 | 待处理 | 仍由 benchmark 手工构造 AgentRunInfo |
| G2 | 部分完成 | tools 已加载；sub-agents、skills 和其他导出字段未应用 |
| G3 | 部分完成 | 开放四个核心 CM 参数；完整配置仍不可控 |
| G4 | 待处理 | item 仍为 `history=[]` 的 isolated lifecycle |
| G5 | 部分完成 | 基础 components 已构造；生产动态组件和 per-step evidence 缺失 |
| G6 | 已修复 | custom prompt 能正确进入 Managed runtime |
| G7 | 部分完成 | 默认安全 FinalContext 证据和首次差异工具已具备；完整脱敏 artifact、budget/overflow 尚缺 |
| G8 | 部分完成 | manifest 和 parity 已有；export/replay/integrity 不完整 |
| G9 | 待处理 | 参数存在但实际串行 |
| G10 | 待处理 | Legacy/Managed observation policy 不公平 |
| G11 | 部分完成 | 基础 pairing/parity 已有；完整 integrity 未实现 |
| G12 | 部分完成 | 支持声明式 HTTP 预检；未自动发现工具依赖 |

## 8. 相关文档

- `sdk/benchmark/generic/RUN_BENCHMARK.md`
- `sdk/benchmark/generic/RUN_CONTEXT_MANAGER_COMPARISON.md`
- `sdk/benchmark/generic/EXPORT_AGENT_CONFIG.md`
- `doc/working/benchmark-generic-opentelemetry-design/benchmark-readiness-gaps-and-next-steps.md`
- `doc/working/benchmark-generic-opentelemetry-design/agent-benchmark-attribution-simple.md`
- `doc/working/benchmark-generic-opentelemetry-design/agent-benchmark-attribution-full.md`
