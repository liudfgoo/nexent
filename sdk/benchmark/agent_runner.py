# -*- coding: utf-8 -*-
"""
Shared utilities for building and running nexent agents in benchmarks.

Provides:
1. Prompt construction (system prompt, prompt templates)
2. AgentRunInfo construction (standard and custom-prompt variants)
3. Message-stream processing and statistics
"""
import sys
import io
import json
import os
import re
from datetime import datetime
from typing import AsyncIterator, Callable, Optional

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from jinja2 import Template, StrictUndefined
from smolagents.utils import BASE_BUILTIN_MODULES
from dotenv import load_dotenv
import string

# ============ Environment Setup ============
# Add parent directory to sys.path so paths.py can be found, then import it.
# paths.py resolves PROJECT_ROOT/SDK_DIR/BACKEND_DIR via .git discovery and
# injects them into sys.path automatically — no manual path manipulation needed.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths  # noqa: F401 — side-effect: adds sdk/, backend/ to sys.path

from utils.prompt_template_utils import get_agent_prompt_template
from utils.context_utils import build_context_components, build_system_prompt_component
from nexent.core.agents.agent_model import (
    AgentRunInfo, AgentConfig, ModelConfig, AgentHistory, ToolConfig
)



from nexent.core.agents.run_agent import agent_run
from nexent.core.utils.observer import MessageObserver
from nexent.core.agents.agent_context import ContextManagerConfig
import logging
logging.getLogger("smolagents").setLevel(logging.WARNING)
import random
load_dotenv()

# ============ Global Configuration ============
LLM_API_KEY = os.getenv("LLM_API_KEY")
LLM_MODEL_NAME = os.getenv("LLM_MODEL_NAME")
LLM_API_URL = os.getenv("LLM_API_URL")

# Disable model thinking for benchmark runs. Both vendor dialects are kept in
# one payload so the same agent_runner.py works against either backend without
# code changes: Qwen-on-vLLM/SGLang reads `chat_template_kwargs.enable_thinking`
# and ignores `thinking`; Anthropic reads `thinking.type` and ignores
# `chat_template_kwargs`. Unknown keys are silently dropped by each provider.
THINKING_OFF_EXTRA_BODY = {
    "chat_template_kwargs": {"enable_thinking": False},
    "thinking": {"type": "disabled"},
}

APP_NAME = os.getenv("APP_NAME", "Nexent")
APP_DESCRIPTION = os.getenv("APP_DESCRIPTION", "Nexent is an open-source agent SDK and platform")

# ============ Default Prompt Templates ============
DEFAULT_DUTY_PROMPT = """You are an intelligent assistant focused on helping users solve problems. You need to:
1. Understand the user's needs and provide accurate answers
2. Maintain a friendly and professional attitude
3. Remember key information from the conversation"""

DEFAULT_CONSTRAINT_PROMPT = """1. Do not generate harmful content
2. Comply with laws and regulations
3. Be honest with users when uncertain"""

DEFAULT_FEW_SHOTS_PROMPT = ""

DEFAULT_FALLBACK_PROMPT = """You are a helpful AI assistant that can help users solve various problems. Please remember important information from the conversation."""

# ============ Message Type Constants ============
TRACKED_MESSAGE_TYPES = {
    "agent_new_run",
    "step_count",
    "model_output",
    "model_output_thinking",
    "model_output_deep_thinking",
    "model_output_code",
    "parse",
    "execution_logs",
    "final_answer",
    "error",
    "token_count",
}


# ============ Prompt Construction Functions ============

def build_system_prompt(
    duty: str = "",
    constraint: str = "",
    few_shots: str = "",
    tools: list = None,
    managed_agents: list = None,
    memory_list: list = None,
    knowledge_base_summary: str = "",
    language: str = "zh",
    is_manager: bool = False,
    user_id: str = "",
    skills: list = None
) -> str:
    """
    Build System Prompt

    Args:
        duty: Duty description
        constraint: Constraints
        few_shots: Few-shot examples
        tools: Tool list
        managed_agents: Managed sub-agent list
        memory_list: Memory list
        knowledge_base_summary: Knowledge base summary
        language: Language (zh/en)
        is_manager: Whether this is a manager agent

    Returns:
        Rendered system prompt string
    """
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
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": user_id,
        "skills": skills or []
    })

    return system_prompt


