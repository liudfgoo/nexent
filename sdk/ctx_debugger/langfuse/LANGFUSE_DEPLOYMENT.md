# Langfuse 自托管部署与配置指南

> 本文档面向需要在本地或团队环境中部署 Langfuse 并配合 Nexent Benchmark / ctx_debugger 使用的同事。

---

## 1. 概述

Langfuse 是开源的 LLM 可观测性平台，Nexent 项目用它来：

1. **Benchmark 实验管理** — 数据集管理、实验运行、自动评分、跨 run 对比
2. **ctx_debugger trace 可视化** — 将 Agent 上下文压缩的 JSONL trace 导入 Langfuse，获得嵌套 trace、token/duration 分析

部署架构：

```
                    ┌─────────────────────────────────────────────────────┐
                    │           Docker Compose (langfuse project)         │
                    │                                                     │
  :3100 ──────────► │  langfuse-web (langfuse:3)                          │
                    │       ↕                                             │
                    │  langfuse-worker (langfuse-worker:3)                │
                    │       ↕           ↕            ↕          ↕         │
                    │  postgres:17   clickhouse    redis:7   minio        │
                    │  (内部网络，不暴露端口)                                │
                    └─────────────────────────────────────────────────────┘
```

- **仅 `langfuse-web` 暴露端口**：宿主机 `3100` → 容器 `3000`
- 其余组件（postgres / clickhouse / redis / minio / worker）全部在 `langfuse_default` 内部网络通信，不暴露端口
- 与 Nexent 主栈隔离（Nexent web 占用 3000 端口，Langfuse 用 3100 避免冲突）

---

## 2. 前置条件

| 依赖 | 最低版本 | 说明 |
|------|---------|------|
| Docker | 24+ | 容器运行时 |
| Docker Compose | v2+ | 编排工具 |
| 磁盘空间 | ~5 GB | 镜像 + 数据卷 |

---

## 3. 部署步骤

### 3.1 准备文件

将以下两个文件放到同一目录（例如 `langfuse/`）：

**`docker-compose.yml`** — 直接使用项目中的文件：

```
sdk/ctx_debugger/langfuse/docker-compose.yml
```

**`.env`** — 在同目录下创建，参考下方模板。

### 3.2 创建 `.env` 文件

```bash
# ============================================================
# Langfuse 自托管实例配置
# ============================================================

# --- Web UI 访问地址 ---
# 必须与浏览器实际访问的 URL 一致。
# 本地访问用 http://localhost:3100
# 局域网访问用 http://<你的IP>:3100
NEXTAUTH_URL=http://localhost:3100

# --- 实例密钥（自行生成，不要泄露） ---
# 生成方式：openssl rand -hex 32
NEXTAUTH_SECRET=<替换为你生成的随机字符串>
SALT=<替换为你生成的随机字符串>
ENCRYPTION_KEY=<替换为你生成的随机字符串>
TELEMETRY_ENABLED=false

# --- 初始化引导（首次启动自动创建组织/项目/用户） ---
# 设置后无需在 UI 中手动注册，API Key 即刻可用
LANGFUSE_INIT_ORG_ID=myorg
LANGFUSE_INIT_ORG_NAME=MyTeam
LANGFUSE_INIT_PROJECT_ID=myproject
LANGFUSE_INIT_PROJECT_NAME=nexent-context
LANGFUSE_INIT_PROJECT_PUBLIC_KEY=pk-lf-<替换为你的公钥>
LANGFUSE_INIT_PROJECT_SECRET_KEY=sk-lf-<替换为你的私钥>
LANGFUSE_INIT_USER_EMAIL=admin@example.com
LANGFUSE_INIT_USER_NAME=admin
LANGFUSE_INIT_USER_PASSWORD=<替换为你的密码>
```

#### 生成密钥的方法

```bash
# NEXTAUTH_SECRET / SALT / ENCRYPTION_KEY
openssl rand -hex 32

# API Key 格式：
#   公钥：pk-lf-<uuid>
#   私钥：sk-lf-<uuid>
# 可用 Python 生成：
python3 -c "import uuid; print(f'pk-lf-{uuid.uuid4()}'); print(f'sk-lf-{uuid.uuid4()}')"
```

> **注意**：`.env` 文件包含敏感信息，**不要提交到 Git**。项目中的 `.gitignore` 已排除 `.env`。

### 3.3 启动服务

```bash
# 在 docker-compose.yml 所在目录执行
docker compose -p langfuse up -d

# 查看启动状态
docker compose -p langfuse ps

# 查看日志（排查启动问题时使用）
docker compose -p langfuse logs -f
```

