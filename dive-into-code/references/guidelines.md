# 参考指南

本文件供 SKILL.md 执行过程中按需查阅。

---

## 深度分级

根据项目复杂度和用户需求选择分析深度：

| 级别 | 适用场景 | 生成文档 | 说明 |
|------|----------|----------|------|
| **快速** | 小型项目（<30 文件）或初步了解 | intro + arch + note | 跳过 dataflow、examples、dev-guide |
| **标准** | 中等项目（30-200 文件）或正式入职 | 全部 7 份（sci 视情况） | 默认选择 |
| **深度** | 大型项目（>200 文件）或架构评审 | 全部 7 份 + 详细代码注释 | 聚焦核心模块，标注未覆盖部分 |

自动判断规则（用户未指定时）：

```bash
FILE_COUNT=$(find "$PROJECT_ROOT" -type f \
  -not -path '*/.git/*' -not -path '*/node_modules/*' \
  -not -path '*/__pycache__/*' -not -path '*/dist/*' \
  -not -path '*/build/*' -not -path '*/target/*' \
  | wc -l)

if [ "$FILE_COUNT" -lt 30 ]; then
  DEPTH="quick"
elif [ "$FILE_COUNT" -le 200 ]; then
  DEPTH="standard"
else
  DEPTH="standard"  # 大型项目仍用标准，但聚焦核心模块
fi
```

---

## 科学/算法信号词表

在 Phase 1-3 中若遇到以下信号，需要在 Phase 4 深入分析并生成 `sci.md`：

| 信号词 | 深入方向 |
|--------|----------|
| neural network / transformer / diffusion / GAN | 模型结构、训练目标、损失函数 |
| optimization / gradient / loss / backprop | 优化算法与数学推导 |
| signal processing / FFT / filter / spectrum | 信号处理原理 |
| physics / simulation / PDE / ODE / FEM | 物理方程与数值方法 |
| statistics / bayesian / inference / MCMC | 统计推断方法 |
| cryptography / hash / cipher / elliptic | 密码学原理 |
| compression / codec / entropy | 信息论与编码 |
| computer vision / segmentation / detection | 视觉算法 |
| NLP / tokenizer / embedding / attention | 语言模型原理 |
| reinforcement learning / policy / reward / agent | 强化学习 |
| graph / GNN / adjacency / shortest path | 图算法 |
| recommendation / collaborative filtering / ranking | 推荐系统 |

---

## 阅读策略

### 核心原则

**广度优先，再选择性深入。** 不要逐行通读整个代码库。

### 推断未知文件用途的线索

当某文件/模块用途不明时，按以下优先级推断：

1. 文件名 + 目录位置
2. import 关系（被谁依赖 / 依赖谁）
3. 函数/类命名
4. 顶部注释或 docstring
5. 测试文件中对该模块的使用方式

### 大型项目策略（>200 文件或 >3 层目录）

只深入以下部分：

- 入口文件（1-2 个）
- 核心模块（≤5 个，按调用频率或业务重要性选择）
- 被调用最多的工具类/基类
- 最重要的 3-5 条数据流

在文档中明确标注「以下目录/模块未深入分析」。

### 文件阅读量控制

- 单个文件优先读取前 100-150 行（通常包含类定义和主要接口）
- 仅在需要追踪具体逻辑时才读取完整文件
- 配置文件、依赖声明全量读取（通常较短）

---

## 质量自查清单

Phase 7 中逐项检查：

### 内容完整性
- [ ] `intro.md`：「为什么存在」部分是否清晰回答了动机？
- [ ] `arch.md`：架构图是否反映了主要模块关系？是否列出了外部集成？
- [ ] `dataflow.md`：是否覆盖至少 3 条最重要的数据流？每条是否追踪到具体函数级别？
- [ ] `note.md`：是否覆盖全部顶层目录？核心目录下是否逐文件说明？
- [ ] `examples.md`：示例是否完整可运行（不缺导入、不缺依赖）？是否包含预期输出？
- [ ] `sci.md`（若生成）：是否给出了参考文献供读者深入？
- [ ] `dev-guide.md`：环境搭建步骤是否完整，新人能否从零启动？

### 质量标准
- [ ] 不确定的内容是否标注了 `[待确认]` 并说明原因？
- [ ] 七份文档之间的术语是否一致？
- [ ] 所有文档是否使用中文？代码和技术术语是否保留英文原文？
- [ ] 是否避免了大段复制粘贴源码？（应精选关键片段并加注释说明）

### 交叉验证
- [ ] `arch.md` 中的模块是否与 `note.md` 中的目录对应？
- [ ] `dataflow.md` 中引用的函数是否在 `note.md` 中有记录？
- [ ] `examples.md` 中使用的 API 是否在 `arch.md` 中作为对外接口提及？