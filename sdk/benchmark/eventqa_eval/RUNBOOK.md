# EventQA 执行手册

操作步骤：从切换 LLM 凭据、烟雾测试、跑完整 100 题、到把 trace 导入 Langfuse。
参数细节见同目录 `README.md`。

---

## 0. 前提

- venv：`nexent/backend/.venv/bin/python`
- 数据：一次性 `python download_data.py`（13MB，写到 `data/eventqa_full.jsonl`，已 .gitignore）
- LLM 凭据：仓库根 `nexent/.env` 的 `LLM_API_KEY` / `LLM_MODEL_NAME` / `LLM_API_URL`
- Langfuse（可选，用于 trace 可视化）：已自托管在 `http://localhost:3100`；凭据见 `sdk/ctx_debugger/langfuse/.env`

---

## 1. 切换到你的内网 DeepSeek

编辑 `nexent/.env`，把活动的三行换成你的内网值（保留旧值注释起来便于回切）：

```bash
# ===== Benchmark LLM Config =====
LLM_API_KEY="<your-internal-deepseek-key>"
LLM_MODEL_NAME="<your-internal-deepseek-model>"
LLM_API_URL="<your-internal-deepseek-base-url>"
```

验证：
```bash
grep -E "^LLM_(API_KEY|MODEL_NAME|API_URL)" /home/feiran/nexent/.env
```

> **避坑**：之前 glm-5（dashscope）会以"inappropriate content"拒收经典小说——
> 内网 DeepSeek 如果带类似审核，先用第 2 步烟雾测试探一下，否则跑 100 题会全废。

---

## 2. 快速烟雾测试（~3–5 分钟）

确认内网 DeepSeek 可达、不拦内容、窗口够大：

```bash
cd /home/feiran/nexent/sdk/benchmark/eventqa_eval
../../backend/.venv/bin/python run_eventqa.py \
    --book_index 0 --limit 1 \
    --max_ingest_chars 200000 --chunk_chars 100000 \
    --token_threshold 200000 \
    --summary_schema narrative \
    --baseline_context_chars 200000
```

预期：终端最后打印 `RESULT: baseline_acc=... | narrative: acc=... ... token_reduction=...`，
不出现 `Error code: 400`、`inappropriate`、`Traceback`。

---

## 3. 完整运行：1 本书 × 100 题（**主命令**）

跑 book 0《乱世佳人》整本 + 全部 100 题、narrative schema、生产化 `token_threshold=200000`：

```bash
cd /home/feiran/nexent/sdk/benchmark/eventqa_eval
../../backend/.venv/bin/python run_eventqa.py \
    --book_index 0 \
    --token_threshold 200000 --chunk_chars 100000 \
    --summary_schema narrative \
    --baseline_context_chars 800000
```

- 去掉 `--limit` = 跑全部 100 题
- 去掉 `--max_ingest_chars` = ingest 整本（约 23 chunk）
- 预计耗时 **~1.5–2.5 小时**（取决于内网 DeepSeek 速度；baseline 探针是大头：100 次 × 86 万字符喂入）

结果落在：

```
outputs/eventqa_full_book0/
├── predictions.jsonl    # 逐题 baseline vs compressed 答案
└── summary.json         # 单书指标 + 完整 narrative summary
outputs/summary.json     # 跨书汇总
```

### 节省成本/时间的常用开关

| 想做 | 加参数 |
|---|---|
| 只跑压缩臂（调压缩参数时用，baseline 是耗时大头）| `--skip_baseline` |
| 只跑 baseline | `--skip_compressed` |
| 抽样 20 题先看趋势 | `--limit 20` |
| 同时跑 default 和 narrative 对比 | `--summary_schema both`（压缩臂耗时翻倍）|
| 换本书（0–4 = 乱世佳人/悲惨世界/基督山/大卫科波菲尔/安娜卡列尼娜）| `--book_index <N>` |

---

## 4.（可选）用 ctx_debugger 抓 trace + 导入 Langfuse

只在**需要可视化看每步上下文/压缩**时走这条路（多了 trace 写盘开销，每次跑都是
一份独立 trace）。

### 4.1 跑测试时同时抓 trace

把上面第 3 节的命令换个**入口**，从 `ctx_debugger` 目录跑：

