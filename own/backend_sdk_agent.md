# Nexent 后端与 SDK 架构关系分析

## 一、整体架构概览

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              后端 (backend)                                  │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │
│  │  agent_app  │  │agent_service│  │create_agent_│  │  agent_run_manager  │ │
│  │   (API层)   │──│  (业务逻辑)  │──│    info     │──│   (运行管理/停止)    │ │
│  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────────────┘ │
│         │                  │                                                │
│         │                  │  创建 AgentRunInfo                              │
│         │                  ▼                                                │
│         │         ┌──────────────────────────────────────────────────────┐   │
│         │         │                 SDK (nexent/core)                     │   │
│         │         │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐   │   │
│         │         │  │  run_agent  │  │ nexent_agent│  │  core_agent │   │   │
│         │         │  │  (运行入口)  │──│ (工厂/配置)  │──│ (执行引擎)   │   │   │
│         │         │  └─────────────┘  └─────────────┘  └─────────────┘   │   │
│         │         │         │                       │                    │   │
│         │         │         │                       ▼                    │   │
│         │         │         │            ┌─────────────────┐             │   │
│         │         │         │            │  smolagents     │             │   │
│         │         │         │            │ (底层Agent框架)  │             │   │
│         │         └──────────────────────────────────────────────────────┘   │
│         │                            ▲                                       │
│         │                            │                                       │
│         │         ┌──────────────────┴──────────────────┐                   │
│         │         │         MessageObserver               │                   │
│         │         │      (消息流/上下文传递)               │                   │
│         └─────────┴──────────────────────────────────────┘                   │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 二、后端与 SDK 的职责分工

### 2.1 后端 (backend) - 编排与管理层

| 模块 | 文件路径 | 核心职责 |
|------|----------|----------|
| **API 层** | `agent_app.py` | 接收 HTTP 请求（`/agent/run`），返回 StreamingResponse |
| **业务逻辑** | `agent_service.py` | 用户鉴权、记忆检索、流式响应组装、消息持久化 |
| **配置构建** | `create_agent_info.py` | 构建 AgentRunInfo：配置模型、工具、子 Agent、Prompt 模板 |
| **运行管理** | `agent_run_manager.py` | 单例管理器：维护 conversation_id → AgentRunInfo 映射，支持停止运行 |

**关键代码流程**:
```python
# backend/services/agent_service.py
async def run_agent_stream(agent_request, ...):
    # 1. 解析用户身份
    # 2. 保存用户消息
    # 3. 构建记忆上下文
    # 4. 选择流式策略
    return StreamingResponse(stream_gen, ...)  # SSE 流式返回
```

### 2.2 SDK (nexent/core) - 执行与推理层

| 模块 | 文件路径 | 核心职责 |
|------|----------|----------|
| **运行入口** | `run_agent.py` | 入口函数 `agent_run()`，在线程中运行 Agent，通过 Observer 消费消息 |
| **Agent 工厂** | `nexent_agent.py` | 创建模型、工具、子 Agent，组装 CoreAgent |
| **执行引擎** | `core_agent.py` | 核心执行引擎：继承自 smolagents 的 CodeAgent，实现 ReAct 循环 |
| **数据模型** | `agent_model.py` | 定义 AgentRunInfo、AgentConfig、ToolConfig 等配置模型 |
| **工具集合** | `tools/` | 各类工具实现（文件操作、搜索、邮件等） |

**关键代码流程**:
```python
# sdk/nexent/core/agents/run_agent.py
async def agent_run(agent_run_info: AgentRunInfo):
    thread_agent = Thread(target=agent_run_thread, args=(agent_run_info,))
    thread_agent.start()
    
    while thread_agent.is_alive():
        cached_message = observer.get_cached_message()  # 消费消息
        for message in cached_message:
            yield message  # 产出给后端
```

---

## 三、上下文管理机制

### 3.1 AgentRunInfo - 运行时上下文容器

```python
# sdk/nexent/core/agents/agent_model.py
class AgentRunInfo(BaseModel):
    query: str                          # 用户查询
    model_config_list: List[ModelConfig]  # 可用模型列表
    observer: MessageObserver           # 消息观察器（流式输出）
    agent_config: AgentConfig           # Agent 详细配置
    mcp_host: Optional[List[...]]       # MCP 服务器配置
    history: Optional[List[AgentHistory]]  # 历史对话
    stop_event: Event                   # 停止信号
```

