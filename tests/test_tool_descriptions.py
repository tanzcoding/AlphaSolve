"""tool_descriptions YAML 字段：suffix / override / parameters.description 的回归测试。"""
from __future__ import annotations

import pytest
from pathlib import Path

from alphasolve.agent import AgentConfig, ToolRegistry, ToolResult


def _registry_with_one_tool() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        name="Read",
        description="Read a file.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path."},
                "n_lines": {"type": "integer", "default": 100, "description": "Line count."},
            },
            "required": ["path"],
        },
        handler=lambda args: ToolResult("ok"),
    )
    return reg


def test_suffix_appends_to_description():
    reg = _registry_with_one_tool()
    defs = reg.tool_defs(
        enabled=["Read"],
        tool_descriptions={"Read": {"suffix": "Workspace restricted."}},
    )
    assert "Read a file." in defs[0].description
    assert defs[0].description.endswith("Workspace restricted.")


def test_override_replaces_description():
    reg = _registry_with_one_tool()
    defs = reg.tool_defs(
        enabled=["Read"],
        tool_descriptions={"Read": {"override": "REPLACED."}},
    )
    assert defs[0].description == "REPLACED."


def test_parameters_description_override():
    reg = _registry_with_one_tool()
    defs = reg.tool_defs(
        enabled=["Read"],
        tool_descriptions={"Read": {"parameters": {"n_lines": {"description": "Custom."}}}},
    )
    props = defs[0].parameters["properties"]
    assert props["n_lines"]["description"] == "Custom."
    assert props["path"]["description"] == "File path."  # 未覆盖


def test_register_rejects_duplicate_without_replace():
    reg = _registry_with_one_tool()
    with pytest.raises(ValueError, match="tool already registered"):
        reg.register(
            name="Read",
            description="replacement",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda args: ToolResult("new"),
        )


def test_register_replace_overrides_existing_tool():
    reg = _registry_with_one_tool()
    reg.register(
        name="Read",
        description="replacement",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=lambda args: ToolResult("new"),
        replace=True,
    )

    defs = reg.tool_defs(enabled=["Read"])
    result = reg.execute("Read", {})
    assert defs[0].description == "replacement"
    assert result.content == "new"


def test_suffix_and_override_mutually_exclusive(tmp_path: Path):
    yaml = tmp_path / "agent.yaml"
    yaml.write_text(
        """
version: 1
agent:
  name: test
  system_prompt: "hi"
  tools: [Read]
  tool_descriptions:
    Read:
      suffix: "x"
      override: "y"
""",
        encoding="utf-8",
    )
    from alphasolve.agent.config import load_agent_config
    with pytest.raises(ValueError, match="mutually exclusive"):
        load_agent_config(yaml)


def test_tool_descriptions_field_default_empty():
    cfg = AgentConfig(name="t", system_prompt="hi")
    assert cfg.tool_descriptions == {}


def test_tool_descriptions_passthrough_when_none():
    """tool_defs 不传 tool_descriptions 时行为与之前完全一致。"""
    reg = _registry_with_one_tool()
    defs_old = reg.tool_defs(enabled=["Read"])
    defs_new = reg.tool_defs(enabled=["Read"], tool_descriptions=None)
    assert defs_old[0].description == defs_new[0].description
    assert defs_old[0].parameters == defs_new[0].parameters


def test_parameters_description_skips_unknown_param():
    """YAML 笔误指向不存在的参数时应直接跳过，不应在 schema 里造一个没有 type 的空 prop。"""
    reg = _registry_with_one_tool()
    defs = reg.tool_defs(
        enabled=["Read"],
        tool_descriptions={"Read": {"parameters": {"nonexistent": {"description": "ghost"}}}},
    )
    props = defs[0].parameters["properties"]
    assert "nonexistent" not in props
    assert set(props.keys()) == {"path", "n_lines"}


def test_merge_suffix_clears_inherited_override():
    """base agent 设了 override，子 agent 设 suffix 时，merge 后只应剩 suffix。"""
    from alphasolve.agent.config import _merge_tool_descriptions

    target = {"Read": {"override": "BASE."}}
    _merge_tool_descriptions(target, {"Read": {"suffix": "child suffix"}})
    assert target == {"Read": {"suffix": "child suffix"}}


def test_merge_override_clears_inherited_suffix():
    """对称情况：base 设 suffix，子 agent 设 override 时，merge 后只应剩 override。"""
    from alphasolve.agent.config import _merge_tool_descriptions

    target = {"Read": {"suffix": "BASE suffix"}}
    _merge_tool_descriptions(target, {"Read": {"override": "child override"}})
    assert target == {"Read": {"override": "child override"}}
