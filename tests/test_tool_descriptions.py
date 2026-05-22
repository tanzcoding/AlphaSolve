"""tool_descriptions YAML 字段：suffix / override / parameters.description 的回归测试。"""
from __future__ import annotations

import pytest
from pathlib import Path

from alphasolve.agent import GeneralAgentConfig, ToolRegistry, ToolResult


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
    from alphasolve.agent.config import load_general_agent_config
    with pytest.raises(ValueError, match="mutually exclusive"):
        load_general_agent_config(yaml)


def test_tool_descriptions_field_default_empty():
    cfg = GeneralAgentConfig(name="t", system_prompt="hi")
    assert cfg.tool_descriptions == {}


def test_tool_descriptions_passthrough_when_none():
    """tool_defs 不传 tool_descriptions 时行为与之前完全一致。"""
    reg = _registry_with_one_tool()
    defs_old = reg.tool_defs(enabled=["Read"])
    defs_new = reg.tool_defs(enabled=["Read"], tool_descriptions=None)
    assert defs_old[0].description == defs_new[0].description
    assert defs_old[0].parameters == defs_new[0].parameters