def build_prompt_templates(
    system_prompt: str,
    language: str = "zh",
    is_manager: bool = False
) -> dict:
    """
    Build complete prompt_templates dict

    Args:
        system_prompt: System prompt string
        language: Language
        is_manager: Whether this is a manager agent

    Returns:
        prompt_templates dict
    """
    prompt_templates = get_agent_prompt_template(is_manager=is_manager, language=language)
    prompt_templates["system_prompt"] = system_prompt
    return prompt_templates


# ============ AgentRunInfo Construction Functions ============

def build_agent_run_info(
    query: str,
    history: list[AgentHistory],
    duty_prompt: str = "",
    constraint_prompt: str = "",
    few_shots_prompt: str = "",
    fallback_prompt: str = "",
    tools: list = None,
    managed_agents: list = None,
    max_steps: int = 10,
    temperature: float = 0.1,
    agent_name: str = "test_agent",
    agent_description: str = "Test Agent",
    language: str = "zh",
    is_manager: bool = False,
    context_manager_config: Optional[ContextManagerConfig] = None,
    user_id: str = "",
    skills: list = None,
    max_tokens: Optional[int] = None,
) -> AgentRunInfo:
    """
    Construct AgentRunInfo with template-based system prompt.

    Args:
        query: User query
        history: Conversation history
        duty_prompt: Duty prompt (empty uses default)
        constraint_prompt: Constraint prompt (empty uses default)
        few_shots_prompt: Few-shot prompt
        fallback_prompt: Fallback prompt (empty uses default)
        tools: Tool list
        managed_agents: Managed sub-agent list
        max_steps: Max execution steps
        temperature: Temperature parameter
        agent_name: Agent name
        agent_description: Agent description
        language: Language
        is_manager: Whether this is a manager agent
        context_manager_config: Context manager config (None uses default)
        user_id: User ID
        skills: Skill list
        max_tokens: Per-call completion output cap forwarded to the main LLM.
                    Default None leaves the provider default (unbounded /
                    model max), matching the SDK back-port. Benchmarks that
                    want to bound runaway / degenerate-loop probes set this
                    explicitly (e.g. 4096).

    Returns:
        AgentRunInfo object
    """
    # Use defaults
    duty = duty_prompt or DEFAULT_DUTY_PROMPT
    constraint = constraint_prompt or DEFAULT_CONSTRAINT_PROMPT
    few_shots = few_shots_prompt or DEFAULT_FEW_SHOTS_PROMPT
    fallback = fallback_prompt or DEFAULT_FALLBACK_PROMPT
    tools = tools or []
    managed_agents = managed_agents or []

    model_config = ModelConfig(
        cite_name="main_model",
        api_key=LLM_API_KEY,
        model_name=LLM_MODEL_NAME,
        url=LLM_API_URL,
        temperature=temperature,
        ssl_verify=False,
        extra_body=THINKING_OFF_EXTRA_BODY,
        max_tokens=max_tokens,
    )

    if duty or constraint or few_shots:
        system_prompt = build_system_prompt(
            duty=duty,
            constraint=constraint,
            few_shots=few_shots,
            tools=tools,
            managed_agents=managed_agents,
            memory_list=[],
            knowledge_base_summary="",
            language=language,
            is_manager=is_manager,
            user_id=user_id,
            skills=skills
        )
    else:
        system_prompt = fallback

    prompt_templates = build_prompt_templates(
        system_prompt,
        language=language,
        is_manager=is_manager
    )

    # Set context manager config
    cm_config = context_manager_config

    # Build context components when ContextManager is enabled, matching
    # production behavior in create_agent_info.py. Without components,
    # ManagedContextRuntime produces empty stable_messages and the system
    # prompt gets silently dropped by _without_leading_stable_messages.
    context_components = None
    if cm_config and cm_config.enabled:
        tools_dict = {tool.name: tool for tool in tools} if tools else {}
        managed_agents_dict = {agent.name: agent for agent in managed_agents} if managed_agents else {}
        context_components = build_context_components(
            duty=duty,
            constraint=constraint,
            few_shots=few_shots,
            app_name=APP_NAME,
            app_description=APP_DESCRIPTION,
            user_id=user_id,
            language=language,
            is_manager=is_manager,
            tools=tools_dict,
            skills=skills or [],
            managed_agents=managed_agents_dict,
            external_a2a_agents={},
            memory_list=[],
            memory_search_query=None,
            knowledge_base_summary="",
            kb_ids=[],
        )

    agent_config = AgentConfig(
        name=agent_name,
        description=agent_description,
        tools=tools,
        max_steps=max_steps,
        model_name="main_model",
        prompt_templates=prompt_templates,
        managed_agents=managed_agents,
        context_manager_config=cm_config,
        context_components=context_components,
    )


    import threading
    return AgentRunInfo(
        query=query,
        model_config_list=[model_config],
        observer=MessageObserver(lang=language),
        agent_config=agent_config,
        mcp_host=None,
        history=history,
        stop_event=threading.Event(),
    )


