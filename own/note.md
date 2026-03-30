# 代码结构：Nexent

## 顶层目录一览

| 路径 | 类型 | 用途说明 |
|------|------|----------|
| `backend/` | 源码 | 后端服务（FastAPI），包含 API 层、服务层、数据层、Agent 管理 |
| `sdk/` | 源码 | SDK 核心库（`nexent` 包），Agent 框架、模型、工具、记忆等 |
| `frontend/` | 源码 | 前端应用（Next.js + React + TypeScript） |
| `docker/` | 配置 | Docker Compose 部署文件、环境变量模板、部署脚本 |
| `k8s/` | 配置 | Kubernetes Helm Charts，用于生产级部署 |
| `make/` | 构建 | 各服务的 Dockerfile（main/web/data_process/mcp/terminal） |
| `test/` | 测试 | pytest 测试套件，覆盖 backend 和 SDK |
| `doc/` | 文档 | VitePress 文档站点源码 |
| `assets/` | 资源 | README 图片资源 |
| `dive-into-code/` | 工具 | 代码分析 Skill 资源文件 |

## 核心源码目录详解

### `backend/`
> 职责：后端微服务，分为多个独立运行的 FastAPI 服务进程

| 文件 | 用途 |
|------|------|
| `config_service.py` | Config Service 入口，端口 5010，管理所有配置相关 API |
| `runtime_service.py` | Runtime Service 入口，端口 5014，处理 Agent 运行和对话 |
| `mcp_service.py` | MCP Service 入口，端口 5011，MCP 工具协议代理 |
| `northbound_service.py` | Northbound Service 入口，端口 5013，对外北向 API |
| `data_process_service.py` | Data Process Service 入口，端口 5012，文件处理和向量化 |

### `backend/apps/`
> 职责：HTTP API 端点层（FastAPI Router），负责参数校验和调用 Services

| 文件 | 用途 |
|------|------|
| `app_factory.py` | FastAPI 应用工厂，统一 CORS、异常处理、监控中间件 |
| `config_app.py` | Config Service 主路由，聚合所有配置子路由 |
| `runtime_app.py` | Runtime Service 主路由，包含对话和 Agent 运行端点 |
| `agent_app.py` | Agent 管理 API（配置端点 + 运行端点） |
| `conversation_management_app.py` | 对话管理 API（创建/删除/历史查询） |
| `model_managment_app.py` | 模型管理 API（LLM 模型增删改查） |
| `prompt_app.py` | 提示词管理 API |
| `skill_app.py` | 技能管理 API |
| `remote_mcp_app.py` | 远程 MCP 服务器管理 API |
| `tool_config_app.py` | 工具配置 API |
| `knowledge_summary_app.py` | 知识库摘要 API |
| `vectordatabase_app.py` | 向量数据库管理 API |
| `file_management_app.py` | 文件管理 API |
| `user_management_app.py` | 用户管理 API |
| `tenant_app.py` | 租户管理 API |
| `group_app.py` | 分组管理 API |
| `voice_app.py` | 语音服务 API |
| `memory_config_app.py` | 记忆配置 API |
| `image_app.py` | 图片代理 API |
| `tenant_config_app.py` | 租户配置 API |
| `config_sync_app.py` | 配置同步 API |
| `datamate_app.py` | DataMate 集成 API |
| `dify_app.py` | Dify 集成 API |
| `idata_app.py` | iData 集成 API |
| `invitation_app.py` | 邀请码管理 API |
| `user_app.py` | 用户信息 API |
| `mock_user_management_app.py` | 速度模式下的模拟用户管理 |

### `backend/services/`
> 职责：核心业务逻辑层，不涉及 HTTP 关注点

| 文件 | 用途 |
|------|------|
| `agent_service.py` | Agent 运行业务逻辑，协调 create_agent_info 和 AgentRunManager |
| `agent_version_service.py` | Agent 版本管理（发布/回滚） |
| `conversation_management_service.py` | 对话管理业务逻辑 |
| `model_management_service.py` | 模型管理业务逻辑 |
| `model_provider_service.py` | 模型提供商管理 |
| `model_health_service.py` | 模型健康检查 |
| `prompt_service.py` | 提示词管理 |
| `skill_service.py` | 技能管理 |
| `remote_mcp_service.py` | 远程 MCP 服务器管理 |
| `mcp_container_service.py` | MCP 容器编排（Docker/K8s） |
| `tool_configuration_service.py` | 工具配置管理 |
| `file_management_service.py` | 文件上传/下载/管理 |
| `data_process_service.py` | 数据处理任务调度 |
| `vectordatabase_service.py` | 向量数据库服务（ES 核心实例化） |
| `memory_config_service.py` | 记忆配置管理 |
| `tenant_service.py` | 租户管理 |
| `user_service.py` | 用户服务 |
| `user_management_service.py` | 用户认证管理（Supabase） |
| `redis_service.py` | Redis 客户端封装 |
| `config_sync_service.py` | 配置同步 |
| `image_service.py` | 图片服务 |
| `voice_service.py` | 语音服务 |
| `invitation_service.py` | 邀请码服务 |
| `group_service.py` | 分组服务 |
| `datamate_service.py` | DataMate 服务 |
| `dify_service.py` | Dify 服务 |
| `idata_service.py` | iData 服务 |
| `northbound_service.py` | 北向 API 业务逻辑 |

