import logging
from typing import Any, Callable, Dict

from pydantic import Field
from smolagents.tools import Tool

from ..utils.tools_common_message import ToolCategory, ToolSign

logger = logging.getLogger("set_state_tool")


class SetStateTool(Tool):
    name = "set_state"
    description = (
        "Save a session-scoped key-value fact in working memory. Use this for "
        "short-lived facts that should override conversational mentions within "
        "this conversation, such as the user's stated name, task target, current "
        "constraint, or in-progress state. This is not persistent across "
        "conversations; use store_memory for cross-session facts."
    )
    description_zh = (
        "保存当前会话范围内的键值状态。用于当前会话内应优先遵守的短期事实，"
        "例如用户姓名、任务目标、当前约束或进行中的状态。该信息不会跨会话长期保留；"
        "跨会话事实应使用 store_memory。"
    )
    inputs = {
        "key": {
            "type": "string",
            "description": "snake_case identifier, e.g. user_name or task_target",
            "description_zh": "snake_case 标识符，例如 user_name 或 task_target",
        },
        "value": {
            "type": "string",
            "description": "String value to keep verbatim",
            "description_zh": "需要原样保存的字符串值",
        },
    }
    output_type = "string"
    category = ToolCategory.DATABASE.value
    tool_sign = ToolSign.MEMORY_OPERATION.value

    def __init__(
        self,
        set_callback: Callable[[str, str], Dict[str, Any]] = Field(
            default=None, exclude=True
        ),
    ):
        super().__init__()
        self.set_callback = set_callback

    def forward(self, key: str, value: str) -> str:
        if not self.set_callback:
            return "Working memory is not available in this run."
        if not key or not key.strip():
            return "Failed to store session state: key must not be empty."
        if value is None:
            return "Failed to store session state: value must not be empty."

        try:
            result = self.set_callback(key.strip(), str(value))
            return _format_set_result(result)
        except Exception as e:
            logger.error(f"set_state failed: {e}")
            return f"Working memory transient failure; state was not persisted: {e}"


def _format_set_result(result: Dict[str, Any]) -> str:
    key = result.get("key", "")
    previous = result.get("previous")
    truncated = result.get("truncated", False)
    evicted_key = result.get("evicted_key")
    kv = result.get("kv") or {}

    lines = [
        f"OK. Stored key '{key}' (previous: {previous if previous is not None else '<none>'})."
    ]
    if truncated:
        lines.append("Note: value was truncated to fit the working memory limit.")
    if evicted_key:
        lines.append(f"Note: evicted oldest key '{evicted_key}' due to working memory size limit.")
    lines.append("Current session state:")
    if kv:
        lines.extend(f"- {k}: {v}" for k, v in kv.items())
    else:
        lines.append("- <empty>")
    return "\n".join(lines)