def build_agent_run_info_with_custom_prompt(
    query: str,
    system_prompt: str,
    history: list[AgentHistory],
    tools: list = None,
    managed_agents: list = None,
    max_steps: int = 10,
    temperature: float = 0.1,
    agent_name: str = "test_agent",
    agent_description: str = "Test Agent",
    language: str = "en",
    is_manager: bool = False,
    context_manager_config: Optional[ContextManagerConfig] = None,
) -> AgentRunInfo:
    """
    Build AgentRunInfo with a pre-rendered system prompt string.

    Unlike build_agent_run_info which renders the system prompt via Jinja2 template,
    this function accepts the final system prompt directly, bypassing the template
    engine entirely. Use this for benchmark scenarios that need a specialized prompt
    without the standard platform scaffolding.

    Args:
        query: User query
        system_prompt: Pre-rendered system prompt string (used as-is)
        history: Conversation history
        tools: Tool list
        managed_agents: Managed sub-agents
        max_steps: Max execution steps
        temperature: Temperature parameter
        agent_name: Agent name
        agent_description: Agent description
        language: Language
        is_manager: Whether this is a manager agent
        context_manager_config: Context manager config

    Returns:
        AgentRunInfo object
    """
    tools = tools or []
    managed_agents = managed_agents or []

    model_config = ModelConfig(
        cite_name="main_model",
        api_key=LLM_API_KEY,
        model_name=LLM_MODEL_NAME,
        url=LLM_API_URL,
        temperature=temperature,
        ssl_verify=False,
        extra_body=THINKING_OFF_EXTRA_BODY,
        )

    prompt_templates = build_prompt_templates(
        system_prompt,
        language=language,
        is_manager=is_manager,
    )

    # Wrap custom system prompt as a single SystemPromptComponent when
    # ContextManager is enabled, so it becomes a stable_message and survives
    # _without_leading_stable_messages stripping.
    context_components = None
    if context_manager_config and context_manager_config.enabled:
        context_components = [
            build_system_prompt_component(
                content=system_prompt,
                template_name="custom_prompt",
                priority=100,
            )
        ]

    agent_config = AgentConfig(
        name=agent_name,
        description=agent_description,
        tools=tools,
        max_steps=max_steps,
        model_name="main_model",
        prompt_templates=prompt_templates,
        managed_agents=managed_agents,
        context_manager_config=context_manager_config,
        context_components=context_components,
    )

    import threading
    return AgentRunInfo(
        query=query,
        model_config_list=[model_config],
        observer=MessageObserver(lang=language),
        agent_config=agent_config,
        mcp_host=None,
        history=history,
        stop_event=threading.Event(),
    )


# ============ YAML Tool Configuration ============

# Tools whose metadata depends on external services (DB, knowledge base engines,
# memory stores) that are not available in a standalone benchmark environment.
_METADATA_UNSUPPORTED_TOOLS = {
    "KnowledgeBaseSearchTool",
    "DifySearchTool",
    "DataMateSearchTool",
    "HaotianSearchTool",
    "StoreMemoryTool",
    "SearchMemoryTool",
}

