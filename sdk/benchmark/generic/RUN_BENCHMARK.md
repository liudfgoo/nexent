# run\_benchmark.py 使用文档

统一的 benchmark 运行器，替代 `run_experiment.py` 和 `re_evaluate.py`。

## 功能

- 从 YAML 配置文件加载 Agent 参数
- CLI 参数覆盖 YAML 配置
- 运行新实验（调用 LLM）
- 重新评分已有结果（不调用 LLM）
- 上传数据集 + 运行一体化

## 命令行参数

### Agent 配置

| 参数               |  必填 | 说明                                               |
| ---------------- | :-: | ------------------------------------------------ |
| `--agent-config` |  否  | Agent YAML 配置文件路径（由 `export_agent_config.py` 生成） |

### 数据集

| 参数             |  必填 | 说明                              |
| -------------- | :-: | ------------------------------- |
| `--dataset`    |  ✅  | Langfuse 数据集名称                  |
| `--upload`     |  否  | JSONL 文件路径（上传后运行）               |
| `--input-key`  |  否  | JSONL 中问题字段的 key（默认 `question`） |
| `--output-key` |  否  | JSONL 中答案字段的 key（默认 `answer`）   |

### 评分器

| 参数                  |  必填 | 说明                        |
| ------------------- | :-: | ------------------------- |
| `--evaluators`      |  否  | 评分器名称列表（默认 `exact_match`） |
| `--list-evaluators` |  否  | 列出所有可用评分器并退出              |

**可用评分器**：

| 评分器              | 说明               | 适用场景         |
| ---------------- | ---------------- | ------------ |
| `numeric_answer` | 提取数字并比较          | 数学题、计算题      |
| `exact_match`    | SQuAD 标准化后精确匹配   | 短答案 QA       |
| `em`             | exact\_match 的别名 | 同上           |
| `f1`             | Token 级 F1 分数    | 开放式 QA       |
| `keyword_match`  | 关键词命中率           | 需要包含特定关键词的回答 |

### Agent 执行参数（覆盖 YAML）

| 参数                     | 说明                      |
| ---------------------- | ----------------------- |
| `--max-steps`          | 最大执行步数                  |
| `--temperature`        | LLM 温度                  |
| `--language`           | 提示语言（`en` / `zh`）       |
| `--duty-prompt`        | 角色描述 prompt             |
| `--constraint-prompt`  | 约束条件 prompt             |
| `--few-shots-prompt`   | Few-shot 示例 prompt      |
| `--system-prompt-file` | 自定义系统 prompt 文件（跳过模板引擎） |

### 上下文管理

Context Manager 配置从 YAML 文件的 `agent_config.enable_context_manager` 字段读取，CLI 参数可覆盖。

**YAML 配置示例**：
```yaml
agent_config:
  enable_context_manager: true  # 或 false
```

**CLI 参数**：

| 参数                          | 说明        |
| --------------------------- | --------- |
| `--enable-context-manager`  | 强制启用上下文管理（覆盖 YAML） |
| `--disable-context-manager` | 强制禁用上下文管理（覆盖 YAML） |

**逻辑流程**：
1. 从 YAML 读取 `enable_context_manager` 值（默认 `false`）
2. CLI 参数 `--enable-context-manager` 或 `--disable-context-manager` 可覆盖
3. 创建 `ContextManagerConfig(enabled=enable_cm)`，`enabled` 参数直接来自配置

### 执行控制

| 参数                  | 说明           |
| ------------------- | ------------ |
| `--max-concurrency` | 最大并行数（默认 1）  |
| `--run-name`        | 自定义运行名称      |
| `--dry-run`         | 仅上传数据集，不运行实验 |

### 重新评分模式

| 参数               | 说明                |
| ---------------- | ----------------- |
| `--rescore`      | 启用重新评分模式（不调用 LLM） |
| `--existing-run` | 要重新评分的已有运行名称      |

## 使用示例

### 场景 1：使用 Agent 配置运行实验

```bash
python run_benchmark.py \
  --agent-config configs/agent_7.yaml \
  --dataset gsm8k-n10 \
  --evaluators numeric_answer \
  --run-name gsm8k-math-assistant-zh
```

### 场景 2：CLI 参数覆盖 YAML 配置

```bash
python run_benchmark.py \
  --agent-config configs/agent_7.yaml \
  --dataset gsm8k-n10 \
  --max-steps 20 \
  --temperature 0.2 \
  --language zh \
  --evaluators numeric_answer em f1
```

