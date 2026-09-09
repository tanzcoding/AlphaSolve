"""交互入口的会话连续性、工具装配和调试输出测试。"""
from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from rich.console import Console

from alphasolve.agent import AgentRunError
from alphasolve.agent.ui import cli_app
from alphasolve.agent.ui.cli_app import AgentApp, make_print_debug_event_sink, make_repl_event_sink


class _FakeAgent:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.calls = []
        self.closed = False

    def run(self, task, **kwargs):
        self.calls.append((task, kwargs))
        return SimpleNamespace(final_answer="done", messages=["visible trace"], trace=[])

    def close(self):
        self.closed = True


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_app, "Agent", _FakeAgent)
    application = AgentApp(project_dir=tmp_path, client_factory=lambda config: object())
    yield application
    application.close()


def test_agent_app_preserves_one_agent_across_prompts_and_closes_it(app):
    assert app.run_once("first", event_sink=None).final_answer == "done"
    agent = app.build_agent()
    app.run_once("second", event_sink=None)
    assert app.build_agent() is agent
    assert [task for task, kwargs in agent.calls] == ["first", "second"]
    assert all("extra_messages" not in kwargs for task, kwargs in agent.calls)
    app.close()
    assert agent.closed


def test_repl_does_not_replay_visible_trace_as_native_history(app, monkeypatch):
    prompts = iter(["first", "second", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(prompts))
    app.run()
    assert [task for task, kwargs in app.build_agent().calls] == ["first", "second"]
    assert all("extra_messages" not in kwargs for task, kwargs in app.build_agent().calls)


def test_show_tools_uses_effective_definitions_without_running_agent(app):
    definitions = app.tool_defs()
    config = app.build_agent().config
    assert [tool.name for tool in definitions] == list(config.tools)
    assert "Read" in config.tools
    assert "Agent" in config.tools
    assert "Bash" in config.tools or "Shell" in config.tools
    assert "general-purpose coding agent" in config.system_prompt
    assert app.build_agent().calls == []


def test_scoped_explorer_uses_its_tools_and_closes_child(app, monkeypatch):
    children = []

    def make_child(**kwargs):
        child = _FakeAgent(**kwargs)
        children.append(child)
        return child

    parent = app.build_agent()
    monkeypatch.setattr(cli_app, "Agent", make_child)
    result = parent.tool_registry.execute(
        "Agent",
        {"type": "scoped_explorer", "description": "inspect notes", "prompt": "Inspect notes."},
        enabled=list(parent.config.tools),
    )
    assert not result.is_error
    assert "done" in result.content
    assert len(children) == 1
    assert "Agent" not in children[0].config.tools
    assert "Read" in children[0].config.tools
    assert children[0].closed


@pytest.mark.parametrize("sink_factory", [make_repl_event_sink, make_print_debug_event_sink])
def test_event_sinks_keep_tool_results_literal_and_complete(sink_factory):
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=100)
    sink = sink_factory(console)
    sink({"type": "tool_call", "name": "Read", "arguments": {"path": "note.md"}})
    content = "[red]literal tool output[/red]\n" + "last line\n" * 150
    sink({"type": "tool_result", "name": "Read", "content": content, "is_error": False})
    rendered = output.getvalue()
    assert "note.md" in rendered
    assert "[red]literal tool output[/red]" in rendered
    assert rendered.count("last line") == 150


def test_print_debug_sink_labels_only_visible_reasoning():
    output = io.StringIO()
    sink = make_print_debug_event_sink(Console(file=output, color_system=None))
    sink({"type": "thinking", "content": "Checking the note.", "streamed": False})
    assert "Visible reasoning" in output.getvalue()
    assert "Checking the note." in output.getvalue()
    assert "COT" not in output.getvalue()


def test_fatal_failure_stops_repl_without_reading_another_prompt(app, monkeypatch):
    prompts = []

    def read_prompt(_):
        prompts.append("prompt")
        return "hello"

    def fail_run(*args, **kwargs):
        raise AgentRunError("subscription limit", trace=[], failure_kind="quota")

    monkeypatch.setattr("builtins.input", read_prompt)
    monkeypatch.setattr(app.build_agent(), "run", fail_run)
    with pytest.raises(AgentRunError, match="subscription limit"):
        app.run()
    assert prompts == ["prompt"]
    assert app.stop_event.is_set()


def test_nonfatal_failure_allows_an_explicit_next_prompt(app, monkeypatch):
    attempts = []
    prompts = iter(["first", "second", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(prompts))

    def run(task):
        attempts.append(task)
        if len(attempts) == 1:
            raise AgentRunError("temporary failure", trace=[])
        return SimpleNamespace(final_answer="done")

    monkeypatch.setattr(app.build_agent(), "run", run)
    app.run()
    assert attempts == ["first", "second"]
    assert not app.stop_event.is_set()


def test_cli_print_failure_closes_app_and_returns_nonzero(tmp_path, monkeypatch, capsys):
    from alphasolve import cli

    closed = []

    def run_once(*args, **kwargs):
        raise AgentRunError("subscription exhausted", trace=[], failure_kind="quota")

    application = SimpleNamespace(run_once=run_once, close=lambda: closed.append(True))
    monkeypatch.setattr(cli_app, "AgentApp", lambda **kwargs: application)
    monkeypatch.setattr(cli, "_install_console_handler", lambda: None, raising=False)
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)
    monkeypatch.setattr(cli, "_apply_env_sources", lambda **kwargs: None)
    monkeypatch.setenv("ALPHASOLVE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["alphasolve", "--agent", "-p", "hello"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert closed == [True]
    assert "subscription exhausted" in capsys.readouterr().err
