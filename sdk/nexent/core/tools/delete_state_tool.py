import logging
from typing import Any, Callable, Dict

from pydantic import Field
from smolagents.tools import Tool

from ..utils.tools_common_message import ToolCategory, ToolSign

logger = logging.getLogger("delete_state_tool")


class DeleteStateTool(Tool):
    name = "delete_state"
    description = (
        "Delete an obsolete or incorrect short-lived working memory key for the "
        "current conversation. Use when the user says a prior goal, constraint, "
        "decision, or temporary fact is no longer valid."
    )
    description_zh = (
        "删除当前会话短期工作记忆中已过期或错误的 key。"
        "当用户说明之前的目标、约束、决策或临时事实不再有效时使用。"
    )
    inputs = {
        "key": {
            "type": "string",
            "description": "snake_case key to delete",
            "description_zh": "要删除的 snake_case key",
        }
    }
    output_type = "string"
    category = ToolCategory.DATABASE.value
    tool_sign = ToolSign.MEMORY_OPERATION.value

    def __init__(
        self,
        delete_callback: Callable[[str], Dict[str, Any]] = Field(
            default=None, exclude=True
        ),
    ):
        super().__init__()
        self.delete_callback = delete_callback

    def forward(self, key: str) -> str:
        if not self.delete_callback:
            return "Working memory is not available in this run."
        if not key or not key.strip():
            return "Failed to delete session state: key must not be empty."

        try:
            result = self.delete_callback(key.strip())
            return _format_delete_result(result)
        except Exception as e:
            logger.error(f"delete_state failed: {e}")
            return f"Working memory transient failure; state was not persisted: {e}"


def _format_delete_result(result: Dict[str, Any]) -> str:
    key = result.get("key", "")
    deleted = result.get("deleted", False)
    kv = result.get("kv") or {}

    lines = [
        f"OK. Deleted key '{key}'." if deleted else f"OK. Key '{key}' was not present."
    ]
    lines.append("Current session state:")
    if kv:
        lines.extend(f"- {k}: {v}" for k, v in kv.items())
    else:
        lines.append("- <empty>")
    return "\n".join(lines)