首次启动需要拉取镜像，大约需要 2-5 分钟。所有组件 health check 通过后，Langfuse Web UI 即可访问。

### 3.4 验证部署

```bash
# 检查所有容器是否 healthy / running
docker compose -p langfuse ps

# 预期输出（6 个服务全部 running）：
# NAME                    STATUS
# langfuse-langfuse-web-1     Up (healthy)
# langfuse-langfuse-worker-1  Up (healthy)
# langfuse-postgres-1         Up (healthy)
# langfuse-clickhouse-1       Up (healthy)
# langfuse-redis-1            Up (healthy)
# langfuse-minio-1            Up (healthy)
```

打开浏览器访问 `http://localhost:3100`（或你配置的 NEXTAUTH_URL），使用 `.env` 中设置的用户名密码登录。

---

## 4. 配置 Benchmark / ctx_debugger 连接

Langfuse 部署好后，需要在运行 Benchmark 或 ctx_debugger 的环境中设置以下环境变量：

### 4.1 必需的环境变量

| 变量 | 说明 | 示例值 |
|------|------|--------|
| `LANGFUSE_HOST` | Langfuse 服务地址 | `http://localhost:3100` |
| `LANGFUSE_PUBLIC_KEY` | 项目公钥 | `pk-lf-xxxxxxxx-xxxx-...` |
| `LANGFUSE_SECRET_KEY` | 项目私钥 | `sk-lf-xxxxxxxx-xxxx-...` |

### 4.2 获取 API Key

**方式一：使用 `.env` 中的初始化密钥**

如果设置了 `LANGFUSE_INIT_PROJECT_PUBLIC_KEY` 和 `LANGFUSE_INIT_PROJECT_SECRET_KEY`，直接使用这两个值即可。

**方式二：在 Langfuse UI 中创建**

1. 登录 `http://localhost:3100`
2. 进入项目 → Settings → API Keys
3. 创建新的 API Key 对

### 4.3 设置环境变量

**方法一：Shell 导出（临时）**

```bash
export LANGFUSE_HOST=http://localhost:3100
export LANGFUSE_PUBLIC_KEY=pk-lf-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
export LANGFUSE_SECRET_KEY=sk-lf-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

**方法二：写入 `.env` 文件（Nexent 项目根目录）**

在 Nexent 项目根目录的 `.env` 文件中追加：

```bash
LANGFUSE_HOST=http://localhost:3100
LANGFUSE_PUBLIC_KEY=pk-lf-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
LANGFUSE_SECRET_KEY=sk-lf-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
LANGFUSE_ENABLED=true
```

### 4.4 验证连接

```bash
# 使用 Python 快速验证
python3 -c "
from langfuse import Langfuse
lf = Langfuse()
lf.auth_check()
print('Langfuse connection OK')
"
```

---

## 5. 使用场景

### 5.1 Benchmark 实验（run_benchmark.py）

```bash
# 确保环境变量已设置
source backend/.venv/bin/activate

# 运行实验
python run_benchmark.py \
  --agent-config configs/agent_7.yaml \
  --dataset gsm8k-n10 \
  --evaluators numeric_answer \
  --run-name gsm8k-math-assistant

# 在 Langfuse UI 查看结果：
# http://localhost:3100 → Datasets → gsm8k-n10 → Runs
```

### 5.2 ctx_debugger Trace 导入

```bash
# 从 sdk/ 目录执行
cd sdk/

# 先 dry-run 查看映射结构（不实际导入）
python -m ctx_debugger.langfuse_export /tmp/trace.jsonl --dry-run

# 实际导入
LANGFUSE_HOST=http://localhost:3100 \
LANGFUSE_PUBLIC_KEY=pk-lf-... \
LANGFUSE_SECRET_KEY=sk-lf-... \
  python -m ctx_debugger.langfuse_export /tmp/trace.jsonl
