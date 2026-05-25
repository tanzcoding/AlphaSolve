"""--agent CLI 入口的最小单测：构造 + run_once 不崩，且不引入 solver 依赖。"""
from __future__ import annotations

import ast
import io
from pathlib import Path

from rich.console import Console

from alphasolve.agent import AgentConfig
from alphasolve.agent.ui.cli_app import AgentApp, make_print_debug_event_sink
from alphasolve.llm.types import CompletionResponse, Message, ToolCall


class _StubClient:
    def complete(self, *, messages, tools, delta_sink=None):
        return CompletionResponse(
            message=Message(role="assistant", content="hello from stub"),
            finish_reason="stop",
        )


class _ToolUsingStubClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, messages, tools, delta_sink=None):
        self.calls += 1
        if self.calls == 1:
            return CompletionResponse(
                message=Message(
                    role="assistant",
                    reasoning_content="I should inspect note.md.",
                    tool_calls=(ToolCall(id="read-1", name="Read", args={"path": "note.md"}),),
                ),
                finish_reason="tool_calls",
            )
        return CompletionResponse(
            message=Message(role="assistant", content="debug complete"),
            finish_reason="stop",
        )


class _SubagentUsingStubClient:
    def __init__(self, config: AgentConfig, seen_tools: dict[str, list[list[str]]]) -> None:
        self.config = config
        self.seen_tools = seen_tools
        self.calls = 0

    def complete(self, *, messages, tools, delta_sink=None):
        self.calls += 1
        tool_names = [tool.name for tool in tools]
        self.seen_tools.setdefault(self.config.name, []).append(tool_names)
        if self.config.name == "agent:scoped_explorer":
            return CompletionResponse(
                message=Message(role="assistant", content="subagent evidence report"),
                finish_reason="stop",
            )
        if self.calls == 1:
            return CompletionResponse(
                message=Message(
                    role="assistant",
                    tool_calls=(
                        ToolCall(
                            id="agent-1",
                            name="Agent",
                            args={
                                "type": "scoped_explorer",
                                "description": "inspect notes",
                                "prompt": "Inspect note.md and report evidence.",
                            },
                        ),
                    ),
                ),
                finish_reason="tool_calls",
            )
        assert any(message.role == "tool" and "subagent evidence report" in message.content for message in messages)
        return CompletionResponse(
            message=Message(role="assistant", content="parent complete"),
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
    """AgentApp 默认使用 build_default_tool_registry 注册的工具集，并挂通用 Agent 工具。"""
    app = AgentApp(
        project_dir=tmp_path,
        client_factory=lambda config: _StubClient(),  # type: ignore[arg-type]
    )
    config = app._build_config()
    assert "Read" in config.tools
    assert "Write" in config.tools
    # Bash 或 Shell 二选一（按平台），至少有一个
    assert "Bash" in config.tools or "Shell" in config.tools
    assert "Agent" in config.tools
    assert "general-purpose coding agent" in config.system_prompt
    assert "Read" in config.system_prompt
    assert "Write" in config.system_prompt


def test_agent_app_agent_tool_launches_non_recursive_scoped_explorer(tmp_path: Path):
    (tmp_path / "note.md").write_text("subagent should inspect this\n", encoding="utf-8")
    seen_tools: dict[str, list[list[str]]] = {}
    app = AgentApp(
        project_dir=tmp_path,
        client_factory=lambda config: _SubagentUsingStubClient(config, seen_tools),  # type: ignore[arg-type]
    )

    result = app.run_once("split the review", event_sink=None)

    assert result.final_answer == "parent complete"
    assert "Agent" in seen_tools["agent"][0]
    assert "Agent" not in seen_tools["agent:scoped_explorer"][0]
    assert "Read" in seen_tools["agent:scoped_explorer"][0]


def test_print_debug_sink_shows_reasoning_and_tool_events(tmp_path: Path):
    (tmp_path / "note.md").write_text("important note\n", encoding="utf-8")
    client = _ToolUsingStubClient()
    app = AgentApp(
        project_dir=tmp_path,
        client_factory=lambda config: client,  # type: ignore[arg-type]
    )
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=100)

    result = app.run_once(
        "inspect",
        event_sink=make_print_debug_event_sink(console),
    )

    debug_text = output.getvalue()
    assert result.final_answer == "debug complete"
    assert "COT" in debug_text
    assert "I should inspect note.md." in debug_text
    assert "tool call: Read" in debug_text
    assert "path: note.md" in debug_text
    assert "tool result: Read" in debug_text
    assert "important note" in debug_text