### 场景 3：不用 YAML，纯 CLI 参数

```bash
python run_benchmark.py \
  --dataset gsm8k-n10 \
  --evaluators numeric_answer \
  --duty-prompt "你是一个专业的数学解题助手，仅输出最终数字答案。" \
  --max-steps 15 \
  --run-name gsm8k-custom-prompt
```

### 场景 4：重新评分已有结果（秒级，不调 LLM）

```bash
python run_benchmark.py \
  --rescore \
  --dataset gsm8k-n10 \
  --existing-run gsm8k-math-assistant \
  --evaluators em f1 exact_match
```

### 场景 5：上传新数据集并运行

```bash
# 先只上传，确认无误
python run_benchmark.py \
  --agent-config configs/agent_7.yaml \
  --dataset my-new-benchmark \
  --upload data/test.jsonl \
  --evaluators numeric_answer \
  --dry-run

# 确认后运行实验
python run_benchmark.py \
  --agent-config configs/agent_7.yaml \
  --dataset my-new-benchmark \
  --evaluators numeric_answer
```

### 场景 6：对比不同 Agent 配置

```bash
# Agent A
python run_benchmark.py \
  --agent-config configs/agent_5.yaml \
  --dataset gsm8k-n10 \
  --evaluators numeric_answer \
  --run-name gsm8k-agent5

# Agent B
python run_benchmark.py \
  --agent-config configs/agent_7.yaml \
  --dataset gsm8k-n10 \
  --evaluators numeric_answer \
  --run-name gsm8k-agent7

# 在 Langfuse UI 中对比两个 run 的结果
```

### 场景 7：使用自定义系统 prompt

```bash
# 创建自定义 prompt 文件
cat > my_prompt.txt << 'EOF'
你是一个专业的数学解题助手。
请仔细阅读题目，逐步推导，最后只输出数字答案。
不要输出解题过程。
EOF

# 使用自定义 prompt
python run_benchmark.py \
  --dataset gsm8k-n10 \
  --system-prompt-file my_prompt.txt \
  --evaluators numeric_answer \
  --run-name gsm8k-custom-system-prompt
```

### 场景 8：启用上下文管理

```bash
# 强制启用（即使 YAML 中未启用）
python run_benchmark.py \
  --agent-config configs/agent_7.yaml \
  --dataset gsm8k-n10 \
  --enable-context-manager \
  --evaluators numeric_answer

# 强制禁用（即使 YAML 中已启用）
python run_benchmark.py \
  --agent-config configs/agent_7.yaml \
  --dataset gsm8k-n10 \
  --disable-context-manager \
  --evaluators numeric_answer
```

## 配置优先级

**CLI 参数 > YAML 配置 > 默认值**

```
--duty-prompt "..."           → 覆盖 YAML 中的 duty_prompt
--max-steps 20                → 覆盖 YAML 中的 max_steps
--enable-context-manager      → 覆盖 YAML 中的 enable_context_manager
--temperature 0.5             → 覆盖默认值 0.1
```

## 完整工作流

```bash
# 1. 导出 Agent 配置
python export_agent_config.py --agent-id 7 --output configs/agent_7.yaml

# 2. 运行实验
python run_benchmark.py \
  --agent-config configs/agent_7.yaml \
  --dataset gsm8k-n10 \
  --evaluators numeric_answer

# 3. 用不同评分器重新评分（不调用 LLM）
python run_benchmark.py \
  --rescore \
  --dataset gsm8k-n10 \
  --existing-run gsm8k-n10-1720432800 \
  --evaluators em f1 exact_match keyword_match

# 4. 在 Langfuse UI 查看结果
# http://localhost:3100 → Datasets → gsm8k-n10 → Runs
```

## 输出示例

```
Loading agent config from: configs/agent_7.yaml
  Agent: 数学解答助手
  Description: 你是一个数学解答助手，擅长解答GSM8K等数学问题...

Evaluators: ['numeric_answer']
Langfuse connected: http://localhost:3100

Configuration:
  Max steps:    15
  Temperature:  0.1
  Language:     en
  Context mgr:  True
  Duty prompt:  你是一个专业的数学解题助手，负责解答各类数学计算问题...

  10 items loaded

============================================================
Running experiment: gsm8k-math-assistant
  Dataset:      gsm8k-n10 (10 items)
  Model:        deepseek-v4-flash
============================================================

[1/10] {"question": "Janet's ducks lay 16 eggs..."} ✓ numeric_answer=1.00
[2/10] {"question": "A robe takes 2 bolts..."} ✓ numeric_answer=1.00
...
[10/10] {"question": "Josh decides to try..."} ✗ numeric_answer=0.00

============================================================
Experiment complete: gsm8k-math-assistant
  Total:  10
  Passed: 9
  Failed: 1
  Avg numeric_answer: 0.9000

View in Langfuse: http://localhost:3100/dataset/xxx
============================================================
```