### `backend/agents/`
> 职责：Agent 运行管理

| 文件 | 用途 |
|------|------|
| `agent_run_manager.py` | 单例，管理活跃的 Agent 运行实例（注册/注销/停止） |
| `create_agent_info.py` | 构建 AgentRunInfo（从 DB 加载配置 → 组装 AgentConfig + ModelConfig + ToolConfig） |
| `preprocess_manager.py` | 预处理管理器 |

### `backend/database/`
> 职责：数据访问层（SQLAlchemy ORM）

| 文件 | 用途 |
|------|------|
| `client.py` | 数据库连接、MinIO 客户端、ES 客户端初始化 |
| `db_models.py` | SQLAlchemy ORM 模型定义 |
| `agent_db.py` | Agent 数据操作 |
| `agent_version_db.py` | Agent 版本数据操作 |
| `conversation_db.py` | 对话数据操作 |
| `knowledge_db.py` | 知识库数据操作 |
| `model_management_db.py` | 模型管理数据操作 |
| `tool_db.py` | 工具配置数据操作 |
| `user_tenant_db.py` | 用户-租户关系 |
| `remote_mcp_db.py` | MCP 服务器记录 |
| `skill_db.py` | 技能数据操作 |
| `memory_config_db.py` | 记忆配置数据操作 |
| `tenant_config_db.py` | 租户配置数据操作 |
| `group_db.py` | 分组数据操作 |
| `invitation_db.py` | 邀请码数据操作 |
| `attachment_db.py` | 附件数据操作 |
| `token_db.py` | Token 数据操作 |
| `partner_db.py` | 合作伙伴数据操作 |
| `role_permission_db.py` | 角色权限数据操作 |
| `utils.py` | 数据库工具函数 |

### `backend/consts/`
> 职责：常量、环境变量、异常定义

| 文件 | 用途 |
|------|------|
| `const.py` | 环境变量统一管理（单一真相源） |
| `exceptions.py` | 异常类定义（AppException + Legacy 异常） |
| `error_code.py` | 错误码枚举和 HTTP 状态映射 |
| `error_message.py` | 错误消息模板 |
| `model.py` | Pydantic 数据模型 |
| `provider.py` | 模型提供商常量 |

### `backend/utils/`
> 职责：通用工具函数

| 文件 | 用途 |
|------|------|
| `auth_utils.py` | 认证工具（JWT 解析、用户身份获取） |
| `llm_utils.py` | LLM 调用工具 |
| `config_utils.py` | 配置管理工具（租户配置管理器） |
| `logging_utils.py` | 日志配置工具 |
| `document_vector_utils.py` | 文档向量化工具 |
| `file_management_utils.py` | 文件管理工具 |
| `langchain_utils.py` | LangChain 工具自动发现 |
| `memory_utils.py` | 记忆工具 |
| `model_name_utils.py` | 模型名称处理 |
| `monitoring.py` | 监控工具（OpenTelemetry） |
| `prompt_template_utils.py` | 提示词模板加载 |
| `skill_params_utils.py` | 技能参数工具 |
| `str_utils.py` | 字符串工具 |
| `task_status_utils.py` | 任务状态工具 |
| `thread_utils.py` | 线程工具 |
| `tool_utils.py` | 工具通用工具 |

### `backend/data_process/`
> 职责：数据处理引擎（Ray + Celery）

| 文件 | 用途 |
|------|------|
| `app.py` | Celery 应用配置和任务路由 |
| `tasks.py` | 处理任务定义（process/forward/process_and_forward） |
| `worker.py` | Worker 初始化（Ray 集群 + Celery Worker） |
| `ray_config.py` | Ray 集群配置 |
| `ray_actors.py` | Ray Actor（文件解析执行器） |
| `utils.py` | 数据处理工具函数 |

### `backend/prompts/`
> 职责：YAML 提示词模板

| 文件 | 用途 |
|------|------|
| `managed_system_prompt_template_en.yaml` | 托管 Agent 系统提示词模板（英文） |
| `managed_system_prompt_template_zh.yaml` | 托管 Agent 系统提示词模板（中文） |
| `manager_system_prompt_template_en.yaml` | 管理者 Agent 系统提示词模板（英文） |
| `manager_system_prompt_template_zh.yaml` | 管理者 Agent 系统提示词模板（中文） |
| `cluster_summary_reduce_en.yaml` | 聚类摘要提示词（英文） |
| `cluster_summary_reduce_zh.yaml` | 聚类摘要提示词（中文） |
| `document_summary_agent_en.yaml` | 文档摘要提示词（英文） |
| `document_summary_agent_zh.yaml` | 文档摘要提示词（中文） |

### `sdk/nexent/core/`
> 职责：SDK 核心模块（Agent、模型、工具）