**上下文传递路径**:
```
backend/create_agent_info.py:create_agent_run_info()
    ↓ 创建 AgentRunInfo
sdk/run_agent.py:agent_run_thread(agent_run_info)
    ↓ 传给
sdk/nexent_agent.py:NexentAgent(observer, model_config_list, stop_event)
    ↓ 传给
sdk/core_agent.py:CoreAgent(observer, prompt_templates, ...)
```

### 3.2 MessageObserver - 消息流与状态管理

```python
# sdk/nexent/core/utils/observer.py
class MessageObserver:
    def __init__(self, lang="zh"):
        self.message_query = []      # 消息队列（用于 SSE 输出）
        self.lang = lang             # 国际化
        self.token_buffer = deque()  # Token 缓冲区（流式处理）
        self.think_buffer = deque()  # Think 标签缓冲区
    
    def add_message(self, agent_name, process_type, content):
        # 转换消息格式并加入队列
        formatted_content = transformer.transform(content, lang=self.lang)
        self.message_query.append(Message(process_type, formatted_content).to_json())
    
    def get_cached_message(self):
        # 后端调用此方法消费消息
        cached_message = self.message_query
        self.message_query = []  # 清空队列
        return cached_message
```

### 3.3 停止信号机制

```python
# backend/agents/agent_run_manager.py
class AgentRunManager:
    def register_agent_run(self, conversation_id, agent_run_info, user_id):
        self.agent_runs[f"{user_id}:{conversation_id}"] = agent_run_info
    
    def stop_agent_run(self, conversation_id, user_id):
        agent_run_info = self.get_agent_run_info(conversation_id, user_id)
        if agent_run_info:
            agent_run_info.stop_event.set()  # 设置停止信号
```

**停止信号传递链**:
```
backend/agents/agent_run_manager.py:stop_event.set()
    ↓ 共享的 Event 对象
sdk/core_agent.py:_run_stream() 中检查 stop_event.is_set()
```

### 3.4 历史对话上下文管理

```python
# sdk/nexent/core/agents/nexent_agent.py
def add_history_to_agent(self, history: List[AgentHistory]):
    self.agent.memory.reset()
    for msg in history:
        if msg.role == 'user':
            self.agent.memory.steps.append(TaskStep(task=msg.content))
        elif msg.role == 'assistant':
            self.agent.memory.steps.append(ActionStep(...))
```

---

## 四、完整执行流程

| 阶段 | 负责层 | 关键操作 | 关键文件 |
|------|--------|----------|----------|
| **1. 请求接收** | 后端 | 接收 POST 请求，鉴权 | `agent_app.py` |
| **2. 配置构建** | 后端 | 查询 DB，组装 AgentRunInfo | `create_agent_info.py` |
| **3. 运行注册** | 后端 | 保存运行状态到管理器 | `agent_run_manager.py` |
| **4. 线程启动** | SDK | 在独立线程中启动 Agent | `run_agent.py` |
| **5. 流式消费** | 后端+SDK | 循环获取 SSE 数据并返回 | `agent_service.py` |
| **6. 停止控制** | 后端+SDK | 设置/检查停止信号 | `agent_run_manager.py` + `core_agent.py` |
| **7. 资源清理** | 后端 | 注销运行实例，保存消息 | `agent_service.py` |

---

## 五、设计要点总结

### 5.1 分层清晰
- **后端**专注于请求处理、用户管理、数据持久化
- **SDK**专注于 Agent 推理执行、工具调用、流式输出

### 5.2 松耦合通信
- `MessageObserver` 作为桥梁，实现跨线程的消息传递
- `AgentRunInfo` 作为统一上下文容器，避免直接依赖

### 5.3 可控的运行时
- `AgentRunManager` 单例管理所有运行中的 Agent
- `stop_event` 机制支持随时中断 Agent 执行
- 基于 `conversation_id + user_id` 的唯一键管理多会话

### 5.4 版本管理支持
- SDK 通过 `version_no` 参数支持草稿/发布版本切换
- 后端在 `create_agent_run_info` 中根据 `is_debug` 决定使用哪个版本