## 重新评分输出示例

```
Evaluators: ['em', 'f1', 'exact_match']
Langfuse connected: http://localhost:3100

Rescore 'gsm8k-math-assistant-rescore': 10 traces from 'gsm8k-math-assistant'
  [1] scores={'em': 1.0, 'f1': 1.0, 'exact_match': 1.0}
  [2] scores={'em': 1.0, 'f1': 1.0, 'exact_match': 1.0}
  ...
  [10] scores={'em': 0.0, 'f1': 0.5, 'exact_match': 0.0}

Rescore 'gsm8k-math-assistant-rescore' DONE: 9/10 passed
  avg_em=0.9000
  avg_f1=0.9500
  avg_exact_match=0.9000
```

## 环境变量

### 必需

| 变量                    | 说明            |
| --------------------- | ------------- |
| `LLM_API_KEY`         | LLM API 密钥    |
| `LLM_MODEL_NAME`      | LLM 模型名称      |
| `LLM_API_URL`         | LLM API 地址    |
| `LANGFUSE_HOST`       | Langfuse 服务地址 |
| `LANGFUSE_PUBLIC_KEY` | Langfuse 公钥   |
| `LANGFUSE_SECRET_KEY` | Langfuse 私钥   |

### 可选

| 变量                 | 说明            | 默认值    |
| ------------------ | ------------- | ------ |
| `LANGFUSE_ENABLED` | 是否启用 Langfuse | `true` |

## 故障排除

### Langfuse 连接失败

```bash
# 检查 Langfuse 容器状态
docker ps | grep langfuse

# 检查环境变量
echo $LANGFUSE_HOST
echo $LANGFUSE_PUBLIC_KEY
echo $LANGFUSE_SECRET_KEY
```

### LLM API 调用失败

```bash
# 检查 LLM 配置
echo $LLM_API_URL
echo $LLM_MODEL_NAME

# 测试 API 连接
curl -X POST $LLM_API_URL/chat/completions \
  -H "Authorization: Bearer $LLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "'$LLM_MODEL_NAME'", "messages": [{"role": "user", "content": "test"}]}'
```

### 数据集不存在

```bash
# 先上传数据集
python run_benchmark.py \
  --dataset my-dataset \
  --upload data/test.jsonl \
  --dry-run

# 或检查 Langfuse UI 中是否存在该数据集
```

### 评分器不存在

```bash
# 列出所有可用评分器
python run_benchmark.py --list-evaluators
```

## 性能优化

### 并行执行

```bash
# 增加并行数（注意 LLM API 限制）
python run_benchmark.py \
  --agent-config configs/agent_7.yaml \
  --dataset gsm8k-n10 \
  --max-concurrency 5 \
  --evaluators numeric_answer
```

### 减少 LLM 调用

```bash
# 使用重新评分模式（不调用 LLM）
python run_benchmark.py \
  --rescore \
  --dataset gsm8k-n10 \
  --existing-run previous-run \
  --evaluators em f1
```

### 减少步数

```bash
# 限制最大步数（减少 token 消耗）
python run_benchmark.py \
  --agent-config configs/agent_7.yaml \
  --dataset gsm8k-n10 \
  --max-steps 5 \
  --evaluators numeric_answer
```

## 与旧脚本的对比

| 功能          | run\_experiment.py | re\_evaluate.py | run\_benchmark.py |
| ----------- | ------------------ | --------------- | ----------------- |
| 运行新实验       | ✅                  | ❌               | ✅                 |
| 重新评分        | ❌                  | ✅               | ✅                 |
| 从 YAML 加载配置 | ❌                  | ❌               | ✅                 |
| CLI 覆盖配置    | 部分                 | 部分              | ✅                 |
| 上传数据集       | ✅                  | ❌               | ✅                 |
| 统一入口        | ❌                  | ❌               | ✅                 |

**结论**：`run_benchmark.py` 完全替代 `run_experiment.py` 和 `re_evaluate.py`。
