# LongMemEval 执行手册

操作步骤：从切换 LLM 凭据、烟雾测试、跑完整 300 题、到把 trace 导入 Langfuse。
参数细节见同目录 `README.md`。

---

## 0. 前提

日常用（环境已搭好）：

- venv：`nexent/backend/.venv/bin/python`
- 数据：一次性 `python download_data.py`（30MB，写到 `data/longmemeval_s_star.jsonl`，已 .gitignore）
- LLM 凭据：仓库根 `nexent/.env` 的 `LLM_API_KEY` / `LLM_MODEL_NAME` / `LLM_API_URL`
- **Judge 模型（可选）**：`JUDGE_API_KEY` / `JUDGE_MODEL_NAME` / `JUDGE_API_URL`
  - 留空时自动 fallback 到 `LLM_*`（同模型既当被测也当判官 — 简单但存在
    self-judging bias）
  - 单独配后判官只跑评分一步，量不大，建议用更强模型（如 GPT-4o）避免 bias
- LLM 可选环境变量（与 EventQA 相同，见 `eventqa_eval/RUNBOOK.md` §8.1）：
  - `LLM_ENABLE_THINKING=false` 关 thinking（Qwen3 类模型必关，否则被 thinking 吃掉
    `max_tokens` 预算，`final_answer(...)` 来不及发，全部 fallback 成 `no_answer`）
  - `LLM_EXTRA_BODY` 通用版本，直接 JSON 透传 `extra_body`
- Langfuse（可选，用于 trace 可视化）：已自托管在 `http://localhost:3100`；凭据见
  `sdk/ctx_debugger/langfuse/.env`

新环境从零起步见 `eventqa_eval/RUNBOOK.md` §0（同一套 venv + 同一套 Langfuse 实例）。

---

## 1. 切换到你的内网 DeepSeek

编辑 `nexent/.env`，把活动的三行换成你的内网值：

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

> **避坑**：LongMemEval 数据是合成多 session 对话（生活流水：求职 / 育宠 / 旅行 /
> 烘焙 …），内容审核问题罕见，但端点稳定性仍要事先确认。

---

## 2. 快速烟雾测试（~5–8 分钟）

确认数据、判官、压缩管线都通：

```bash
cd /home/feiran/nexent/sdk/benchmark/longmemeval_eval
../../backend/.venv/bin/python run_longmemeval.py \
    --dialogue_index 0 --limit 1 \
    --max_ingest_sessions 6 --sessions_per_batch 2 \
    --token_threshold 3000 --keep_recent_pairs 1 \
    --baseline_context_chars 40000 \
    --summary_schema multi_topic
```

预期：终端最后打印
`RESULT: baseline_acc=... compressed_acc=... retention=... token_reduction=... schema=multi_topic`，
不出现 `Error code: 400` / `Traceback` / `inappropriate`。

烟雾测试参数说明：
- `--max_ingest_sessions 6` 只 ingest 前 6 个 session（约 5% 数据，几分钟跑完）
- `--token_threshold 3000` 阈值极低 → 压缩**必然**触发
- `--keep_recent_pairs 1` 尾部只留 1 对 → 压缩区最大化
- `--baseline_context_chars 40000` baseline 截到 1 万 token，避免烟雾测试也卡几分钟

---

## 3. 完整运行：1 个对话 × 60 题（**主命令**）

跑 dialogue 0（《数字营销 Specialist Olivia》共 111 个 session、35 万 token） +
全部 60 题、multi_topic schema、贴近 256K 窗口模型的生产化参数：

```bash
cd /home/feiran/nexent/sdk/benchmark/longmemeval_eval
../../backend/.venv/bin/python run_longmemeval.py \
    --dialogue_index 0 \
    --token_threshold 200000 \
    --sessions_per_batch 12 --keep_recent_pairs 2 \
    --summary_schema multi_topic \
    --baseline_context_chars 800000
```

- 不传 `--limit` = 默认跑全部 60 题（v2 起默认 60）
- 不传 `--max_ingest_sessions` = ingest 整 111 session
- `--keep_recent_pairs 2` 与 SDK 默认一致（之前 v1 的脚本默认 10，retention=1 是因为压缩区被尾部稀释；测试 2 / 4 看真实压缩效果）
- 预计耗时 **~1.5–2.5 小时**（baseline 60 题 + compressed 60 题 + 10 轮 ingest；baseline 是大头）