# Analyze tools need storage_client / vlm_model / data_process_service_url
# injected via metadata. We construct these from environment variables.
_ANALYZE_TOOL_CLASSES = {
    "AnalyzeTextFileTool",
    "AnalyzeImageTool",
    "AnalyzeAudioTool",
    "AnalyzeVideoTool",
}


def _build_storage_client():
    endpoint = os.getenv("MINIO_ENDPOINT")
    access_key = os.getenv("MINIO_ACCESS_KEY")
    secret_key = os.getenv("MINIO_SECRET_KEY")
    if not all([endpoint, access_key, secret_key]):
        return None
    from nexent.storage.minio import MinIOStorageClient
    return MinIOStorageClient(
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        region=os.getenv("MINIO_REGION"),
        default_bucket=os.getenv("MINIO_DEFAULT_BUCKET"),
        secure=os.getenv("MINIO_SECURE", "true").lower() == "true",
    )


def _build_vlm_model():
    api_url = os.getenv("VLM_API_URL") or os.getenv("LLM_API_URL")
    api_key = os.getenv("VLM_API_KEY") or os.getenv("LLM_API_KEY")
    model_name = os.getenv("VLM_MODEL_NAME") or os.getenv("LLM_MODEL_NAME")
    if not all([api_url, api_key, model_name]):
        return None
    from nexent.core.models.openai_vlm import OpenAIVLModel
    return OpenAIVLModel(
        observer=MessageObserver(),
        model_id=model_name,
        api_base=api_url,
        api_key=api_key,
        temperature=0.7,
        ssl_verify=False,
    )


def _build_llm_model():
    """Construct an OpenAILongContextModel instance for AnalyzeTextFileTool.

    Mirrors the production path in file_management_service.get_llm_model()
    which correctly passes a model *object* (not a string) to the tool.
    """
    api_url = os.getenv("LLM_API_URL")
    api_key = os.getenv("LLM_API_KEY")
    model_name = os.getenv("LLM_MODEL_NAME")
    if not all([api_url, api_key, model_name]):
        return None
    from nexent.core.models.openai_long_context_model import OpenAILongContextModel
    max_tokens = os.getenv("LLM_MAX_TOKENS")
    return OpenAILongContextModel(
        observer=MessageObserver(),
        model_id=model_name,
        api_base=api_url,
        api_key=api_key,
        max_context_tokens=int(max_tokens) if max_tokens else 128000,
        ssl_verify=False,
    )


def _build_analyze_tool_metadata(class_name: str) -> dict:
    """Construct metadata dict for Analyze* tools from environment variables.

    NexentAgent.create_local_tool() reads these metadata keys and sets them
    on the tool instance after construction. observer is auto-injected by
    NexentAgent and does not need to be in metadata.
    """
    metadata = {}

    storage_client = _build_storage_client()
    if storage_client:
        metadata["storage_client"] = storage_client

    if class_name == "AnalyzeTextFileTool":
        llm_model = _build_llm_model()
        if llm_model:
            metadata["llm_model"] = llm_model
        data_process_url = os.getenv("DATA_PROCESS_SERVICE")
        if data_process_url:
            metadata["data_process_service_url"] = data_process_url
    else:
        vlm_model = _build_vlm_model()
        if vlm_model:
            metadata["vlm_model"] = vlm_model

    return metadata


