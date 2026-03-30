# 开发指南：Nexent

## 环境搭建

### 系统要求

| 资源 | 最低要求 | 推荐配置 |
|------|----------|----------|
| CPU | 2 核 | 4 核 |
| RAM | 6 GiB | 16 GiB |
| 磁盘 | 20 GB | 50 GB+ |
| Python | 3.10.x | 3.10.x |
| Node.js | 20.17.0+ | 20.x LTS |
| Docker | 20.x+ | 24.x+ |
| Docker Compose | v2+ | v2+ |

### 依赖安装步骤

#### 方式一：Docker Compose 部署（推荐）

```bash
# 1. 克隆仓库
git clone https://github.com/ModelEngine-Group/nexent.git
cd nexent

# 2. 配置环境变量
cd docker
cp .env.example .env
# 编辑 .env，填写 LLM API Key 等必要配置

# 3. 一键部署
bash deploy.sh
```

#### 方式二：从源码构建

**后端：**

```bash
cd backend

# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows

# 安装依赖
pip install -e .

# 安装数据处理依赖（可选）
pip install -e ".[data-process]"

# 安装开发依赖（包含测试）
pip install -e ".[test]"
```

**SDK：**

```bash
cd sdk

pip install -e .
# 安装开发/质量检查工具
pip install -e ".[quality]"
```

**前端：**

```bash
cd frontend

# 安装 pnpm（如未安装）
npm install -g pnpm

# 安装依赖
pnpm install

# 开发模式运行
pnpm dev
```

### 配置说明

核心配置文件为 `docker/.env`，主要环境变量分组：

| 配置分组 | 关键变量 | 说明 |
|----------|----------|------|
| **LLM 配置** | 通过前端设置向导配置 | 在 Web UI 中配置模型 API |
| **Elasticsearch** | `ELASTICSEARCH_HOST`, `ELASTIC_PASSWORD` | ES 连接信息 |
| **PostgreSQL** | `POSTGRES_HOST`, `POSTGRES_USER`, `NEXENT_POSTGRES_PASSWORD` | 数据库连接 |
| **MinIO** | `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY` | 对象存储 |
| **Redis** | `REDIS_URL` | 缓存/队列 |
| **服务地址** | `CONFIG_SERVICE_URL`, `RUNTIME_SERVICE_URL` 等 | 服务间通信 |
| **Supabase** | `SUPABASE_URL`, `SITE_URL` 等 | 认证服务 |
| **代理** | `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` | 网络代理（可选） |

**重要**：所有环境变量通过 `backend/consts/const.py` 统一管理，后端代码不应直接调用 `os.getenv()`。

## 本地运行

### 启动命令

**Docker 模式：**

```bash
cd docker
bash deploy.sh
# 选择部署模式：development / production
```

**源码模式（后端各服务需分别启动）：**

```bash
# Config Service（端口 5010）
cd backend && python config_service.py

# Runtime Service（端口 5014）
cd backend && python runtime_service.py

# MCP Service（端口 5011）
cd backend && python mcp_service.py

# Northbound Service（端口 5013）
cd backend && python northbound_service.py

# Data Process Service（端口 5012）
cd backend && python data_process_service.py

# 前端（端口 3000）
cd frontend && pnpm dev
```

### 访问方式

- **前端 UI**：http://localhost:3000
- **Config API**：http://localhost:5010/api/docs（Swagger 文档）
- **Runtime API**：http://localhost:5014/api/docs
- **MinIO Console**：http://localhost:9011
- **Ray Dashboard**：http://localhost:8265

### 常用开发命令

```bash
# 前端代码检查
cd frontend && pnpm lint
cd frontend && pnpm type-check
cd frontend && pnpm format:check

# 前端完整检查
cd frontend && pnpm check-all

# 后端代码风格检查
cd sdk && ruff check .
cd sdk && ruff format --check .

# 停止所有服务
cd docker && docker compose down

# 查看日志
cd docker && docker compose logs -f nexent-config
cd docker && docker compose logs -f nexent-runtime
```

## 测试

### 测试框架

- **后端 + SDK**：pytest + pytest-asyncio + pytest-cov
- **前端**：[待确认] — 项目中未见前端测试框架配置

### 运行测试

```bash
# 运行全部测试
cd test && python run_all_test.py

# 运行特定模块测试
cd test && pytest sdk/core/ -v
cd test && pytest backend/ -v

# 运行单个测试文件
pytest test/sdk/core/agents/test_core_agent.py -v

# 带覆盖率报告
pytest --cov=nexent --cov-report=html test/
```

### 测试配置

- `test/pytest.ini`：pytest 配置（asyncio_mode = auto）
- `test/.coveragerc`：覆盖率配置
- `test/conftest.py`：全局 fixtures（Mock 外部服务）

### 测试覆盖范围

| 模块 | 测试路径 | 覆盖内容 |
|------|----------|----------|
| SDK Core | `test/sdk/core/` | Agent、Models、Tools、Utils |
| SDK Memory | `test/sdk/memory/` | 记忆系统 |
| SDK Storage | `test/sdk/storage/` | MinIO 客户端 |
| SDK Vector DB | `test/sdk/vector_database/` | Elasticsearch 核心操作 |
| SDK Skills | `test/sdk/skills/` | 技能加载和管理 |
| Backend | `test/backend/` | 文档向量化、LLM 集成、运行时服务 |