跑全部 5 个对话 × 60 题 = **300 题完整集**：

```bash
../../backend/.venv/bin/python run_longmemeval.py \
    --token_threshold 200000 \
    --sessions_per_batch 12 --keep_recent_pairs 2 \
    --summary_schema multi_topic \
    --baseline_context_chars 800000
```

不传 `--dialogue_index` = 跑 5 个对话；耗时 **~8–12 小时**。

结果落在：

```
outputs/longmemeval_s_star_d<N>/
├── predictions.jsonl    # 逐题 baseline vs compressed 答案 + judge_label
└── summary.json         # 单对话指标 + 完整 multi_topic summary + per-category
outputs/summary.json     # 跨对话汇总（avg accuracy / retention / per-category）
```

### 节省成本/时间的常用开关

| 想做 | 加参数 |
|---|---|
| 只跑压缩臂（调压缩参数时省时间）| `--skip_baseline` |
| 只跑 baseline | `--skip_compressed` |
| 抽样 20 题先看趋势 | `--limit 20` |
| 换对话（0–4 = 5 个 dialogue）| `--dialogue_index <N>` |
| 用旧的 default schema 做对照 | `--summary_schema default` |
| 烟雾测试只 ingest 前 N session | `--max_ingest_sessions 6` |

---

## 4.（可选）用 ctx_debugger 抓 trace + 导入 Langfuse

只在**需要可视化看每步上下文/压缩**时走这条路。

### 4.1 跑测试时同时抓 trace

把第 3 节的命令换个入口，从 `run_with_debugger.py` 跑：

```bash
cd /home/feiran/nexent/sdk/benchmark/longmemeval_eval
NEXENT_CONTEXT_DEBUG=/tmp/longmemeval_d0_multi.jsonl \
  ../../backend/.venv/bin/python run_with_debugger.py \
      --dialogue_index 0 \
      --token_threshold 200000 \
      --sessions_per_batch 12 --keep_recent_pairs 2 \
      --summary_schema multi_topic \
      --baseline_context_chars 800000
```

参数和 `run_longmemeval.py` 一样，原样转发；trace 写到 `$NEXENT_CONTEXT_DEBUG`。

> **避坑**：env 变量名是 `NEXENT_CONTEXT_DEBUG`，别打成 `EXENT_CONTEXT_DEBUG`
> —— 错拼后会 fallback 到 `/tmp/nexent_longmemeval_trace.jsonl`，文件名对不上你
> 期望的路径，事后找 trace 抓瞎。

### 4.2 导入 Langfuse

```bash
cd /home/feiran/nexent/sdk
set -a; source ctx_debugger/langfuse/.env; set +a
LANGFUSE_HOST=http://localhost:3100 \
LANGFUSE_PUBLIC_KEY="$LANGFUSE_INIT_PROJECT_PUBLIC_KEY" \
LANGFUSE_SECRET_KEY="$LANGFUSE_INIT_PROJECT_SECRET_KEY" \
  ../backend/.venv/bin/python -m ctx_debugger.langfuse_export \
      /tmp/longmemeval_d0_multi.jsonl \
      --session-id longmemeval-d0-multi-q60
```

**每次跑都换 `--session-id`**（如 `longmemeval-d0-multi-kp2`、`longmemeval-d0-multi-kp4`）
方便在 Langfuse 里并排对比不同参数。

### 4.3 不连网先看映射结构

```bash
cd /home/feiran/nexent/sdk
../backend/.venv/bin/python -m ctx_debugger.langfuse_export \
    /tmp/longmemeval_d0_multi.jsonl --dry-run
```

---

## 5. 参数速查（细节见 README）

