# 最小使用示例：Nexent

## 快速开始

### 环境准备

```bash
# 克隆仓库
git clone https://github.com/ModelEngine-Group/nexent.git
cd nexent/docker

# 配置环境变量
cp .env.example .env
# 编辑 .env，填写必要的配置（LLM API Key 等）

# 一键部署
bash deploy.sh
```

部署完成后访问 **http://localhost:3000**，按照设置向导完成初始化。

---

## 示例 1：使用 Docker Compose 部署完整平台

**功能说明**：一键启动 Nexent 全栈服务（后端 + 前端 + 数据库 + 中间件）。

**前置条件**：Docker & Docker Compose 已安装，至少 2 核 CPU / 6GB RAM。

```bash
# 1. 克隆项目
git clone https://github.com/ModelEngine-Group/nexent.git
cd nexent/docker

# 2. 创建环境变量文件
cp .env.example .env

# 3. 编辑必要配置（至少填写 LLM 相关配置）
# ELASTIC_PASSWORD, MINIO 配置等使用默认值即可
vi .env

# 4. 启动所有服务
bash deploy.sh
```

**预期输出**：

```
✓ nexent-postgresql    running  (port 5434)
✓ nexent-elasticsearch running  (port 9210)
✓ nexent-redis         running  (port 6379)
✓ nexent-minio         running  (port 9010)
✓ nexent-config        running  (port 5010)
✓ nexent-runtime       running  (port 5014)
✓ nexent-mcp           running  (port 5011)
✓ nexent-northbound    running  (port 5013)
✓ nexent-data-process  running  (port 5012)
✓ nexent-web           running  (port 3000)
```

**关键参数说明**：

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `ELASTIC_PASSWORD` | Elasticsearch 密码 | `nexent@2025` |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | MinIO 访问凭证 | — |
| `POSTGRES_HOST` | PostgreSQL 地址 | `nexent-postgresql` |

---

## 示例 2：使用 SDK 创建并运行一个 Agent

**功能说明**：通过 Python SDK 编程式创建 Agent 并执行查询。

**前置条件**：安装 nexent SDK 包（`pip install -e sdk/`），配置好 LLM API。

```python
from nexent.core.agents import NexentAgent, CoreAgent
from nexent.core.agents.agent_model import (
    AgentConfig, ModelConfig, ToolConfig
)
from nexent.core.utils.observer import MessageObserver
from threading import Event

# 1. 配置模型
model_config = ModelConfig(
    cite_name="main_model",
    api_key="your-api-key",
    model_name="gpt-4o",
    url="https://api.openai.com/v1"
)

# 2. 配置工具（可选：使用 Tavily 搜索）
tool_config = ToolConfig(
    class_name="TavilySearchTool",
    name="web_search",
    description="Search the web for information",
    inputs='{"query": "str"}',
    output_type="string",
    params={"api_key": "your-tavily-key"},
    source="local"
)

# 3. 配置 Agent
agent_config = AgentConfig(
    name="my_agent",
    description="A simple web search agent",
    tools=[tool_config],
    max_steps=5,
    model_name="main_model"
)

# 4. 创建并运行 Agent
observer = MessageObserver(lang="en")
agent = NexentAgent(
    observer=observer,
    model_config_list=[model_config],
    stop_event=Event()
)
core_agent = agent.create_single_agent(agent_config)
agent.set_agent(core_agent)
agent.agent_run_with_observer("What is the weather in Beijing today?")
```

**预期输出**：

Agent 将通过 MessageObserver 流式输出执行过程：
1. `PROCESS_TYPE.PARSE` — 解析的代码
2. `PROCESS_TYPE.EXECUTION_LOGS` — 工具执行日志
3. `PROCESS_TYPE.FINAL_ANSWER` — 最终回答

**关键参数说明**：