## 调试技巧

### 日志位置与级别配置

```python
# 每个模块使用标准 logging
import logging
logger = logging.getLogger(__name__)

# 日志级别通过 configure_logging() 统一配置
from utils.logging_utils import configure_logging
configure_logging(logging.DEBUG)  # 开发时使用 DEBUG
```

- **Docker 环境**：通过 `docker compose logs -f <service>` 查看各服务日志
- **日志格式**：结构化日志，包含时间戳、级别、模块名
- **Elasticsearch 日志**：可通过 `configure_elasticsearch_logging()` 配置 ES 专用日志级别

### 常用调试手段

1. **Swagger UI**：访问 `http://localhost:5010/api/docs` 直接测试 API
2. **Ray Dashboard**：`http://localhost:8265` 查看数据处理任务状态
3. **Flower**：`http://localhost:5555` 查看 Celery Worker 状态
4. **MinIO Console**：`http://localhost:9011` 查看上传的文件
5. **前端 DevTools**：使用 React DevTools 和 Network 面板调试

### 已知易踩的坑

1. **环境变量**：后端代码必须通过 `consts/const.py` 读取环境变量，不要直接 `os.getenv()`
2. **SDK 不读环境变量**：SDK 模块通过参数接收配置，不直接访问环境变量
3. **异常处理**：Services 层只抛领域异常，不要在 Services 中使用 `HTTPException`
4. **Agent 版本**：调试模式使用 `version_no=0`（草稿），发布版本使用 `query_current_version_no()`
5. **MCP Transport**：URL 以 `/sse` 结尾使用 SSE transport，`/mcp` 结尾使用 StreamableHTTP
6. **ES 健康检查**：所有后端服务启动前依赖 ES 健康检查通过

## 代码规范

### 命名约定

- **Python**：snake_case（函数/变量）、PascalCase（类）
- **TypeScript**：camelCase（函数/变量）、PascalCase（类/接口/类型）
- **文件名**：snake_case（Python）、kebab-case 或 camelCase（TypeScript）
- **常量**：UPPER_SNAKE_CASE
- **路由路径**：snake_case，使用复数名词（如 `/agents`、`/tools`）

### 目录组织约定

```
backend/
├── apps/        # HTTP 端点，每个资源一个 *_app.py
├── services/    # 业务逻辑，每个资源一个 *_service.py
├── database/    # 数据访问，每个资源一个 *_db.py
├── consts/      # 常量和异常
└── utils/       # 工具函数

sdk/nexent/
├── core/        # 核心模块（agents/models/tools/utils）
├── memory/      # 记忆系统
├── vector_database/  # 向量数据库
└── ...

frontend/
├── app/         # Next.js 页面
├── components/  # React 组件
├── services/    # API 客户端
├── hooks/       # 自定义 Hooks
├── stores/      # 状态管理
└── types/       # TypeScript 类型
```

### 提交信息格式

使用 emoji 前缀的提交信息格式：

| 类型 | 前缀 | 说明 |
|------|------|------|
| Feature | ✨ | 新功能 |
| Bugfix | 🐛 | 修复 Bug |
| Docs | 📝 | 文档变更 |
| Refactor | ♻️ | 重构（不影响功能） |
| Style | 🎨 | 代码格式调整 |
| Test | 🧪 | 测试变更 |
| Chore | 🔨 | 工具/配置更新 |
| Migration | 🚚 | 文件迁移 |

示例：`✨ add user authentication`、`🐛 resolve login timeout issue`

### 代码风格工具

- **Python**：ruff（line-length: 119）
- **TypeScript**：ESLint + Prettier
- **注释语言**：必须使用英文

## 贡献流程

### 分支策略（GitFlow）

- **main**：正式发布分支，始终可部署
- **develop**：开发主线，集成本地开发的新功能
- **feature/***：从 develop 创建，完成后合并回 develop
- **release/***：发布准备分支，最终合并到 main 和 develop
- **hotfix/***：从 main 创建，紧急修复后合并到 main 和 develop

### PR / Code Review 流程

1. Fork 仓库，创建 feature 分支
2. 编写代码和测试，确保通过 CI（lint + test + build）
3. 提交 PR 到主仓库
4. 需要 **至少 2 个批准**（包括 Code Owner 批准）
5. 不能自行批准自己的 PR
6. 所有 CI 检查通过后才能合并

### 发版流程

1. 从 develop 创建 release 分支
2. 进行最终测试和小幅调整
3. 合并到 main 并打 tag
4. 同步合并回 develop
5. Docker 镜像自动构建并推送到 Docker Hub

### 监控

生产环境支持 OpenTelemetry 遥测：

```bash
# 启用遥测（在 .env 中）
ENABLE_TELEMETRY=true
JAEGER_ENDPOINT=http://localhost:14268/api/traces
PROMETHEUS_PORT=8000

# 启动监控栈
cd docker && bash start-monitoring.sh
```

监控组件：Prometheus（指标）+ Grafana（可视化）+ Jaeger（链路追踪）