| 参数 | 这次用的值 | 含义 |
|---|---|---|
| `--dialogue_index` | `0` | 0–4，5 个 dialogue；不传 = 跑全部 |
| `--limit` | 缺省 `60` / 烟雾用 `1` | 每对话题数（v2 默认全量）|
| `--token_threshold` | `200000` | 压缩触发阈值；模仿 256K 窗口模型配置 |
| `--sessions_per_batch` | `12` | 每个 ingest batch 装多少 atomic session（111/12 ≈ 10 batches）|
| `--keep_recent_pairs` | `2` | 尾部保留多少 (user, assistant) pair 不压缩；**SDK 默认 2** |
| `--summary_schema` | `multi_topic` | 必用：LongMemEval 多主题、`default` 的 active_task 抽样会丢老话题 |
| `--baseline_context_chars` | `800000` | baseline 截断长度（~200K tokens，按 4 char/token 估）|
| `--max_ingest_sessions` | 缺省 `0`（全 111）/ 烟雾用 6 | ingest 截断（0=不截断）|
| `--skip_baseline` / `--skip_compressed` | 缺省 否 | 跳过某一臂（调参时省时间）|
| `--probe_max_steps` | 缺省 `3` | 每道 probe agent 最大步数 |

### 5.1 baseline_context_chars 怎么估

LongMemEval dialogue 平均 **~160 万字符 / ~35 万 tokens**（粗算 4.6 char/token）。

| 模型窗口 | 建议 baseline_context_chars | 实际能塞 |
|---|---|---|
| 128K | `500000` | ~109K tokens（留 system + question 余量）|
| **256K**（这次内网 DeepSeek）| `800000` | ~174K tokens（仍要截 50% 的 dialogue）|
| 1M | `1600000` | 全 dialogue（不截断）|

> 256K 模型必须截断；这是模型的真实表现限制，**不是 bug**。baseline accuracy 偏低
> 是预期的，retention 比 baseline 高才说明压缩有真用。

### 5.2 keep_recent_pairs 怎么调

- `2`（SDK 默认）：压缩区最大，token_reduction 反映真实压缩能力。**首选**。
- `4`：留更多原始尾部，对"最近几个 session 里的某细节"问题更鲁棒，
  但 last_compressed tokens 会涨，token_reduction 缩小。
- `10`（v1 旧默认）：尾部稀释压缩区，retention 容易接近 1（看着好看实际没压），
  **不要用**。

---

## 6. 故障排查

| 症状 | 原因 / 处置 |
|---|---|
| `data file not found` | 先跑 `python download_data.py`。 |
| `compressed_pairs=0`（trace 显示压缩未触发）| ingest 累计 token 没超 `--token_threshold`；减小阈值或减小 `--sessions_per_batch`。 |
| `token_reduction` 接近 0（如 0.06）| `--keep_recent_pairs` 太大（如 10）把压缩区稀释了；改 2。 |
| baseline accuracy 偏低（< 0.3）| 模型窗口不够 + dialogue 太长被截断；提高 `--baseline_context_chars` 到接近模型窗口上限。**这是模型真实表现，不是 bug**。 |
| 大量 `judge_label=unknown` | judge 模型对自由文本 preference 类问题判不准；考虑配独立 `JUDGE_*` 用更强模型。 |
| 大量 `no_answer` / 答案是"there is no mention of X in the conversation" | compressed 臂：summary schema 丢了 leaf-level fact（日期 / 数额 / 名字）；**用 multi_topic schema 并确保最新版包含 key_facts 字段**（见 README 与 `summary_schemas.py`）。baseline 臂：可能是模型 thinking 吃掉预算，见 §0 的 `LLM_ENABLE_THINKING=false`。 |
| `Still exceeds threshold after compression: X > Y` | 警告，不致命：保留尾部 + 新 batch 已超阈值；减 `--keep_recent_pairs` 或加 `--token_threshold`，或减 `--sessions_per_batch`。 |
| Langfuse 导入空白 | `--dry-run` 看 trace 是否非空；确认 `LANGFUSE_HOST`/keys；`curl -s http://localhost:3100/api/public/health`。 |

---

## 7. 已知限制

### 7.1 抽样 vs 完整

v2 起 `--limit` 默认 60（完整集，5 × 60 = 300 题）。调参阶段可用 `--limit 20`
（5 × 20 = 100 题）快速看趋势 —— **但**每 category 只有 2–5 题，统计噪声极大，
正式数字必须 `--limit 60`。

