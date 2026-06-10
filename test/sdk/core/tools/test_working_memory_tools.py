from sdk.nexent.core.tools.delete_state_tool import DeleteStateTool
from sdk.nexent.core.tools.set_state_tool import SetStateTool


def test_set_state_tool_returns_current_snapshot():
    def cb(key, value):
        return {
            "key": key,
            "previous": None,
            "current": value,
            "truncated": False,
            "evicted_key": None,
            "kv": {"task_target": value},
        }

    tool = SetStateTool(set_callback=cb)
    result = tool.forward("task_target", "prepare report")

    assert "Stored key 'task_target'" in result
    assert "- task_target: prepare report" in result


def test_set_state_tool_reports_transient_failure():
    def cb(key, value):
        raise RuntimeError("redis timeout")

    tool = SetStateTool(set_callback=cb)
    result = tool.forward("task_target", "prepare report")

    assert "transient failure" in result
    assert "not persisted" in result


def test_delete_state_tool_returns_current_snapshot():
    def cb(key):
        return {"key": key, "deleted": True, "kv": {"user_name": "Alice"}}

    tool = DeleteStateTool(delete_callback=cb)
    result = tool.forward("task_target")

    assert "Deleted key 'task_target'" in result
    assert "- user_name: Alice" in result
