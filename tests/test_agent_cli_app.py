"""--agent CLI 入口的最小单测：构造 + run_once 不崩，且不引入 solver 依赖。"""
from __future__ import annotations

import ast
from pathlib import Path

from alphasolve.agent import AgentConfig
from alphasolve.agent.ui.cli_app import AgentApp
from alphasolve.llm.types import CompletionResponse, Message


class _StubClient:
    def complete(self, *, messages, tools, delta_sink=None):
        return CompletionResponse(
            message=Message(role="assistant", content="hello from stub"),
            finish_reason="stop",
        )


def test_agent_app_run_once_returns_result(tmp_path: Path):
    app = AgentApp(
        project_dir=tmp_path,
        client_factory=lambda config: _StubClient(),  # type: ignore[arg-type]
    )
    result = app.run_once("hi")
    assert result.final_answer == "hello from stub"


def test_agent_app_does_not_import_solver():
    """spec §2: --agent 启动的入口必须独立于 solver。"""
    import alphasolve.agent.ui.cli_app as cli_app_module
    source = Path(cli_app_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not module.startswith("alphasolve.solver"), (
                f"agent/ui/cli_app.py must not import alphasolve.solver.*; found: {module}"
            )
            assert not module.startswith("alphasolve.workflow"), (
                f"agent/ui/cli_app.py must not import alphasolve.workflow.*; found: {module}"
            )


def test_agent_app_uses_default_tools(tmp_path: Path):
    """AgentApp 默认使用 build_default_tool_registry 注册的工具集，不挂 Agent 工具。"""
    app = AgentApp(
        project_dir=tmp_path,
        client_factory=lambda config: _StubClient(),  # type: ignore[arg-type]
    )
    config = app._build_config()
    assert "Read" in config.tools
    assert "Write" in config.tools
    # Bash 或 Shell 二选一（按平台），至少有一个
    assert "Bash" in config.tools or "Shell" in config.tools
    assert "Agent" not in config.tools  # subagent 调度不属于第二层 CLI
    assert config.system_prompt == ""
