# 项目背景：Nexent

## 是什么

Nexent 是一个基于 **Harness Engineering** 原则打造的**零代码智能体（AI Agent）自动生成平台**。它提供统一的工具、技能、记忆和编排能力，内置约束机制、反��循环和控制平面——无需编排，无需复杂的拖拉拽操作，仅使用纯语言即可开发任意智能体。

> 一个提示词，无限种可能。（One prompt. Endless reach.）

## 为什么存在

- **解决什么问题**：在企业级 AI 应用开发中，构建一个功能完整的 Agent 需要编写大量编排代码、管理工具链、处理对话状态和记忆。Nexent 将这些复杂性抽象为零代码操作，让用户专注于"描述需求"而非"编写代码"。
- **此前的痛点**：传统做法需要开发者手动编排 Agent 工具链、管理 LLM 调用、对接多种外部 API（搜索引擎、知识库、文件处理等），流程繁琐且维护成本高。低代码平台虽然简化了部分流程，但"拖拉拽"的操作方式依然有较高学习曲线。

## 目标与设计哲学

- **核心目标**：让任何人都能通过自然语言创建生产级 AI Agent
- **设计原则**：
  - **零代码优先**：纯语言驱动，不需要任何编程
  - **Harness Engineering**：内置约束、反馈循环和控制平面，确保 Agent 行为可控
  - **可扩展**：通过 MCP 协议支持工具插件化扩展
  - **多模型适配**：不绑定特定 LLM 提供商，支持 OpenAI 兼容的任意模型

## 适用场景

- **典型用户**：
  - 企业 AI 应用开发者（需要快速构建 Agent）
  - 产品经理 / 业务人员（需要验证 AI 场景可行性）
  - AI 平台运维团队（需要管理多租户 Agent 服务）

- **适用场景**：
  - 构建客服 / 助手类 Agent
  - 文档处理与知识库问答
  - 多模态交互（语音 + 文本 + 图片）
  - 多 Agent 协同工作流

- **不适用场景**：
  - 纯模型训练 / 微调场景
  - 不涉及 LLM 的传统应用开发

## 技术栈

| 层级 | 技术 |
|------|------|
| **后端语言** | Python 3.10 |
| **后端框架** | FastAPI + Uvicorn |
| **前端框架** | Next.js 15 + React 18 + TypeScript |
| **UI 组件库** | Ant Design 6 + Tailwind CSS |
| **状态管理** | Zustand |
| **Agent 框架** | smolagents（HuggingFace） |
| **数据库** | PostgreSQL（关系数据）+ Elasticsearch（向量检索）+ Redis（缓存/队列） |
| **对象存储** | MinIO |
| **数据处理** | Ray + Celery + Unstructured |
| **容器化** | Docker + Docker Compose + K8s（可选） |
| **认证** | Supabase Auth + JWT |
| **SDK 核心** | Pydantic + OpenAI SDK + mem0（记忆） |
| **MCP 协议** | smolagents ToolCollection + FastMCP + MCPAdapt |
| **监控** | OpenTelemetry + Prometheus + Grafana |

## 项目现状

- **版本**：v2.0.0（2025 年 3 月发布）
- **成熟度**：Beta → Stable 过渡期，平台功能相对稳定
- **许可证**：MIT License
- **社区**：开源项目，GitHub 仓库活跃，Discord 社区运营
- **同类项目对比**：
  - 相比 Dify / Coze 等低代码平台：Nexent 强调零代码和纯语言驱动
  - 相比 LangChain / CrewAI 等框架：Nexent 提供完整的平台层（前端 UI + 后端服务 + 部署方案）