| 子目录 | 用途 |
|--------|------|
| `agents/` | Agent 核心（NexentAgent 工厂、CoreAgent 执行引擎、模型/工具配置） |
| `models/` | 模型封装（OpenAI LLM、Embedding、VLM、STT、TTS） |
| `tools/` | 内置工具集（30+ 工具：搜索、文件操作、邮件、知识库、技能等） |
| `utils/` | 工具类（Observer、常量、提示词模板、Favicon 提取） |
| `nlp/` | NLP 工具（分词、停用词） |

### `sdk/nexent/memory/`
> 职责：记忆系统（基于 mem0）

| 文件 | 用途 |
|------|------|
| `memory_core.py` | mem0 AsyncMemory 实例管理（进程内缓存 + 配置哈希） |
| `memory_service.py` | 记忆搜索服务（多级检索：tenant/agent/user/user_agent） |
| `embedder_adaptor.py` | Embedding 适配器（将 SDK Embedding 适配为 mem0 接口） |
| `memory_utils.py` | 记忆工具函数 |

### `sdk/nexent/vector_database/`
> 职责：向量数据库抽象层

| 文件 | 用途 |
|------|------|
| `base.py` | 抽象基类 `VectorDatabaseCore`（索引管理、文档操作、搜索） |
| `elasticsearch_core.py` | Elasticsearch 实现（准确搜索 + 语义搜索 + 混合搜索） |
| `datamate_core.py` | DataMate 向量数据库实现 |
| `utils.py` | 工具函数 |

### `sdk/nexent/storage/`
> 职责：对象存储抽象层

| 文件 | 用途 |
|------|------|
| `storage_client_base.py` | 抽象基类 `StorageClient` |
| `storage_client_factory.py` | 存储客户端工厂 |
| `minio.py` | MinIO 实现 |
| `minio_config.py` | MinIO 配置类 |

### `sdk/nexent/skills/`
> 职责：技能系统

| 文件 | 用途 |
|------|------|
| `skill_manager.py` | 技能管理器（加载/保存/上传/删除/执行技能脚本） |
| `skill_loader.py` | SKILL.md 解析器（frontmatter + body） |
| `constants.py` | 常量定义 |

### `frontend/`
> 职责：Next.js 前端应用

| 目录 | 用途 |
|------|------|
| `app/[locale]/` | 国际化页面路由（chat、agents、knowledges、market、memory、models 等） |
| `components/` | React 组件（agent、auth、mcp、navigation、permission、tool-config、ui） |
| `services/` | API 客户端（agentConfigService、conversationService、knowledgeBaseService 等 17 个服务） |
| `stores/` | Zustand 状态管理（agentConfigStore） |
| `hooks/` | 自定义 Hooks（agent、auth、chat、knowledge、mcp、memory、model、tool 等） |
| `types/` | TypeScript 类型定义（agentConfig、chat、auth、knowledgeBase、memory、skill） |
| `const/` | 前端常量配置 |
| `lib/` | 工具库（auth、chat、logger、language 等） |

## 关键文件说明

### `backend/agents/create_agent_info.py`
- **作用**：构建 Agent 运行所需的全部信息
- **主要函数**：`create_agent_run_info()`, `create_agent_config()`, `create_tool_config_list()`
- **与其他文件的关系**：被 `agent_service.py` 调用，依赖几乎所有 services 和 database 模块

### `sdk/nexent/core/agents/core_agent.py`
- **作用**：Agent 执行引擎，基于 smolagents CodeAgent 的 ReAct 循环
- **主要类/函数**：`CoreAgent`, `run()`, `_step_stream()`, `_run_stream()`
- **与其他文件的关系**：被 `nexent_agent.py` 包装使用，依赖 observer、openai_llm

### `sdk/nexent/core/agents/run_agent.py`
- **作用**：Agent 运行入口，管理 MCP 连接和流式输出
- **主要函数**：`agent_run()`, `agent_run_thread()`
- **与其他文件的关系**：被 `agent_service.py` 调用

### `backend/apps/app_factory.py`
- **作用**：FastAPI 应用工厂，统一 CORS、异常处理、监控
- **与其他文件的关系**：被所有 Service 入口使用

### `sdk/nexent/vector_database/base.py`
- **作用**：向量数据库抽象基类，定义统一的索引/文档/搜索接口
- **与其他文件的关系**：被 `elasticsearch_core.py` 实现，被 `vectordatabase_service.py` 使用

## 入口点

- **Config Service 主入口**：`backend/config_service.py`（端口 5010）
- **Runtime Service 主入口**：`backend/runtime_service.py`（端口 5014）
- **MCP Service 主入口**：`backend/mcp_service.py`（端口 5011）
- **Northbound Service 主入口**：`backend/northbound_service.py`（端口 5013）
- **Data Process Service 主入口**：`backend/data_process_service.py`（端口 5012）
- **前端 Web 入口**：`frontend/server.js`（端口 3000）
- **SDK 公开 API 入口**：`sdk/nexent/__init__.py`
- **部署入口**：`docker/deploy.sh`

## 注

本项目不涉及科学/算法内容（无 NLP 模型训练、无数学优化算法、无信号处理等），因此不生成 `sci.md`。