```

导入后可在 Langfuse UI 中看到：

| ctx_debugger 事件 | Langfuse 映射 |
|---|---|
| 每个 Agent turn (`agent_init`) | 一条 Trace |
| `llm_call_*` | Generation（含 input/output、tokens、duration） |
| `compress_*` | Span（内嵌 compression generations） |
| `tool_call_*` / `code_execute_*` | Tool / Span observation |
| 整个 trace 文件 | 一个 Langfuse Session（turn 分组） |

---

## 6. 运维操作

### 6.1 停止服务

```bash
docker compose -p langfuse down
```

### 6.2 重启服务

```bash
docker compose -p langfuse restart
```

### 6.3 更新镜像

```bash
docker compose -p langfuse pull
docker compose -p langfuse up -d
```

### 6.4 清除数据（慎用）

```bash
# 停止服务并删除所有数据卷（不可恢复！）
docker compose -p langfuse down -v
```

### 6.5 数据持久化

以下 Docker Volume 保存了持久化数据：

| Volume | 内容 |
|--------|------|
| `langfuse_postgres_data` | PostgreSQL 数据库（项目、用户、实验数据） |
| `langfuse_clickhouse_data` | ClickHouse 分析数据（trace、observation） |
| `langfuse_clickhouse_logs` | ClickHouse 日志 |
| `langfuse_minio_data` | MinIO 对象存储（事件、媒体文件） |
| `langfuse_redis_data` | Redis 缓存数据 |

---

## 7. 局域网访问配置

如果需要让局域网内其他机器访问 Langfuse UI：

### 7.1 修改 `.env`

```bash
# 将 NEXTAUTH_URL 改为本机的局域网 IP
NEXTAUTH_URL=http://<你的局域网IP>:3100
```

> docker-compose.yml 中 langfuse-web 的端口绑定为 `3100:3000`（不带 IP 前缀），即绑定到所有网络接口，局域网可直接访问。

### 7.2 重启服务

```bash
docker compose -p langfuse down
docker compose -p langfuse up -d
```

### 7.3 同事连接

同事在自己机器的环境变量中设置：

```bash
export LANGFUSE_HOST=http://<你的局域网IP>:3100
export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...
```

---

## 8. 故障排除

### 容器启动失败

```bash
# 检查端口占用（3100 端口是否已被其他服务使用）
lsof -i :3100

# 查看具体容器日志
docker compose -p langfuse logs langfuse-web
docker compose -p langfuse logs postgres
```

### Langfuse UI 无法访问

```bash
# 确认容器状态
docker compose -p langfuse ps

# 确认 NEXTAUTH_URL 配置正确（必须与浏览器访问的 URL 完全一致）
docker compose -p langfuse exec langfuse-web env | grep NEXTAUTH_URL
```

### Benchmark 连接失败

```bash
# 检查环境变量
echo $LANGFUSE_HOST
echo $LANGFUSE_PUBLIC_KEY
echo $LANGFUSE_SECRET_KEY

# 测试 API 连通性
curl -s -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" \
  "$LANGFUSE_HOST/api/public/health" | python3 -m json.tool
```

### 数据卷空间不足

```bash
# 查看 Docker 磁盘使用
docker system df

# 清理未使用的镜像和容器
docker system prune -f
```

---

## 9. 组件版本参考

| 组件 | 镜像 | 版本 |
|------|------|------|
| Langfuse Web | `docker.io/langfuse/langfuse` | 3 (latest) |
| Langfuse Worker | `docker.io/langfuse/langfuse-worker` | 3 (latest) |
| PostgreSQL | `docker.io/postgres` | 17 |
| ClickHouse | `docker.io/clickhouse/clickhouse-server` | latest |
| Redis | `docker.io/redis` | 7 |
| MinIO | `cgr.dev/chainguard/minio` | latest |

### Python SDK 版本要求

| 使用场景 | SDK 版本 | 安装命令 |
|---------|---------|---------|
| Benchmark（run_benchmark.py） | `langfuse<3`（v2 SDK） | `uv pip install "langfuse<3"` |
| ctx_debugger（langfuse_export.py） | 任意版本 | `uv pip install langfuse` |
| Dataset/Experiment API（高级用法） | `>=4.10.0` | `uv pip install "langfuse>=4.10.0"` |

---

## 10. 文件位置汇总

| 文件 | 路径 | 说明 |
|------|------|------|
| Docker Compose | `sdk/ctx_debugger/langfuse/docker-compose.yml` | 编排文件 |
| 环境变量 | `sdk/ctx_debugger/langfuse/.env` | 实例密钥与初始化配置（gitignored） |
| Gitignore | `sdk/ctx_debugger/langfuse/.gitignore` | 排除 .env |
| Trace 导出脚本 | `sdk/ctx_debugger/langfuse_export.py` | JSONL → Langfuse 导入 |
| Benchmark 入口 | `sdk/benchmark/generic/run_benchmark.py` | 实验运行器 |
| Benchmark 文档 | `sdk/benchmark/generic/RUN_BENCHMARK.md` | Benchmark 详细用法 |
