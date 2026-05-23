import json
import os
import sys
from io import StringIO

from rich.console import Console

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alphasolve.solver.debug_agent import DEBUG_AGENT_TOOLS, GeneralAgentDebugApp, GeneralAgentDebugRenderer  # noqa: E402
from alphasolve.llm.types import CompletionResponse, Message, StreamDelta, ToolCall  # noqa: E402


def _resp(content: str = "", tool_calls: tuple[ToolCall, ...] = (), finish_reason: str = None) -> CompletionResponse:
    return CompletionResponse(
        message=Message(role="assistant", content=content, tool_calls=tool_calls),
        finish_reason=finish_reason or ("tool_calls" if tool_calls else "stop"),
    )


class FakeDebugClient:
    def __init__(self) -> None:
        self.calls = 0
        self.seen_messages = []

    def complete(self, *, messages, tools, delta_sink=None):
        self.calls += 1
        self.seen_messages.append(messages)
        if self.calls == 1:
            return _resp(
                tool_calls=(
                    ToolCall(
                        id="call_write",
                        name="Write",
                        args={
                            "path": "debug-agent-test.yaml",
                            "content": "ok: true\n",
                        },
                    ),
                ),
            )
        return _resp("done")


class FakePrintClient:
    def __init__(self) -> None:
        self.seen_messages = []

    def complete(self, *, messages, tools):
        self.seen_messages.append(messages)
        return _resp("printed answer")


class FakeStreamingPrintClient:
    def __init__(self) -> None:
        self.delta_sink_seen = False

    def complete(self, *, messages, tools, delta_sink=None):
        self.delta_sink_seen = delta_sink is not None
        if delta_sink is not None:
            delta_sink(StreamDelta(type="text", text="streamed answer"))
        return _resp("streamed answer")


def test_agent_debug_app_uses_blank_prompt_and_requested_tools(tmp_path):
    client = FakeDebugClient()
    app = GeneralAgentDebugApp(
        project_dir=tmp_path,
        client_factory=lambda _config: client,
        renderer_factory=None,
    )

    config = app.build_config()
    assert config.system_prompt == ""
    assert config.tools == tuple(DEBUG_AGENT_TOOLS)

    registry = app.build_registry()
    exposed = [t.name for t in registry.tool_defs(config.tools)]
    assert exposed == DEBUG_AGENT_TOOLS

    result = app.run_once("write a small yaml file")
    assert result.final_answer == "done"
    assert (tmp_path / "debug-agent-test.yaml").read_text(encoding="utf-8") == "ok: true\n"

    app.run_once("what did you just do?")
    second_run_messages = client.seen_messages[-1]
    user_messages = [message.content for message in second_run_messages if message.role == "user"]
    assert user_messages == ["write a small yaml file", "what did you just do?"]


def test_agent_debug_renderer_uses_transcript_blocks_instead_of_tool_table(tmp_path):
    stream = StringIO()
    console = Console(file=stream, force_terminal=False, color_system=None, width=100)
    renderer = GeneralAgentDebugRenderer(console=console, workspace=tmp_path, tools=list(DEBUG_AGENT_TOOLS), screen=False)

    renderer.start("write a small yaml file")
    renderer.handle_event({"type": "thinking", "content": "I should write the file first."})
    renderer.handle_event({"type": "tool_call", "name": "Write", "arguments": {"path": "debug-agent-test.yaml", "content": "ok: true\n"}})
    long_result = "result-line\n" + ("x" * 1500)
    renderer.handle_event({"type": "tool_result", "name": "Write", "content": long_result, "is_error": False})
    renderer.handle_event({"type": "assistant_message", "content": "done", "tool_call_count": 0})
    renderer.handle_event({"type": "run_finish", "final_answer": "done"})
    renderer.start("second prompt")
    renderer.stop()

    rendered = stream.getvalue()
    assert "Tool Calls" not in rendered
    assert "write a small yaml file" in rendered
    assert "second prompt" in rendered
    assert rendered.index("I should write the file first.") < rendered.index("write debug-agent-test.yaml")
    assert rendered.index("write debug-agent-test.yaml running") < rendered.index("write debug-agent-test.yaml done")
    assert rendered.count("x") >= 1500


def test_agent_debug_cli_print_mode_runs_once_and_prints_final_answer(monkeypatch, tmp_path, capsys):
    from alphasolve import cli as cli_module

    client = FakePrintClient()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["alphasolve", "--agent-debug", "--demo", "-p", "script prompt"])
    if hasattr(cli_module, "_install_console_handler"):
        monkeypatch.setattr(cli_module, "_install_console_handler", lambda: None)
    monkeypatch.setattr(cli_module, "make_demo_client_factory", lambda: (lambda _config: client))

    cli_module.main()

    captured = capsys.readouterr()
    assert captured.out == "printed answer\n"
    assert captured.err == ""
    user_messages = [message.content for message in client.seen_messages[0] if message.role == "user"]
    assert user_messages == ["script prompt"]


def test_agent_debug_print_mode_still_uses_streaming_when_supported(tmp_path):
    client = FakeStreamingPrintClient()
    app = GeneralAgentDebugApp(
        project_dir=tmp_path,
        client_factory=lambda _config: client,
        renderer_factory=None,
    )

    result = app.run_once("survey quietly")

    assert result.final_answer == "streamed answer"
    assert client.delta_sink_seen is True