```bash
cd /home/feiran/nexent/sdk/ctx_debugger
NEXENT_CONTEXT_DEBUG=/tmp/eventqa_book0_narr.jsonl \
  ../../backend/.venv/bin/python example_with_eventqa.py \
      --book_index 0 \
      --token_threshold 200000 --chunk_chars 100000 \
      --summary_schema narrative \
      --baseline_context_chars 800000
```

参数和 `run_eventqa.py` 一样，原样转发。trace 写到 `$NEXENT_CONTEXT_DEBUG`。

**这次 demo 的命令**（1 本书 1 题，整本 ingest）：

```bash
cd /home/feiran/nexent/sdk/ctx_debugger
NEXENT_CONTEXT_DEBUG=/tmp/eventqa_narr_trace.jsonl \
  ../../backend/.venv/bin/python example_with_eventqa.py \
      --book_index 0 --limit 1 \
      --token_threshold 200000 --chunk_chars 100000 \
      --summary_schema narrative \
      --baseline_context_chars 800000
```

### 4.2 导入 Langfuse

```bash
cd /home/feiran/nexent/sdk
set -a; source ctx_debugger/langfuse/.env; set +a
LANGFUSE_HOST=http://localhost:3100 \
LANGFUSE_PUBLIC_KEY="$LANGFUSE_INIT_PROJECT_PUBLIC_KEY" \
LANGFUSE_SECRET_KEY="$LANGFUSE_INIT_PROJECT_SECRET_KEY" \
  ../backend/.venv/bin/python -m ctx_debugger.langfuse_export \
      /tmp/eventqa_book0_narr.jsonl \
      --session-id book0-narrative-full
```

**每次跑都换一个 `--session-id`**（如 `book0-narr-thr150k`、`book0-narr-chunk60k`），
就是新 session，方便在 Langfuse 里并排对比不同参数。已建过的 session 名：
`nexent-ctx-demo`、`eventqa-demo`、`eventqa-narrative`（这次 demo）。

在 Langfuse 项目 `nexent-context` 下点对应 session 即可看：每个 turn 嵌套展开
ingest 轮 / 压缩 span / 主 LLM 调用 / 工具调用 / token 用量。

### 4.3 不连网先看映射结构

```bash
cd /home/feiran/nexent/sdk
../backend/.venv/bin/python -m ctx_debugger.langfuse_export \
    /tmp/eventqa_book0_narr.jsonl --dry-run
```

---

## 5. 参数速查（细节见 README）

| 参数 | 这次用的值 | 含义 |
|---|---|---|
| `--book_index` | `0` | 0–4，5 本小说 |
| `--limit` | 缺省=100 / 烟雾用 1 | 每本题数 |
| `--token_threshold` | `200000` | 压缩触发阈值，模仿 glm-5 200K 窗口生产配置 |
| `--chunk_chars` | `100000` | 小说切块粒度（~23k tokens/chunk，整本 ~23 块）|
| `--summary_schema` | `narrative` | `default` / `narrative` / `both` |
| `--baseline_context_chars` | `800000` | baseline 截断长度（~186k tokens，~200K 窗口生产场景）|
| `--keep_recent_pairs` | 缺省 `2` | 尾部保留 chunk 数 |
| `--max_ingest_chars` | 缺省 `0`（整本）/ 烟雾用 200000 | ingest 截断（0=不截断）|

---

## 6. 故障排查

| 症状 | 原因 / 处置 |
|---|---|
| `Error code: 400 ... inappropriate content` | LLM 端点有内容审核拦经典文学。换模型/端点（DeepSeek 直连无此问题）。 |
| 输出大量 `</s>`、随机字符、`扫码失败` | LLM 在产出退化乱码（OpenRouter `:free` 见过）。换模型。 |
| `Still exceeds threshold after compression: X > Y` | 警告，不致命。说明保留尾部 + 当前 chunk 已经超过 token_threshold；可以减小 `--keep_recent_pairs` 或 `--chunk_chars`，或加大 `--token_threshold`。 |
| `compressed_pairs=0`（trace 显示压缩未触发）| ingest 累计 token 没超过 `--token_threshold`。增加 `--max_ingest_chars`、减小 `--token_threshold`、或减小 `--chunk_chars`。 |
| Langfuse 导入空白 | `--dry-run` 看 trace 是否非空；确认 `LANGFUSE_HOST`/keys 正确；`curl -s http://localhost:3100/api/public/health` 检查服务。 |
| `data file not found` | 先跑 `python download_data.py`。 |
