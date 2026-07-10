# Generic Benchmark：真实 SDK 行为与简化项审计

## 结论

当前 Generic Benchmark 不是重新实现一套 Nexent SDK。它由 benchmark 代码模拟平台侧的输入、
配置构造和结果采集，然后直接调用真实的 Nexent SDK Agent 执行链路。

因此，Agent 核心运行时和启用后的上下文压缩逻辑是真实 SDK 行为，但当前实验尚未完整复现
Nexent 平台生产环境中的配置加载、多轮会话、上下文组件、工具、子 Agent、容量预算和观测链路。

## 实际调用链

```text
Langfuse DatasetItem
  -> generic/task_adapter.py
  -> benchmark/agent_runner.py 构造 AgentRunInfo
  -> nexent.core.agents.run_agent.agent_run()
  -> NexentAgent.create_single_agent()
  -> CoreAgent.run()
  -> ManagedContextRuntime / LegacyContextRuntime
  -> ContextManager 组装、预算控制和压缩
  -> OpenAIModel 调用真实 LLM
```

## 真实 SDK 行为

### R1. Agent 创建和 ReAct 执行循环

Benchmark 最终调用 SDK 的 `agent_run()`，由 `NexentAgent` 创建真实的 `CoreAgent` 和
`OpenAIModel`，并执行 SDK 的 step、工具调用、观察和最终答案流程。Agent 输出不是 benchmark
预制或 mock 的。

相关代码：

- `sdk/benchmark/generic/task_adapter.py`：调用 `run_agent_with_tracking()`
- `sdk/benchmark/agent_runner.py`：`run_agent_with_tracking()` 消费真实 `agent_run()` 消息流
- `sdk/nexent/core/agents/run_agent.py`：创建并运行 `NexentAgent`
- `sdk/nexent/core/agents/nexent_agent.py`：创建 `CoreAgent` 和模型

### R2. 上下文运行时选择

`NexentAgent.create_single_agent()` 根据 `ContextManagerConfig.enabled` 选择真实 SDK 运行时：

- 启用：`ContextManager` + `ManagedContextRuntime`
- 禁用：`LegacyContextRuntime`

这不是 benchmark 自己实现的上下文运行时。

### R3. 每一步上下文组装

`CoreAgent` 在每次模型调用前调用 `context_runtime.prepare_step()`，并将返回的
`FinalContext.messages` 直接作为模型输入。达到最大步数后生成最终答案时，也会调用
`context_runtime.prepare_final_answer()`。

因此，模型实际看到的消息顺序和内容由 SDK 上下文运行时决定。

### R4. ContextManager 压缩

启用 ContextManager 后，`ManagedContextRuntime` 直接调用真实
`ContextManager.assemble_final_context()`。相关逻辑包括：

- token 预算和阈值判断
- 历史消息分区
- 保留最近 steps 和 conversation pairs
- LLM 摘要压缩
- 增量摘要
- summary cache
- 压缩失败后的截断降级
- 最终上下文组装

达到压缩条件时，执行的是 SDK 的真实压缩算法及真实摘要模型调用。

### R5. Observer 消息流与基础 token 统计

Benchmark 通过 SDK 的 `MessageObserver` 消费 `step_count`、`model_output`、
`execution_logs`、`final_answer` 和 `token_count` 等消息。最终答案和基础 token 数据来自
真实 Agent 运行过程。

## Benchmark 模拟或简化项

以下项目使用稳定编号，后续整改和验收按编号跟踪。

### G1. 平台侧 AgentRunInfo 由 benchmark 手工构造

`sdk/benchmark/agent_runner.py` 直接从环境变量创建 `ModelConfig`，并手工创建
`AgentConfig`、`MessageObserver` 和 `AgentRunInfo`。

当前没有经过 backend service、数据库配置解析、模型容量解析等完整生产链路。因此当前测试
目标更接近 SDK Core，而不是 Nexent 平台端到端运行。

### G2. 导出的 YAML Agent 配置没有被完整应用

`run_benchmark.py` 当前主要读取：

- prompts
- `max_steps`
- `enable_context_manager`

导出配置中的 tools、sub-agents 和 skills 没有被转换为 SDK 配置并传给
`make_nexent_task()`。当前 Generic Benchmark 通常实际运行的是：

```text
单 Agent + 无工具 + 无子 Agent + 无技能
```

### G3. ContextManager 只支持启用或禁用

`run_benchmark.py` 当前只构造：

```python
ContextManagerConfig(enabled=enable_cm)
```

其余配置均使用 SDK 默认值，不能从实验配置控制或还原线上参数，例如：

- `token_threshold`
- soft/hard input budget
- `keep_recent_steps`
- `keep_recent_pairs`
- summary 输入和缩减预算
- component budgets
- observation 截断长度
- 上下文选择策略

### G4. 每个数据项都是空历史的独立会话

`task_adapter.py` 在构造每次运行时固定传入 `history=[]`。当前可以测试一次 run 内多步 ReAct
导致的上下文增长和压缩，但不能完整测试：

- 多轮用户对话
- 前序 run 的 task/action pair 压缩
- conversation-level ContextManager 复用
- 跨请求增量摘要和 cache

### G5. 动态上下文组件未按生产方式组装

当前没有设置 `AgentConfig.context_components`。长期记忆、知识库摘要、skills、应用上下文和
Agent definitions 等内容，也没有按照生产链路构造成 ContextManager 组件。

系统 prompt 主要由 benchmark 提前渲染。因此可以测试消息历史压缩，但不能完整测试组件选择、
优先级、注入开关和预算分配。