| 参数 | 类型 | 说明 | 默认值 |
|------|------|------|--------|
| `max_steps` | int | Agent 最大执行步数 | 5 |
| `temperature` | float | LLM 温度 | 0.1 |
| `top_p` | float | Top-P 采样 | 0.95 |
| `provide_run_summary` | bool | 是否向父 Agent 提供执行摘要 | False |

---

## 示例 3：使用 MCP 工具扩展 Agent

**功能说明**：通过 MCP 协议连接外部工具服务器，扩展 Agent 能力。

**前置条件**：有一个运行中的 MCP Server（支持 SSE 或 StreamableHTTP）。

```python
from smolagents import ToolCollection
from nexent.core.agents import NexentAgent
from nexent.core.agents.agent_model import AgentConfig, ModelConfig, ToolConfig
from nexent.core.utils.observer import MessageObserver
from threading import Event

# MCP 服务器配置
mcp_host = [
    # SSE 方式
    "http://localhost:8080/sse",
    # 或 StreamableHTTP 方式（带认证）
    {
        "url": "http://localhost:8081/mcp",
        "transport": "streamable-http",
        "authorization": "Bearer your-token"
    }
]

# 连接 MCP Server 并创建 Agent
observer = MessageObserver(lang="en")
model_config = ModelConfig(
    cite_name="main_model",
    api_key="your-api-key",
    model_name="gpt-4o",
    url="https://api.openai.com/v1"
)

with ToolCollection.from_mcp(mcp_host, trust_remote_code=True) as tools:
    # 工具会自动发现，在 agent_config 中引用即可
    tool_names = [t.name for t in tools.tools]
    print(f"Discovered MCP tools: {tool_names}")

    # 配置使用 MCP 工具
    mcp_tool_configs = [
        ToolConfig(
            class_name=name,
            name=name,
            source="mcp"
        ) for name in tool_names
    ]

    agent_config = AgentConfig(
        name="mcp_agent",
        description="Agent with MCP tools",
        tools=mcp_tool_configs,
        max_steps=10,
        model_name="main_model"
    )

    agent = NexentAgent(
        observer=observer,
        model_config_list=[model_config],
        stop_event=Event(),
        mcp_tool_collection=tools
    )
    core_agent = agent.create_single_agent(agent_config)
    agent.set_agent(core_agent)
    agent.agent_run_with_observer("Use the available tools to help me")
```

**预期输出**：

```
Discovered MCP tools: ['tool_a', 'tool_b', ...]
```

**关键参数说明**：

| 参数 | 类型 | 说明 | 默认值 |
|------|------|------|--------|
| `mcp_host` | list | MCP 服务器地址列表，支持 str 或 dict | — |
| `transport` | str | `"sse"` 或 `"streamable-http"` | 自动检测 |
| `authorization` | str | Bearer token 认证头 | None |
| `trust_remote_code` | bool | 是否信任远程代码 | True |

---

## 示例 4：知识库创建与检索

**功能说明**：创建向量索引、上传文档、执行语义搜索。

**前置条件**：Elasticsearch 服务可用，Embedding 模型已配置。

```python
from nexent.vector_database import ElasticsearchCore
from nexent.core.models.embedding_model import OpenAIEmbedding

# 1. 创建向量数据库核心
es_core = ElasticsearchCore(
    host="http://localhost:9200",
    elastic_password="nexent@2025"
)

# 2. 创建 Embedding 模型
embedding = OpenAIEmbedding(
    model_name="text-embedding-3-small",
    api_key="your-api-key",
    base_url="https://api.openai.com/v1"
)

# 3. 创建索引
es_core.create_index("my_knowledge_base", embedding_dim=1536)

# 4. 向量化文档
documents = [
    {"content": "Nexent is an AI agent platform.", "source": "intro.txt"},
    {"content": "It supports 20+ file formats for processing.", "source": "features.txt"},
]
count = es_core.vectorize_documents(
    index_name="my_knowledge_base",
    embedding_model=embedding,
    documents=documents
)
print(f"Indexed {count} documents")

# 5. 搜索
results = es_core.semantic_search(
    index_names=["my_knowledge_base"],
    query_text="What file formats does Nexent support?",
    embedding_model=embedding,
    top_k=3
)
for r in results:
    print(f"Score: {r.get('score')}, Content: {r.get('content')}")
```

