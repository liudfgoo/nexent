---
name: dive-into-code
description: 深度理解并文档化一个代码仓库的实现目标、代码架构、技术原理、关键数据流与使用示例。适用场景：用户想快速上手某个项目、想了解代码结构、想搞清楚技术原理、想写架构文档、想生成开发者入门指南。触发词包括：「帮我理解这个项目」「分析这个仓库」「读懂这个代码」「生成项目文档」「看看这个 README」「探索代码结构」「这个代码怎么工作的」「创建开发文档」「代码架构是什么」。只要用户提到代码仓库 + 理解/分析/文档/架构，就应使用此 skill。
---

# Dive Into Code

从 README 出发，逐层深入探索一个代码仓库，最终生成一组高质量中文 Markdown 文档。

## 资源文件

开始工作前，按需读取以下资源：

| 文件 | 何时读取 | 内容 |
|------|----------|------|
| `references/guidelines.md` | 开始分析前 | 深度分级、阅读策略、科学信号词表、质量自查清单 |
| `assets/templates.md` | Phase 6 生成文档时 | 全部 7 份文档的输出模板 |

## 输出文件清单

| 文件 | 内容 | 是否必须 |
|------|------|----------|
| `intro.md` | 项目背景：是什么、为什么、适用场景 | 是 |
| `arch.md` | 代码架构：模块划分、设计模式、扩展点 | 是 |
| `dataflow.md` | 关键数据流：核心功能的完整处理路径 | 是 |
| `note.md` | 代码结构：目录/文件职责速查表 | 是 |
| `examples.md` | 最小示例：核心功能的可运行代码 | 是 |
| `sci.md` | 科学原理：算法/模型/数学原理 | 仅涉及时 |
| `dev-guide.md` | 开发指南：搭建、测试、调试、贡献 | 是 |

---

## 输出目录策略

按以下优先级确定输出目录 `$OUT_DIR`：

1. **用户明确指定** → 使用用户给出的路径
2. **项目根目录可写** → `$PROJECT_ROOT/own/`
3. **均不可用** → `/mnt/user-data/outputs/codebase-docs/`

```bash
if [ -n "$USER_SPECIFIED_DIR" ]; then
  OUT_DIR="$USER_SPECIFIED_DIR"
elif [ -w "$PROJECT_ROOT" ]; then
  OUT_DIR="$PROJECT_ROOT/own"
else
  OUT_DIR="/mnt/user-data/outputs/codebase-docs"
fi
mkdir -p "$OUT_DIR"
```

---

## Phase 0 — 定位项目根目录

```bash
ls /mnt/user-data/uploads/
```

将 `$PROJECT_ROOT` 设置为项目根目录。若不确定，询问用户。

---

## Phase 1 — 读取入口文档

**目标**：建立第一印象，提取关键信号。

```bash
for f in README.md README.rst README.txt README; do
  [ -f "$PROJECT_ROOT/$f" ] && cat "$PROJECT_ROOT/$f" && break
done
```

从 README 中提取并记录：

- 项目名称与一句话描述
- 核心问题/动机
- 主要特性列表
- 技术栈关键词（语言、框架、依赖）
- 是否涉及科学/算法内容（参考 `references/guidelines.md` 中的信号词表）
- 安装/使用方式
- 目标用户
- 项目状态（alpha / beta / stable / archived）

若 README 不够充分，补充读取：

```bash
cat "$PROJECT_ROOT/CONTRIBUTING.md" 2>/dev/null
cat "$PROJECT_ROOT/docs/index.md" 2>/dev/null || ls "$PROJECT_ROOT/docs/" 2>/dev/null
cat "$PROJECT_ROOT/ARCHITECTURE.md" 2>/dev/null
cat "$PROJECT_ROOT/DESIGN.md" 2>/dev/null
cat "$PROJECT_ROOT/CHANGELOG.md" 2>/dev/null | head -50
```

---

## Phase 2 — 探索目录结构

**目标**：形成整体布局认知。