**已发现的 SDK 级缺陷（已修复）**：当 `context_manager.enabled=True` 且
`context_components=[]`（benchmark 场景的常见配置）时，`prepare_run_context` 构建的
`stable_messages` 为空列表。`assemble_final_context` 调用 `_without_leading_stable_messages()`
从历史消息中剥掉 system prompt，但空的 `stable_messages` 没有将其重新加回。结果：benchmark
提前渲染的 system prompt（含 few-shot CoT 等内容）被静默丢弃，模型只收到 user message。

此缺陷在 G6 修复前被掩盖——`--system-prompt-file --enable-context-manager` 因 G6 bug
降级到 LegacyContextRuntime（后者直接从 `memory.system_prompt` 读取，不受此问题影响）。G6
修复后正确启用 ManagedContextRuntime，暴露了此缺陷。

修复：`prepare_run_context` 在 `stable_messages` 为空且 `fallback_system_prompt` 存在时，
构造 `ChatMessage(role=SYSTEM, content=fallback_system_prompt)` 作为 stable_messages。

### G6. 自定义 system prompt 路径漏传 ContextManager 配置

使用 `--system-prompt-file` 时，`task_adapter.py` 调用
`build_agent_run_info_with_custom_prompt()`，但没有传入 `context_manager_config`。

因此以下参数组合虽然表面上启用了 ContextManager，实际会使用 LegacyContextRuntime：

```bash
--system-prompt-file path/to/prompt.txt --enable-context-manager
```

这是明确的实现缺陷，不只是实验范围简化。

### G7. 压缩观测指标没有完整写入实验结果

当前 Langfuse 输出主要记录：

- final answer
- step 数
- 总输入和输出 token
- thinking、code 和 observation

SDK 已提供但 benchmark 尚未完整记录的内容包括：

- compression calls
- summary input/output tokens
- cache hits 和 cache 类型
- compression ratio
- 压缩前后 token 数
- compression boundary
- exported summary
- final context evidence

所以压缩逻辑可能真实执行，但仅凭当前实验输出难以验证压缩发生的位置、方式和效果。

### G8. 配置覆盖行为与导出配置不完全一致

当前 `temperature` 没有从 YAML 的 Agent 配置读取；未传 CLI 参数时固定使用 `0.1`。
其他生产配置也可能在“导出成功”后没有进入执行路径。需要建立导出字段到运行字段的显式映射
和覆盖优先级测试。

### G9. `max_concurrency` 参数当前没有实际并发效果

`run_experiment()` 接收 `max_concurrency`，但仍通过普通 `for` 循环逐项调用 `task_fn`。
该问题不影响单次 Agent 运行真实性，但会影响 benchmark 的吞吐测试和参数语义。

## 当前实验可以声称的范围

当前可以准确描述为：

> 使用简化的配置构造器和 Langfuse 数据集驱动真实 Nexent SDK CoreAgent，真实执行单 Agent
> ReAct、模型调用、step 上下文组装以及可选的 ContextManager 压缩。

当前不应描述为：

> 完整复现 Nexent 平台中的 Agent 创建、完整配置加载、多轮会话、动态上下文组件、工具与子
> Agent、容量预算和压缩观测链路。

## 建议整改顺序

| 优先级 | 编号 | 原因 | 验收目标 |
|---|---|---|---|
| P0 | G6 | 参数声明与实际行为不一致 | 自定义 prompt 路径正确启用 ManagedContextRuntime |
| P0 | G7 | 无法可靠判断压缩实验结果 | 每一步输出压缩调用、token、cache、比例和边界 |
| P1 | G3 | 无法控制关键实验变量 | YAML/CLI 可配置完整 ContextManagerConfig |
| P1 | G4 | 无法测试会话级上下文管理 | 支持多轮数据集和同一 ContextManager 跨 run 复用 |
| P1 | G5 | 上下文组装覆盖不完整 | 构造并记录真实 context components |
| P1 | G2 | 导出 Agent 与实际运行不一致 | tools、sub-agents、skills 可加载并执行 |
| P2 | G8 | 配置可重复性不足 | 所有运行字段有明确来源和覆盖测试 |
| P2 | G1 | 缺少平台端到端覆盖 | 明确区分 SDK Core 模式与平台集成模式 |
| P2 | G9 | CLI 参数语义不完整 | `max_concurrency` 被实现或删除 |

## 整改记录

| 编号 | 状态 | 变更 | 验证 |
|---|---|---|---|
| G1 | 待处理 | | |
| G2 | 待处理 | | |
| G3 | 待处理 | | |
| G4 | 待处理 | | |
| G5 | SDK 缺陷已修复 | `prepare_run_context` 在 `stable_messages` 为空且 `fallback_system_prompt` 存在时，构造 system ChatMessage 作为 stable prefix，防止 system prompt 被 `_without_leading_stable_messages` 静默丢弃 | `context_components=[]` + `enabled=True` 时 system prompt 正确出现在模型输入中 |
| G6 | 已修复 | `task_adapter.py` 的 custom prompt 分支补传 `context_manager_config` | `--system-prompt-file --enable-context-manager` 正确启用 ManagedContextRuntime（注：修复后暴露了 G5 的 system prompt 丢弃缺陷，G5 已同步修复） |
| G7 | 已修复 | SDK `agent_run_with_observer` 将 `step_metrics` 中的压缩字段转发到 `token_count` observer 消息；benchmark `AgentRunResult` 新增压缩聚合字段；`run_agent_with_tracking` 解析每步压缩数据；`task_adapter` 输出 `compression` 段；`run_benchmark` 将压缩指标写入 Langfuse trace metadata、step span metadata 和 trace score | 每步输出 compression calls、input/output tokens、cache hits、cache types、ratio；trace 级聚合可对比不同配置 |
| G8 | 待处理 | | |
| G9 | 待处理 | | |