**预期输出**：

```
Indexed 2 documents
Score: 0.89, Content: It supports 20+ file formats for processing.
Score: 0.72, Content: Nexent is an AI agent platform.
```

**关键参数说明**：

| 参数 | 类型 | 说明 | 默认值 |
|------|------|------|--------|
| `embedding_dim` | int | 向量维度 | 由模型决定 |
| `batch_size` | int | 批量处理大小 | 64 |
| `top_k` | int | 返回结果数 | 5 |
| `weight_accurate` | float | 混合搜索中准确匹配权重 | 0.3 |

---

## 示例 5：使用技能（Skills）系统

**功能说明**：创建、加载和执行自定义技能。

**前置条件**：技能目录已配置（`SKILLS_PATH`）。

```python
from nexent.skills import SkillManager

# 1. 初始化技能管理器
manager = SkillManager(local_skills_dir="/mnt/nexent/skills")

# 2. 创建技能
skill_data = {
    "name": "data_analyzer",
    "description": "Analyze CSV data and generate reports",
    "tags": ["data", "analysis"],
    "content": "This skill analyzes CSV files and produces summary reports..."
}
manager.save_skill(skill_data)

# 3. 列出所有技能
skills = manager.list_skills()
for s in skills:
    print(f"  {s['name']}: {s['description']}")

# 4. 执行技能脚本
result = manager.run_skill_script(
    skill_name="data_analyzer",
    script_path="scripts/analyze.py",
    params={"--input": "data.csv", "--format": "json"}
)
print(result)

# 5. 生成技能摘要（用于 Agent 提示词注入）
summary = manager.build_skills_summary()
print(summary)
```

**预期输出**：

```
  data_analyzer: Analyze CSV data and generate reports
{"total_rows": 1000, "columns": ["name", "age", "city"]}
<skills>
  <skill>
    <name>data_analyzer</name>
    <description>Analyze CSV data and generate reports</description>
  </skill>
</skills>
```

**关键参数说明**：

| 参数 | 类型 | 说明 | 默认值 |
|------|------|------|--------|
| `local_skills_dir` | str | 技能存储目录 | — |
| `file_type` | str | 上传文件类型（`"auto"`/`"md"`/`"zip"`） | `"auto"` |

---

## 常见使用模式

### 多 Agent 协同（管理者 + 子 Agent）
通过 `AgentConfig.managed_agents` 配置子 Agent 列表，形成树形协作结构。管理者 Agent 负责任务分发和结果汇总。

### 混合搜索（关键词 + 语义）
使用 `VectorDatabaseCore.hybrid_search()` 方法，通过 `weight_accurate` 参数调节关键词匹配和语义搜索的权重比例。

### 流式输出
Agent 运行时通过 `MessageObserver` 缓存消息，由 `agent_run()` 异步生成器以 SSE 形式推送给前端。

---

## 常见问题

**Q1: Agent 执行超时怎么办？**
- 检查 `max_steps` 是否足够（默认 5）
- 检查 LLM API 是否可达
- 查看 `stop_event` 是否被意外触发

**Q2: 知识库搜索没有结果？**
- 确认文档已成功向量化（检查 ES 索引文档数）
- 尝试降低 `top_k` 的阈值或使用混合搜索
- 检查 Embedding 模型与文档向量化时使用的模型是否一致

**Q3: MCP 工具连接失败？**
- 确认 MCP Server URL 的 transport 类型是否正确（`/sse` → SSE，`/mcp` → StreamableHTTP）
- 检查网络连通性和认证 token
- 查看 MCP Service 日志获取详细错误信息
