# -*- coding: utf-8 -*-
"""
改进版本：test_multi_run.py with proper message statistics and filtering

关键改进：
1. 只显示 final_answer，不显示中间过程的 MODEL OUTPUT
2. 正确的消息统计（按消息类型而不是 chunk 总数）
3. 清晰的对话流程
"""
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

import asyncio
import threading
import json
import os
from datetime import datetime
from jinja2 import Template, StrictUndefined
from smolagents.utils import BASE_BUILTIN_MODULES
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKEND_PATH = os.path.join(PROJECT_ROOT, "backend")

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if BACKEND_PATH not in sys.path:
    sys.path.insert(0, BACKEND_PATH)

from utils.prompt_template_utils import get_agent_prompt_template
from nexent.core.agents.agent_model import (
    AgentRunInfo, AgentConfig, ModelConfig, AgentHistory, ToolConfig
)
from nexent.core.agents.run_agent import agent_run
from nexent.core.utils.observer import MessageObserver
from nexent.core.tools import CreateFileTool, ReadFileTool
import logging
logging.getLogger("smolagents").setLevel(logging.WARNING)

load_dotenv()

LLM_API_KEY = os.getenv("LLM_API_KEY")
LLM_MODEL_NAME = os.getenv("LLM_MODEL_NAME")
LLM_API_URL = os.getenv("LLM_API_URL")

APP_NAME = os.getenv("APP_NAME", "Nexent")
APP_DESCRIPTION = os.getenv("APP_DESCRIPTION", "Nexent 是一个开源智能体SDK和平台")

DUTY_PROMPT = """你是一个智能助手，专注于帮助用户解决问题。你需要：
1. 理解用户的需求并提供准确的回答
2. 保持友好和专业的态度
3. 记住对话中的关键信息"""

CONSTRAINT_PROMPT = """1. 不得生成有害内容
2. 遵守法律法规
3. 不确定时诚实告知用户"""

FEW_SHOTS_PROMPT = ""
FALLBACK_PROMPT = """你是一个有用的 AI 助手，可以帮助用户解决各种问题。请记住对话中的重要信息。"""

# ============ 改进：按照消息的逻辑意义分类 ============

# 统计但不显示的消息类型（用于分析）
TRACKED_MESSAGE_TYPES = {
    "agent_new_run",          # 任务开始
    "step_count",              # 步骤计数
    "model_output_thinking",   # 思考过程
    "model_output",            # 模型输出
    "code_output",             # 代码执行结果
    "final_answer",            # 最终答案
    "error",                   # 错误
}

DEBUG_MODE = False


def process_agent_message(chunk: str) -> tuple[str, str]:
    """解析 agent_run 返回的 JSON 消息"""
    try:
        data = json.loads(chunk)
        return data.get("type", ""), data.get("content", "")
    except json.JSONDecodeError:
        return "", chunk


def build_system_prompt(
    duty: str = "",
    constraint: str = "",
    few_shots: str = "",
    tools: list = None,
    managed_agents: list = None,
    memory_list: list = None,
    knowledge_base_summary: str = "",
    language: str = "zh",
    is_manager: bool = False
) -> str:
    """构建 System Prompt"""
    tools = tools or []
    managed_agents = managed_agents or []
    memory_list = memory_list or []
    
    prompt_template = get_agent_prompt_template(is_manager=is_manager, language=language)
    template_content = prompt_template.get("system_prompt", "")
    
    tools_dict = {tool.name: tool for tool in tools}
    managed_agents_dict = {agent.name: agent for agent in managed_agents}
    
    system_prompt = Template(template_content, undefined=StrictUndefined).render({
        "duty": duty,
        "constraint": constraint,
        "few_shots": few_shots,
        "tools": tools_dict,
        "managed_agents": managed_agents_dict,
        "authorized_imports": str(BASE_BUILTIN_MODULES),
        "APP_NAME": APP_NAME,
        "APP_DESCRIPTION": APP_DESCRIPTION,
        "memory_list": memory_list,
        "knowledge_base_summary": knowledge_base_summary,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })
    
    return system_prompt