def build_tools_from_yaml(tools_yaml: list) -> list[ToolConfig]:
    """Reconstruct ToolConfig objects from exported YAML tool entries.

    Args:
        tools_yaml: List of tool dicts from YAML 'tools' section.
                    Each entry has: tool_name, tool_class, tool_source,
                    tool_description, tool_params, enabled.

    Returns:
        List of ToolConfig objects ready for make_nexent_task(tools=...).
        Analyze* tools get metadata constructed from environment variables.
        Tools depending on external services (KB, memory) are skipped with a warning.
    """
    if not tools_yaml:
        return []

    tool_configs = []
    skipped = []

    for entry in tools_yaml:
        if not entry.get("enabled", True):
            continue

        class_name = entry.get("tool_class", "")
        tool_name = entry.get("tool_name", "")
        source = entry.get("tool_source", "local")

        if class_name in _METADATA_UNSUPPORTED_TOOLS:
            skipped.append(f"{tool_name} ({class_name})")
            continue

        metadata = None
        if class_name in _ANALYZE_TOOL_CLASSES:
            metadata = _build_analyze_tool_metadata(class_name)

        tool_configs.append(ToolConfig(
            class_name=class_name,
            name=tool_name,
            description=entry.get("tool_description", ""),
            inputs=entry.get("tool_inputs"),
            output_type=entry.get("tool_output_type"),
            params=entry.get("tool_params", {}),
            source=source,
            usage=entry.get("tool_usage"),
            metadata=metadata if metadata else None,
        ))

    if skipped:
        print(f"  WARNING: Skipped {len(skipped)} tools requiring external services: "
              f"{', '.join(skipped)}")

    return tool_configs


# ============ Message Processing Functions ============

def process_agent_message(chunk: str) -> tuple[str, str]:
    """
    Parse JSON message returned by agent_run

    Args:
        chunk: JSON string

    Returns:
        (message_type, message_content) tuple
    """
    try:
        data = json.loads(chunk)
        return data.get("type", ""), data.get("content", "")
    except json.JSONDecodeError:
        return "", chunk


class AgentRunResult:
    """Agent run result wrapper"""
    def __init__(self):
        self.final_answer: str = ""
        self.full_response: str = ""
        self.message_type_count: dict = {}
        self.step_count: int = 0
        self.errors: list = []
        self.total_input_tokens: int = 0       # estimated (includes system prompt)
        self.total_api_input_tokens: int = 0   # API-reported (may exclude cached system prompt)
        self.total_output_tokens: int = 0
        self.steps: list = []
        self.compression_calls: int = 0
        self.compression_input_tokens: int = 0
        self.compression_output_tokens: int = 0
        self.compression_cache_hits: int = 0
        self.compression_cache_types: list = []
        self.total_uncompressed_est_tokens: int = 0

    def __repr__(self):
        return f"AgentRunResult(final_answer_len={len(self.final_answer)}, " \
               f"steps={self.step_count}, types={self.message_type_count})"


