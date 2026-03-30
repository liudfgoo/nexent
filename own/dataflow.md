# 关键数据流：Nexent

## 数据流总览

| # | 数据流名称 | 触发方式 | 核心路径 |
|---|-----------|----------|----------|
| 1 | Agent 对话执行 | WebSocket 连接 | 前端 → Runtime Service → AgentRunManager → NexentAgent ��� CoreAgent → LLM → 工具执行 → 流式响应 |
| 2 | 知识库检索 | Agent 工具调用 | CoreAgent → KnowledgeBaseSearchTool → VectorDatabaseCore → Elasticsearch → 返回匹配文档 |
| 3 | 文件上传与向量化 | HTTP POST | 前端 → Config Service → Data Process Service → Ray Worker → 文件解析 → Embedding → 写入 ES |
| 4 | Agent 配置创建/更新 | HTTP POST | 前端 → Config Service → Services → PostgreSQL → 返回配置 |
| 5 | MCP 工具调用 | Agent 运行时 | CoreAgent → ToolCollection → MCP Server（SSE/HTTP） → 返回工具结果 |

---

## 数据流 1：Agent 对话执行（核心流程）

### 触发方式
用户在前端聊天界面发送消息，通过 WebSocket 连接到 Runtime Service。

### 流转路径

```text
[前端 WS 消息] → [runtime_app.py: POST /agent/run] → [agent_service.py: run_agent()]
    → [create_agent_info.py: create_agent_run_info()] → [AgentRunManager.register()]
    → [run_agent.py: agent_run()] → [Thread: agent_run_thread()]
    → [nexent_agent.py: NexentAgent.create_single_agent()] → [CoreAgent.run()]
    → [core_agent.py: _step_stream()] → [openai_llm.py: model()] → [LLM 推理]
    → [代码解析 + 工具执行] → [MessageObserver 缓存消息]
    → [agent_run() 轮询 observer] → [SSE 流式推送前端]
```

### 各步骤详解

| 步骤 | 模块/函数 | 输入 | 处理逻辑 | 输出 |
|------|-----------|------|----------|------|
| 1 | `apps/agent_app.py` → `agent_runtime_router` | WebSocket 消息（query, agent_id, history, files） | 解析请求参数，获取 tenant_id/user_id | 调用 agent_service |
| 2 | `services/agent_service.py: run_agent()` | agent_id, query, history, minio_files, tenant_id, user_id | 组装 AgentRunInfo 并注册到 AgentRunManager | AgentRunInfo 对象 |
| 3 | `agents/create_agent_info.py: create_agent_run_info()` | agent_id, tenant_id, query 等 | 从 DB 加载模型/Agent/工具配置，构建 AgentConfig；查询 MCP Server 列表；检索记忆 | `AgentRunInfo` (含 observer, config, stop_event) |
| 4 | `agents/create_agent_info.py: create_agent_config()` | agent_id, tenant_id, user_id | 递归构建子 Agent 配置；组装系统提示词（duty + constraint + few_shots + tools + memory + knowledge_base_summary） | `AgentConfig` |
| 5 | `sdk/nexent/core/agents/run_agent.py: agent_run_thread()` | AgentRunInfo | 创建 NexentAgent → create_single_agent → set_agent → agent_run_with_observer | 流式消息通过 observer |
| 6 | `sdk/nexent/core/agents/nexent_agent.py: create_single_agent()` | AgentConfig | 工厂模式创建模型实例 + 工具实例 + 子 Agent 实例 | `CoreAgent` 实例 |
| 7 | `sdk/nexent/core/agents/core_agent.py: run()` → `_run_stream()` | task (用户查询) | ReAct 循环：模型推理 → 解析代码 → 执行 → 观察结果 → 循环或输出最终答案 | ActionStep / FinalAnswerStep |
| 8 | `sdk/nexent/core/agents/core_agent.py: _step_stream()` | memory_messages | 发送历史消息到 LLM → 解析模型输出为可执行代码 → Python 执行器运行 → 收集输出 | ActionOutput |
| 9 | `sdk/nexent/core/utils/observer.py: add_message()` | agent_name, process_type, content | 将消息写入缓存队列 | 消息入队 |
| 10 | `run_agent.py: agent_run()` | AgentRunInfo | 在独立线程中运行 Agent，主线程轮询 observer 缓存并通过 SSE yield 消息 | SSE 流式消息 |