def build_prompt_templates(system_prompt: str, language: str = "zh", is_manager: bool = False) -> dict:
    """构建完整的 prompt_templates 字典"""
    prompt_templates = get_agent_prompt_template(is_manager=is_manager, language=language)
    prompt_templates["system_prompt"] = system_prompt
    return prompt_templates


def build_agent_run_info(query: str, history: list[AgentHistory]) -> AgentRunInfo:
    """构造 AgentRunInfo"""
    model_config = ModelConfig(
        cite_name="main_model",
        api_key=LLM_API_KEY,
        model_name=LLM_MODEL_NAME,
        url=LLM_API_URL,
        temperature=0.1,
    )
    workspace_path = os.path.join(os.getcwd(), "workspace")

    # 创建工具实例
    create_tool_instance = CreateFileTool(init_path=workspace_path)
    read_tool_instance = ReadFileTool(init_path=workspace_path)
    # create_tool_instance = CreateFileTool()
    # read_tool_instance = ReadFileTool()
    # 用于 system prompt 渲染
    tools_instances = [create_tool_instance, read_tool_instance]

    # 用于 AgentConfig，供 NexentAgent 实例化
    # 从工具实例中提取必要信息
    tools_config = [
        ToolConfig(
            class_name="CreateFileTool",
            name=create_tool_instance.name,
            description=create_tool_instance.description,
            inputs=str(create_tool_instance.inputs),
            output_type=create_tool_instance.output_type,
            source="local",
            params={"init_path": create_tool_instance.init_path}
        ),
        ToolConfig(
            class_name="ReadFileTool",
            name=read_tool_instance.name,
            description=read_tool_instance.description,
            inputs=str(read_tool_instance.inputs),
            output_type=read_tool_instance.output_type,
            source="local",
            params={"init_path": read_tool_instance.init_path}
        ),
    ]

    is_manager = False

    if DUTY_PROMPT or CONSTRAINT_PROMPT or FEW_SHOTS_PROMPT:
        system_prompt = build_system_prompt(
            duty=DUTY_PROMPT,
            constraint=CONSTRAINT_PROMPT,
            few_shots=FEW_SHOTS_PROMPT,
            tools=tools_instances,
            managed_agents=[],
            memory_list=[],
            knowledge_base_summary="",
            language="zh",
            is_manager=is_manager
        )
    else:
        system_prompt = FALLBACK_PROMPT

    prompt_templates = build_prompt_templates(
        system_prompt,
        language="zh",
        is_manager=is_manager
    )

    agent_config = AgentConfig(
        name="interactive_agent",
        description="交互式对话 Agent（支持文件操作）",
        tools=tools_config,
        max_steps=10,
        model_name="main_model",
        prompt_templates=prompt_templates,
        managed_agents=[]
    )

    return AgentRunInfo(
        query=query,
        model_config_list=[model_config],
        observer=MessageObserver(lang="zh"),
        agent_config=agent_config,
        mcp_host=None,
        history=history,
        stop_event=threading.Event(),
    )


