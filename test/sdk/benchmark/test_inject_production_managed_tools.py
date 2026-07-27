"""Tests for inject_production_managed_tools in agent_runner.

Verifies that the Generic Benchmark passively injects the same builtin skill
tools that production's create_agent_info.py adds automatically, matching
class names, tool names, descriptions, inputs, source, usage, metadata,
and params.
"""
from types import SimpleNamespace

from sdk.benchmark import agent_runner


def test_builtin_skill_tools_are_injected_with_runtime_scope():
    """Production injects 4 builtin skill tools; benchmark must mirror that."""
    configured = SimpleNamespace(name="search")

    tools = agent_runner.inject_production_managed_tools(
        [configured],
        agent_id=8,
        tenant_id="tenant-a",
        version_no=2,
        local_skills_dir="/skills",
    )

    assert [tool.name for tool in tools] == [
        "search",
        "run_skill_script",
        "read_skill_md",
        "read_skill_config",
        "write_skill_file",
    ]
    injected = tools[1]
    assert injected.class_name == "RunSkillScriptTool"
    assert injected.source == "builtin"
    assert injected.usage == "builtin"
    assert injected.metadata["agent_id"] == 8
    assert injected.metadata["tenant_id"] == "tenant-a"
    assert injected.metadata["version_no"] == 2
    assert injected.metadata["_benchmark_assembly_origin"] == "injected_builtin"
    assert injected.params["local_skills_dir"] == "/skills"


def test_injection_skips_tools_already_present_by_name():
    """YAML tools with the same name as builtin tools must not be duplicated."""
    existing = SimpleNamespace(name="run_skill_script")

    tools = agent_runner.inject_production_managed_tools(
        [existing],
        agent_id=1,
        tenant_id="t",
        version_no=0,
        local_skills_dir=None,
    )

    names = [tool.name for tool in tools]
    assert names.count("run_skill_script") == 1
    # The other 3 should still be injected
    assert "read_skill_md" in names
    assert "read_skill_config" in names
    assert "write_skill_file" in names


def test_injection_with_empty_existing_tools():
    """All 4 builtin tools injected when no existing tools."""
    tools = agent_runner.inject_production_managed_tools(
        [],
        agent_id=0,
        tenant_id="tenant_id",
        version_no=0,
        local_skills_dir=None,
    )

    assert len(tools) == 4
    assert [t.name for t in tools] == [
        "run_skill_script",
        "read_skill_md",
        "read_skill_config",
        "write_skill_file",
    ]
    for tool in tools:
        assert tool.source == "builtin"
        assert tool.output_type == "string"
        assert tool.params == {"local_skills_dir": None}


def test_injection_matches_production_tool_definitions():
    """Verify class_name, name, description, and inputs match production exactly."""
    tools = agent_runner.inject_production_managed_tools(
        [],
        agent_id=1,
        tenant_id="t",
        version_no=1,
        local_skills_dir="/opt/skills",
    )

    expected = [
        {
            "class_name": "RunSkillScriptTool",
            "name": "run_skill_script",
            "description": (
                "Execute a skill script with given parameters. Use this to run "
                "Python or shell scripts that are part of a skill."
            ),
            "inputs": '{"skill_name": "str", "script_path": "str", "params": "dict"}',
        },
        {
            "class_name": "ReadSkillMdTool",
            "name": "read_skill_md",
            "description": (
                "Read skill execution guide and optional additional files. Always "
                "reads SKILL.md first, then optionally reads additional files."
            ),
            "inputs": '{"skill_name": "str", "additional_files": "list[str]"}',
        },
        {
            "class_name": "ReadSkillConfigTool",
            "name": "read_skill_config",
            "description": (
                "Read the config.yaml file from a skill directory. Returns JSON "
                "containing configuration variables needed for skill workflows."
            ),
            "inputs": '{"skill_name": "str"}',
        },
        {
            "class_name": "WriteSkillFileTool",
            "name": "write_skill_file",
            "description": (
                "Write content to a file within a skill directory. Creates parent "
                "directories if they do not exist."
            ),
            "inputs": '{"skill_name": "str", "file_path": "str", "content": "str"}',
        },
    ]

    for tool, exp in zip(tools, expected):
        assert tool.class_name == exp["class_name"]
        assert tool.name == exp["name"]
        assert tool.description == exp["description"]
        assert tool.inputs == exp["inputs"]


def test_build_tools_from_yaml_skips_runtime_metadata_when_disabled():
    """Snapshot-only callers disable runtime metadata initialization."""
    yaml_tools = [
        {
            "tool_class": "AnalyzeTextFileTool",
            "tool_name": "analyze_text",
            "tool_source": "local",
            "tool_description": "Analyze text",
            "enabled": True,
        },
    ]

    # With runtime metadata enabled (default), metadata would be built
    # (may be None if env vars are not set, but the code path is exercised)
    tools_with = agent_runner.build_tools_from_yaml(yaml_tools)
    assert len(tools_with) == 1

    # With runtime metadata disabled, metadata is always None
    tools_without = agent_runner.build_tools_from_yaml(
        yaml_tools, include_runtime_metadata=False
    )
    assert len(tools_without) == 1
    assert tools_without[0].metadata is None