### 数据格式变化

1. **前端请求**：JSON（query + agent_id + history[] + files[]）
2. **内部组装**：`AgentRunInfo`（Pydantic Model），含 `AgentConfig`（嵌套的树形配置）
3. **LLM 调用**：`List[ChatMessage]`（smolagents 消息格式）
4. **LLM 输出**：文本（含 `<RUN>` 代码块或自然语言）
5. **工具执行**：Python 执行器输出（stdout）
6. **流式响应**：SSE 事件流（ProcessType 标记消息类型：PARSE / EXECUTION_LOGS / FINAL_ANSWER / ERROR）

### 错误处理

- **LLM 调用失败**：`AgentGenerationError`，记入 ActionStep.error
- **代码执行失败**：`AgentExecutionError`，记录执行日志
- **最大步数溢出**：`AgentMaxStepsError`，调用 `_handle_max_steps_reached()` 返回已收集信息
- **用户中断**：`stop_event.set()` → Agent 循环检测到后退出
- **MCP 连接失败**：`"Couldn't connect to the MCP server"` → 返回友好错误消息
- **记忆检索失败**：在 `create_agent_config()` 中捕获，向上抛出以在流式层发出 `<MEM_FAILED>` 标记

---

## 数据流 2：知识库检索

### 触发方式
Agent 运行过程中调用 `KnowledgeBaseSearchTool`。

### 流转路径

```text
[CoreAgent 工具调用] → [KnowledgeBaseSearchTool.forward()]
    → [VectorDatabaseCore.hybrid_search()] → [ElasticsearchCore.hybrid_search()]
    → [ES 准确匹配 + 向量相似度搜索] → [结果合并排序] → [返回 top_k 文档]
```

### 各步骤详解

| 步骤 | 模块/函数 | 输入 | 处理逻辑 | 输出 |
|------|-----------|------|----------|------|
| 1 | `core/tools/knowledge_base_search_tool.py: forward()` | query, index_names | 构建搜索请求 | — |
| 2 | `vector_database/elasticsearch_core.py: hybrid_search()` | index_names, query_text, embedding_model, top_k | 计算查询向量；并行执行 accurate_search + semantic_search；加权合并 | 排序后的文档列表 |
| 3 | `vector_database/elasticsearch_core.py: semantic_search()` | index_names, query_text, embedding_model | 将 query 向量化 → ES knn 搜索 | 带分数的文档列表 |
| 4 | `vector_database/elasticsearch_core.py: accurate_search()` | index_names, query_text | ES match 查询（全文匹配） | 带分数的文档列表 |

### 数据格式变化

1. **输入**：自然语言查询字符串 + 索引名列表
2. **向量化**：通过 `BaseEmbedding` 模型转为 float 向量
3. **ES 查询**：knn + match 组合查询 DSL
4. **输出**：`List[Dict]`，每个 Dict 包含 content、score、metadata

---

## 数据流 3：文件上传与向量化

### 触发方式
用户在前端上传文件到知识库。

### 流转路径

```text
[前端上传文件] → [Config Service: POST /file/upload] → [MinIO 存储]
    → [Data Process Service: POST /tasks/process] → [Ray Worker]
    → [文件解析(Unstructured)] → [文本分块] → [Embedding 向量化]
    → [ElasticsearchCore.vectorize_documents()] → [写入 ES 索引]
```

### 各步骤详解