```bash
find "$PROJECT_ROOT" -maxdepth 2 \
  -not -path '*/.git/*' \
  -not -path '*/node_modules/*' \
  -not -path '*/__pycache__/*' \
  -not -path '*/.venv/*' \
  -not -path '*/dist/*' \
  -not -path '*/build/*' \
  -not -path '*/.next/*' \
  -not -path '*/target/*' \
  -not -path '*/.idea/*' \
  | sort
```

对顶层目录推断职责：

| 目录模式 | 推断职责 |
|----------|----------|
| `src/` / `lib/` / `pkg/` | 核心逻辑 |
| `tests/` / `test/` / `spec/` | 测试 |
| `docs/` | 文档 |
| `examples/` / `demo/` | 示例 |
| `scripts/` / `tools/` | 工具脚本 |
| `config/` / `configs/` | 配置 |
| `api/` / `routes/` / `handlers/` | API 层 |
| `models/` / `schemas/` | 数据模型 |
| `migrations/` | 数据库迁移 |
| `data/` / `assets/` | 数据或静态资源 |
| `third_party/` / `vendor/` | 第三方依赖 |

---

## Phase 3 — 深入核心代码

**目标**：理解模块边界、关键抽象和数据流。

### 3.1 读取项目配置文件

```bash
# Python
cat "$PROJECT_ROOT/pyproject.toml" 2>/dev/null || cat "$PROJECT_ROOT/setup.py" 2>/dev/null
cat "$PROJECT_ROOT/requirements.txt" 2>/dev/null

# JavaScript / Node
cat "$PROJECT_ROOT/package.json" 2>/dev/null

# Go
cat "$PROJECT_ROOT/go.mod" 2>/dev/null

# Rust
cat "$PROJECT_ROOT/Cargo.toml" 2>/dev/null

# C/C++
cat "$PROJECT_ROOT/CMakeLists.txt" 2>/dev/null

# Java / Kotlin
cat "$PROJECT_ROOT/pom.xml" 2>/dev/null || cat "$PROJECT_ROOT/build.gradle" 2>/dev/null

# 部署相关
cat "$PROJECT_ROOT/Dockerfile" 2>/dev/null
cat "$PROJECT_ROOT/docker-compose.yml" 2>/dev/null
```

### 3.2 定位并阅读核心源文件

每步读完后判断是否已足够，避免全量扫描。

**步骤 A — 寻找入口点**

```bash
for f in main.py app.py index.py __main__.py server.py \
          main.go cmd/main.go \
          main.rs src/main.rs src/lib.rs \
          index.js index.ts src/index.js src/index.ts src/app.ts \
          Main.java App.java; do
  [ -f "$PROJECT_ROOT/$f" ] && echo "=== $f ===" && head -100 "$PROJECT_ROOT/$f"
done
```

**步骤 B — 阅读核心模块**

优先选择：
- 定义主要类/接口的文件
- 名字含 `base`、`core`、`engine`、`model`、`pipeline`、`trainer`、`server`、`client`、`handler`、`service`、`controller` 的文件
- `__init__.py` 或 `index.*`（模块导出接口）

**步骤 C — 阅读数据结构与配置定义**

```bash
find "$PROJECT_ROOT/src" "$PROJECT_ROOT/lib" "$PROJECT_ROOT/pkg" \
  -name "*.py" -o -name "*.ts" -o -name "*.go" -o -name "*.rs" -o -name "*.java" 2>/dev/null \
  | xargs grep -l "class\|struct\|interface\|dataclass\|Schema\|Config\|Model\|Entity" 2>/dev/null \
  | head -10
```

**步骤 D — 识别外部集成**

```bash
grep -r "connect\|client\|session\|endpoint\|redis\|mongo\|postgres\|mysql\|kafka\|rabbitmq\|grpc\|http\|fetch\|axios" \
  "$PROJECT_ROOT/src" --include="*.py" --include="*.ts" --include="*.go" --include="*.rs" --include="*.java" -l 2>/dev/null \
  | head -10
```