### 7.2 retention = 1.0 ≠ "压缩没损失"

retention 是 `compressed_acc / baseline_acc`，是聚合比；可能 baseline 答对 5 题、
compressed 也答对 5 题但**不是同 5 题**。看 `predictions.jsonl` 里的 per-qid
correctness 才知道压缩具体在哪类题上有 trade-off。

### 7.3 per-category 报告依赖足够样本

6 类问题在 60 题里分布不均（multi-session 15 / temporal-reasoning 15 /
single-session-user 9 / knowledge-update 9 / single-session-assistant 6 /
single-session-preference 6）。`--limit 20` 时每类只有 2–5 题，retention 数值
波动大，不要急着下结论。

### 7.4 token_reduction 是单点采样

如 README 里说明，`token_reduction` 取**最后一轮 ingest** 的 `get_token_counts()`
（与 `manual_cases` / `eventqa_eval` 同法）。不同 schema 的最后一轮恰好撞到相同
token 数时，retention 会相同 —— 属正常采样行为。

### 7.5 multi_topic schema 仍有改进空间

历史观察：default `active_task` schema 会把老话题当 obsolete 丢弃，对多主题数据
集致命。multi_topic schema 保住了"话题—细节"两层；v2 起又加了 `key_facts` 和
`knowledge_updates` 两个字段，专攻 quantitative 漏出（日期 / 金额 / 名称 / 最新
值更新），针对 `knowledge-update` 与 `temporal-reasoning` 两类失败。若仍 miss，
考虑：
- 加大 `topic_details` 字数上限（已 800）
- 在 system prompt 里强调"动词原文保留 verbatim"
- 极端情况：跑两遍 — 一遍 multi_topic 抽 leaf，一遍 default 做剧情线索

### 7.6 LLM thinking 模式的影响

Qwen3 等 thinking 模型在 LongMemEval 上和 EventQA 一样会出问题（见
`eventqa_eval/RUNBOOK.md` §8.1）：thinking 喷的 token 算进 `max_tokens` 预算，
`content` 来不及发完整 `final_answer(...)` → smolagents 解析失败 → `no_answer`。
正式跑前设 `LLM_ENABLE_THINKING=false`。

### 7.7 self-judging bias

默认 fallback 用 `LLM_*` 同款模型做判官，数字偏乐观（judge 偏向认可同款模型的
输出风格）。做正式对比时单独配 `JUDGE_*` —— LongMemEval 的 6 种 judge prompt
（每类一份，见 `eval_utils.py`）对判官能力要求不低，建议 GPT-4o / Claude Sonnet
等外部强模型。

### 7.8 复现/补跑必须对齐**完整** config —— 别从记忆或片段命令拼参数（踩过的坑）

要补跑或重跑某个 dialogue 与其它 dialogue 对比时，**以目标 dialogue 的
`summary.json` 里的 `config` 块为准，逐字段对齐**，不要凭记忆或截取的命令拼参数。
非默认参数漏一个就会让结果不可比，而且不会报错。

实测踩坑：补跑 d2/d3/d4 时漏掉了 `--sessions_per_batch 12`（原始跑用的，但当时
误用了脚本默认 `4`）。后果是连锁的：

- `sessions_per_batch` 决定 ingest 批数 → 压缩轮数。116 session 下 `4` → **29 批**、
  `12` → **10 批**。
- 批数越多，增量摘要累积越多：摘要从 d0/d1 的 **~4k token** 暴涨到 **~39k token**（约 10×）。
- 臃肿摘要稀释信号，`comp_acc` 直接塌（d2 0.25→0.08）—— 看起来像"修了截断 bug 反而变差"，
  其实是 batching 配错了。

容易被一起漏掉的还有 `--probe_max_tokens 512`（脚本默认现在是 `4096`）。

复现前先 dump 一份参照：

```bash
python -c "import json; print(json.dumps(json.load(open('outputs/longmemeval_s_star_d0/summary.json'))['config'], indent=2))"
```

把里面每个字段都映射成对应的 `--flag` 再跑。（同理适用于 eventqa_eval：那边对应的
关键非默认项是 `--chunk_chars` 和 `--baseline_context_tokens`。）