| 步骤 | 模块/函数 | 输入 | 处理逻辑 | 输出 |
|------|-----------|------|----------|------|
| 1 | `services/file_management_service.py` | 文件流 + tenant_id | 上传到 MinIO | MinIO URL |
| 2 | `services/data_process_service.py` | 文件 URL + 索引名 | 提交处理任务到 Data Process Service | task_id |
| 3 | `data_process/tasks.py` | 文件 URL + 参数 | 通过 Celery/Ray 分发任务 | Worker 任务 |
| 4 | `data_process/worker.py` + `data_process/ray_actors.py` | 文件内容 | 使用 Unstructured 解析（支持 PDF/DOCX/PPTX/XLSX 等 20+ 格式） | 结构化文本 |
| 5 | 文本分块 + Embedding | 解析后文本 | 按策略分块 → 调用 Embedding 模型生成向量 | (chunk, vector) 列表 |
| 6 | `vector_database/elasticsearch_core.py: vectorize_documents()` | 文档列表 + embedding_model | 批量向 ES 写入文档（含向量字段） | 索引文档数 |

---

## 数据流 4：Agent 配置创建/更新

### 触发方式
用户在前端 Agent 管理页面创建或编辑 Agent。

### 流转路径

```text
[前端操作] → [Config Service: POST /agent/create 或 PUT /agent/update]
    → [agent_app.py] → [agent_service.py] → [agent_db.py]
    → [PostgreSQL: ag_tenant_agent_t] → [返回 Agent 配置]
```

### 各步骤详解

| 步骤 | 模块/函数 | 输入 | 处理逻辑 | 输出 |
|------|-----------|------|----------|------|
| 1 | `apps/agent_app.py` | JSON（Agent 配置数据） | 参数校验 | 调用 service |
| 2 | `services/agent_service.py` | Agent 配置 DTO | 业务校验（名称唯一性等） | 调用 DB |
| 3 | `database/agent_db.py` | SQL 参数 | SQLAlchemy ORM 写入/更新 | 数据库记录 |
| 4 | `database/agent_version_db.py` | 版本号 | 管理版本快照 | 版本记录 |

---

## 数据流 5：MCP 工具调用

### 触发方式
Agent 运行时通过 `ToolCollection` 连接 MCP Server 并调用工具。

### 流转路径

```text
[CoreAgent 工具调用] → [smolagents ToolCollection.from_mcp()]
    → [MCP Server (SSE/StreamableHTTP)] → [工具执行] → [返回结果]
```

### 各步骤详解

| 步骤 | 模块/函数 | 输入 | 处理逻辑 | 输出 |
|------|-----------|------|----------|------|
| 1 | `run_agent.py: agent_run_thread()` | mcp_host 列表 | 规范化 MCP 配置（URL → dict + transport 检测） | 规范化后的 MCP 配置 |
| 2 | `smolagents ToolCollection.from_mcp()` | MCP Server URL + transport | 建立 SSE/StreamableHTTP 连接，发现可用工具 | ToolCollection 实例 |
| 3 | CoreAgent 执行 | 工具名 + 参数 | 通过 MCP 协议调用远程工具 | 工具执行结果 |

---

## 跨数据流的共享组件

| 组件 | 使用的数据流 | 职责 |
|------|-------------|------|
| `MessageObserver` | Agent 对话执行 | 消息缓存和流式推送 |
| `AgentRunManager` | Agent 对话执行 | 管理活跃 Agent 运行实例 |
| `ElasticsearchCore` | 知识库检索、文件向量化 | 向量数据库操作 |
| `MinIOStorageClient` | 文件上传、Agent 对话（文件引用） | 对象存储读写 |
| `OpenAIModel` | Agent 对话执行、知识库向量化 | LLM/Embedding 推理 |
| `TenantConfigManager` | Agent 对话执行、配置管理 | 租户级配置读取 |
| `AuthUtils` | 所有数据流 | 用户认证和权限校验 |
| `AppFactory` | 所有后端服务 | FastAPI 应用创建和异常处理 |