**步骤 E — 读取测试/示例**

```bash
ls "$PROJECT_ROOT/tests/" 2>/dev/null || ls "$PROJECT_ROOT/test/" 2>/dev/null
ls "$PROJECT_ROOT/examples/" 2>/dev/null
# 选 2-3 个典型用例读取
```

### 3.3 追踪关键功能数据流

识别项目的 3-5 个关键功能，逐一追踪：

```
功能名称 → 入口函数 → 调用链 → 数据变换 → 输出/副作用
```

对每条数据流记录：
1. **触发方式**：HTTP 请求、CLI 命令、定时任务、消息消费
2. **输入数据**：格式、来源
3. **处理步骤**：依次经过哪些模块/函数，每步做了什么
4. **输出/副作用**：返回值、写库、发消息、写文件
5. **错误处理**：异常捕获位置与处理策略

追踪辅助命令：

```bash
# 从入口函数追踪调用链
grep -rn "def 函数名\|func 函数名\|function 函数名" "$PROJECT_ROOT/src" 2>/dev/null
grep -rn "函数名" "$PROJECT_ROOT/src" 2>/dev/null
```

---

## Phase 4 — 识别科学/算法内容（可选）

若在 Phase 1-3 中发现科学/算法信号（完整信号词表见 `references/guidelines.md`），需额外深入：

```bash
grep -r "arxiv\|paper\|equation\|formula\|theorem\|lemma\|proof\|doi\|reference" \
  "$PROJECT_ROOT" --include="*.py" --include="*.md" --include="*.txt" --include="*.rs" --include="*.go" -l 2>/dev/null | head -5
```

若无科学内容，跳过此阶段，Phase 6 中不生成 `sci.md`。

---

## Phase 5 — 提取最小使用示例

**目标**：为每个核心功能提供最小可运行示例。

素材来源（按优先级）：
1. `examples/` 或 `demo/` 目录 — 直接提取并精简
2. 测试文件 — 从测试用例中提取典型调用
3. README 中的示例 — 整理并补充
4. 自行构造 — 基于 API 理解编写

```bash
find "$PROJECT_ROOT/examples" "$PROJECT_ROOT/demo" -type f 2>/dev/null | head -10
grep -r "def test_\|func Test\|it('\|describe('\|#\[test\]" \
  "$PROJECT_ROOT/tests" "$PROJECT_ROOT/test" 2>/dev/null | head -20
```

每个示例要求：
- 完整可运行（不超过 30 行）
- 包含前置条件、预期输出、关键参数说明
- 若无法确认可运行性，标注 `[待验证]`

---

## Phase 6 — 生成文档

**开始前读取 `assets/templates.md`**，获取每份文档的输出模板。

```bash
mkdir -p "$OUT_DIR"
```

按模板依次生成：
1. `intro.md` — 项目背景
2. `arch.md` — 代码架构（含 Mermaid 架构图）
3. `dataflow.md` — 关键数据流（3-5 条，追踪到函数级别）
4. `note.md` — 代码结构速查
5. `examples.md` — 最小使用示例（3-5 个核心功能）
6. `sci.md` — 科学原理（仅涉及时生成）
7. `dev-guide.md` — 开发指南

---

## Phase 7 — 质量自查

完成全部文档后，执行 `references/guidelines.md` 中的质量检查清单。

重点确认：
- 数据流是否追踪到具体函数级别
- 示例是否真正可运行
- 术语在所有文档间是否一致
- 不确定内容是否标注了 `[待确认]`
- 所有文档使用中文，代码和术语保留英文原文

---

## 注意事项

- **阅读策略**：广度优先，再针对关键模块深入，不逐行通读
- **不确定时**：结合文件名、import 关系、函数命名、顶部注释推断
- **大型项目（>200 文件）**：只深入入口文件 + 核心模块（≤5 个）+ 最重要的 3-5 条数据流
- **深度分级**：详见 `references/guidelines.md`，未指定时按文件数自动判断
- **输出格式**：全部 Markdown（`.md`），中文撰写