async def interactive_chat():
    """
    交互式多轮对话主循环
    
    改进点：
    1. 只显示最终答案（final_answer）
    2. 正确的消息统计（按消息类型，而不是 chunk 总数）
    3. 清晰的对话历史
    """
    print("=" * 60)
    print("Nexent Agent 多轮对话测试（改进版）")
    print("输入 'exit' 或 'quit' 退出对话")
    print("=" * 60)
    print()
    
    conversation_history = []
    total_turns = 0
    
    while True:
        try:
            user_input = input("\n用户: ").strip()
            
            if not user_input:
                continue
            
            if user_input.lower() in ["exit", "quit"]:
                print("\n再见！")
                break
            
            agent_run_info = build_agent_run_info(user_input, conversation_history)
            
            print("\n助手: ", end="", flush=True)
            
            # ============ 改进的消息处理和统计 ============
            full_response = ""
            final_answer = ""
            
            # 按消息类型统计（而不是统计 chunk 总数）
            message_type_count = {}
            step_count = 0
            
            async for chunk in agent_run(agent_run_info):
                if not chunk:
                    continue
                
                # 解析消息
                msg_type, msg_content = process_agent_message(chunk)
                
                if DEBUG_MODE:
                    print(f"\n[DEBUG] Type={msg_type}, Content Length={len(msg_content)}", 
                          file=sys.stderr, flush=True)
                
                if msg_type in TRACKED_MESSAGE_TYPES:
                    message_type_count[msg_type] = message_type_count.get(msg_type, 0) + 1
                    
                    # 计算逻辑步骤（step）
                    if msg_type in ["step_count", "final_answer"]:
                        step_count += 1
                
                if msg_type == "final_answer":
                    # print(msg_content, end="", flush=True)
                    final_answer = msg_content
                    full_response += msg_content
                
                elif msg_type == "error":
                    print(f"\n❌ 错误: {msg_content}", end="", flush=True)
            
            if not final_answer:
                final_answer = full_response if full_response else "（未获得回应）"
            
            print()  # 换行
            
            # ============ 添加到对话历史 ============
            conversation_history.append(
                AgentHistory(role="user", content=user_input)
            )
            conversation_history.append(
                AgentHistory(role="assistant", content=final_answer)
            )
            
            total_turns = len(conversation_history) // 2
            total_turns += 1
            # ============ 改进的统计输出 ============
            print(f"\n[对话统计]")
            print(f"  当前轮数: {total_turns}")
            # print(f"  执行步骤: {step_count}")
            print(f"  消息类型分布: {dict(message_type_count)}")
            
        except KeyboardInterrupt:
            print("\n\n对话被中断，再见！")
            break
        except Exception as e:
            print(f"\n发生错误: {e}")
            import traceback
            traceback.print_exc()
            continue


async def test_with_mock_history():
    """使用模拟历史记录测试"""
    history = [
        AgentHistory(role="user", content="我叫张三，我是个工程师"),
        AgentHistory(role="assistant", content="你好张三，很高兴认识你！作为工程师，你一定有很多技术方面的问题吧。"),
    ]
    
    query = "你还记得我的名字和职业吗？"
    agent_run_info = build_agent_run_info(query, history)
    
    print("=== 开始运行（带历史记录）===\n")
    
    final_answer = ""
    message_type_count = {}
    
    async for chunk in agent_run(agent_run_info):
        if not chunk:
            continue
        
        msg_type, msg_content = process_agent_message(chunk)
        
        # 统计
        message_type_count[msg_type] = message_type_count.get(msg_type, 0) + 1
        
        # 只显示最终答案
        if msg_type == "final_answer":
            print(msg_content, end="", flush=True)
            final_answer = msg_content
    
    print("\n\n=== 运行结束 ===\n")
    print(f"最终答案：{final_answer[:100]}...")
    print(f"消息类型分布：{dict(message_type_count)}")


async def main():
    """主函数"""
    print("\n选择模式:")
    print("1. 交互式多轮对话")
    print("2. 测试历史记录功能")
    print("3. 启用调试模式的交互式对话")
    print("4. 退出")
    
    choice = input("\n请输入选择 (1/2/3/4): ").strip()
    
    global DEBUG_MODE
    
    if choice == "1":
        await interactive_chat()
    elif choice == "2":
        await test_with_mock_history()
    elif choice == "3":
        DEBUG_MODE = True
        await interactive_chat()
    elif choice == "4":
        print("退出程序")
    else:
        print("无效选择，启动交互式对话")
        await interactive_chat()


if __name__ == "__main__":
    asyncio.run(main())