async def run_agent_with_tracking(
    agent_run_info: AgentRunInfo,
    on_final_answer: Optional[Callable[[str], None]] = None,
    on_error: Optional[Callable[[str], None]] = None,
    debug: bool = False
) -> AgentRunResult:
    """
    Run Agent and track message statistics

    Args:
        agent_run_info: Agent run info
        on_final_answer: Callback when final_answer is received
        on_error: Callback when error is received
        debug: Whether to print debug info

    Returns:
        AgentRunResult object containing final result and statistics

    Example:
        >>> result = await run_agent_with_tracking(agent_run_info)
        >>> print(result.final_answer)
        >>> print(result.message_type_count)
    """
    result = AgentRunResult()
    current_step = None
    initial_query = agent_run_info.query

    async for chunk in agent_run(agent_run_info):
        if not chunk:
            continue

        msg_type, msg_content = process_agent_message(chunk)

        if debug:
            print(f"[DEBUG] Type={msg_type}, Content Length={len(msg_content)}",
                  file=sys.stderr, flush=True)

        if msg_type in TRACKED_MESSAGE_TYPES:
            result.message_type_count[msg_type] = result.message_type_count.get(msg_type, 0) + 1

            if msg_type == "step_count":
                result.step_count += 1
                current_step = {
                    "step_number": msg_content,
                    "query": initial_query if result.step_count == 1 else "",
                    "thinking": "",
                    "deep_thinking": "",
                    "main_output": "",
                    "code": "",
                    "tool_call": "",
                    "observation": "",
                    "token_usage": None,
                }
                result.steps.append(current_step)

        if msg_type == "model_output_thinking" and current_step is not None:
            current_step["thinking"] += msg_content

        if msg_type == "model_output_deep_thinking" and current_step is not None:
            current_step["deep_thinking"] += msg_content

        if msg_type == "model_output" and current_step is not None:
            current_step["main_output"] += msg_content

        if msg_type == "model_output_code" and current_step is not None:
            current_step["code"] += msg_content

        if msg_type == "parse" and current_step is not None:
            current_step["tool_call"] += msg_content

        if msg_type == "execution_logs" and current_step is not None:
            current_step["observation"] += msg_content

        if msg_type == "final_answer":
            result.final_answer = msg_content
            result.full_response += msg_content
            result.steps.append({
                "step_number": "final_answer",
                "query": initial_query,
                "thinking": "",
                "deep_thinking": "",
                "main_output": msg_content,
                "code": "",
                "tool_call": "",
                "observation": "",
                "token_usage": None,
            })
            if on_final_answer:
                on_final_answer(msg_content)

        elif msg_type == "error":
            result.errors.append(msg_content)
            if on_error:
                on_error(msg_content)

        elif msg_type == "token_count":
            try:
                token_data = json.loads(msg_content)
                # Use estimated_context_tokens (includes system prompt + tools +
                # full context) for total_input_tokens.  API-reported
                # step_input_tokens may exclude cached system prompt tokens
                # (provider-dependent), so it is tracked separately.
                est_ctx = token_data.get("estimated_context_tokens")
                api_input = token_data.get("step_input_tokens", 0) or 0
                result.total_input_tokens += (est_ctx or api_input or 0)
                result.total_api_input_tokens += api_input
                result.total_output_tokens += token_data.get("step_output_tokens", 0) or 0

                result.compression_calls += token_data.get("compression_calls", 0) or 0
                result.compression_input_tokens += token_data.get("compression_input_tokens", 0) or 0
                result.compression_output_tokens += token_data.get("compression_output_tokens", 0) or 0
                result.compression_cache_hits += token_data.get("compression_cache_hits", 0) or 0
                result.total_uncompressed_est_tokens += token_data.get("uncompressed_est_tokens", 0) or 0
                cache_types = token_data.get("compression_cache_types", []) or []
                for ct in cache_types:
                    if ct not in result.compression_cache_types:
                        result.compression_cache_types.append(ct)

                if current_step is not None:
                    est_ctx = token_data.get("estimated_context_tokens")
                    api_in = token_data.get("step_input_tokens", 0)
                    current_step["token_usage"] = {
                        "input_tokens": est_ctx or api_in or 0,
                        "api_input_tokens": api_in,
                        "output_tokens": token_data.get("step_output_tokens", 0),
                    }
                    current_step["compression"] = {
                        "calls": token_data.get("compression_calls", 0),
                        "input_tokens": token_data.get("compression_input_tokens", 0),
                        "output_tokens": token_data.get("compression_output_tokens", 0),
                        "cache_hits": token_data.get("compression_cache_hits", 0),
                        "cache_types": token_data.get("compression_cache_types", []),
                        "ratio": token_data.get("compression_ratio", 0.0),
                        "uncompressed_est_tokens": token_data.get("uncompressed_est_tokens", 0),
                        "estimated_context_tokens": token_data.get("estimated_context_tokens"),
                        "token_threshold": token_data.get("token_threshold"),
                    }
            except (json.JSONDecodeError, TypeError):
                pass

    # Fallback when no final answer
    if not result.final_answer:
        result.final_answer = result.full_response if result.full_response else "(No response received)"

    return result




def parse_conversation_to_history(file_path: str) -> list[AgentHistory]:
    """
    Parse a JSON conversation file into a list of AgentHistory objects.

    Expected format: [{"role": "user"|"assistant", "content": "..."}, ...]

    Args:
        file_path: Path to a .json conversation file.

    Returns:
        List of AgentHistory objects in conversation order.

    Raises:
        ValueError: If file is not a .json file.
    """
    if not file_path.endswith(".json"):
        raise ValueError(
            f"Only .json conversation files are supported, got: {file_path}"
        )

    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    return [AgentHistory(role=entry["role"], content=entry["content"]) for entry in data]