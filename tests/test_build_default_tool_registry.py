"""第二层 build_default_tool_registry 注册 12 个基础工具的测试。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alphasolve.agent import Workspace  # noqa: E402
from alphasolve.agent.tool_registry import build_default_tool_registry  # noqa: E402


@pytest.fixture
def registry(tmp_path: Path):
    ws = Workspace(root=tmp_path)
    return build_default_tool_registry(ws)


def test_registers_all_basic_tools(registry):
    names = {tool.name for tool in registry.registered_tools()}
    expected = {
        "Read", "Write", "Edit", "MakeDir", "Rename", "Move", "Delete",
        "Glob", "ListDir", "Grep", "GetCurrentTime",
    }
    # Bash 或 Shell（按平台二选一）
    shell_tool = ({"Bash"} & names) or ({"Shell"} & names)
    assert shell_tool, "Bash or Shell must be registered"
    missing = expected - names
    assert not missing, f"missing tools: {missing}"


def test_bash_unrestricted_when_available(tmp_path: Path):
    """Bash 解除 ls 限制（如果平台有 bash）。"""
    from alphasolve.utils.shell import has_bash
    if not has_bash():
        pytest.skip("bash not available on this platform")
    ws = Workspace(root=tmp_path)
    registry = build_default_tool_registry(ws)
    bash_tool = next(t for t in registry.registered_tools() if t.name == "Bash")
    # 调用 echo（非 ls）应当成功
    result = bash_tool.handler({"command": "echo hello"})
    assert "hello" in result.content
    assert not result.is_error


def test_write_mode_in_parameters(registry):
    write_tool = next(t for t in registry.registered_tools() if t.name == "Write")
    mode_param = write_tool.parameters["properties"]["mode"]
    assert mode_param["enum"] == ["overwrite", "append"]
    assert mode_param["default"] == "overwrite"
