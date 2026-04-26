import io
import os
import sys
import time

from rich.console import Console

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alphasolve.agents.team.dashboard import make_worker_event_sink  # noqa: E402
from alphasolve.utils.rich_renderer import LemmaTeamRenderer  # noqa: E402


def test_dashboard_renders_native_split_panes_with_cot_and_tool_statuses():
    stream = io.StringIO()
    console = Console(
        file=stream,
        width=124,
        height=34,
        record=True,
        force_terminal=False,
        color_system=None,
    )
    renderer = LemmaTeamRenderer(console=console)

    renderer.update_orchestrator_phase("planning", status="thinking")
    renderer.update_orchestrator_thinking(
        module="orchestrator",
        thinking_text="survey known lemmas\nchoose a promising worker hint\nspawn a focused search",
        elapsed=0,
    )
    renderer.update_orchestrator_tool_start(
        module="orchestrator",
        name="spawn_worker",
        arg_preview='{"hint":"try a compact proof"}',
    )
    renderer.update_orchestrator_tool_done(name="spawn_worker", is_error=False)

    renderer.register_worker(0)
    renderer.update_thinking(
        0,
        module="generator",
        thinking_text="try the invariant\ncheck boundary cases\nwrite the candidate lemma",
        elapsed=0,
    )
    renderer.update_tool_start(0, module="generator", name="write_file", arg_preview='{"path":"lemma.md"}')
    renderer.update_tool_done(0, name="write_file", is_error=False)

    renderer.register_worker(1)
    renderer.update_tool_start(1, module="verifier", name="run_python", arg_preview="{}")
    renderer.update_tool_done(1, name="run_python", is_error=True)

    console.print(renderer.render())
    text = console.export_text()

    assert "AlphaSolve" in text
    assert "native dashboard" in text
    assert "@orchestrator" in text
    assert "@worker-00" in text
    assert "@worker-01" in text
    assert "CoT" in text
    assert "spawn a focused search" in text
    assert "✓ spawn_worker" in text
    assert "✓ write_file" in text
    assert "✗ run_python" in text


def test_dashboard_live_refresh_is_event_driven_to_reduce_flicker():
    stream = io.StringIO()
    console = Console(
        file=stream,
        width=100,
        height=28,
        record=True,
        force_terminal=False,
        color_system=None,
    )
    renderer = LemmaTeamRenderer(console=console, screen=False)

    renderer.start()
    try:
        assert renderer.refresh_per_second == 2.0
        assert renderer._min_refresh_interval == 0.5
        assert renderer._live is not None
        assert getattr(renderer._live, "auto_refresh", None) is False
    finally:
        renderer.stop()


def test_dashboard_line_diff_painter_skips_unchanged_lines():
    stream = io.StringIO()
    console = Console(
        file=stream,
        width=100,
        height=28,
        record=True,
        force_terminal=True,
        color_system=None,
    )
    renderer = LemmaTeamRenderer(console=console, screen=False, refresh_per_second=1000)

    renderer.start()
    try:
        assert renderer._live is not None

        stream.seek(0)
        stream.truncate(0)
        renderer._live.update(renderer.render(), refresh=True)
        unchanged_delta = stream.getvalue()
        assert "AlphaSolve" not in unchanged_delta
        assert "\x1b[2K" not in unchanged_delta

        stream.seek(0)
        stream.truncate(0)
        renderer._last_refresh_at = 0
        renderer.update_orchestrator_phase("planning", status="thinking")
        changed_delta = stream.getvalue()
        assert "planning" in changed_delta
        assert 0 < changed_delta.count("\x1b[2K") < 8
    finally:
        renderer.stop()


def test_dashboard_repaints_when_terminal_size_changes():
    stream = io.StringIO()
    console = Console(
        file=stream,
        width=100,
        height=28,
        record=True,
        force_terminal=True,
        color_system=None,
    )
    renderer = LemmaTeamRenderer(console=console, screen=False)

    renderer.start()
    try:
        stream.seek(0)
        stream.truncate(0)

        console._width = 140
        console._height = 34
        with renderer._lock:
            renderer._refresh_for_resize_or_pending_locked()

        resized_delta = stream.getvalue()
        assert "AlphaSolve" in resized_delta
        assert resized_delta.count("\x1b[2K") >= 20
    finally:
        renderer.stop()


def test_dashboard_thinking_tokens_bypass_refresh_throttle():
    stream = io.StringIO()
    console = Console(
        file=stream,
        width=100,
        height=28,
        record=True,
        force_terminal=True,
        color_system=None,
    )
    renderer = LemmaTeamRenderer(console=console, screen=False)

    renderer.start()
    try:
        stream.seek(0)
        stream.truncate(0)

        renderer._last_refresh_at = time.time()
        renderer.update_thinking(0, module="worker", thinking_text="first token", elapsed=0)

        token_delta = stream.getvalue()
        assert "first token" in token_delta
        assert "\x1b[2K" in token_delta
    finally:
        renderer.stop()


def test_dashboard_sink_does_not_duplicate_streamed_final_events():
    stream = io.StringIO()
    console = Console(
        file=stream,
        width=100,
        height=28,
        record=True,
        force_terminal=False,
        color_system=None,
    )
    renderer = LemmaTeamRenderer(console=console, screen=False)
    sink = make_worker_event_sink(renderer, worker_id=0, role="generator")
    assert sink is not None

    sink({"type": "thinking_delta", "content": "first", "delta": "first"})
    sink({"type": "thinking_delta", "content": "first second", "delta": " second"})
    sink({"type": "thinking", "content": "first second", "streamed": True})
    sink({"type": "assistant_delta", "content": "ok", "delta": "ok"})
    sink({"type": "assistant_message", "content": "ok", "streamed_content": True})

    state = renderer._workers[0]
    assert state.thinking_text == "first second"
    assert state.output_text == "ok"


def test_dashboard_retry_event_removes_partial_streamed_output():
    stream = io.StringIO()
    console = Console(
        file=stream,
        width=100,
        height=28,
        record=True,
        force_terminal=False,
        color_system=None,
    )
    renderer = LemmaTeamRenderer(console=console, screen=False)
    sink = make_worker_event_sink(renderer, worker_id=0, role="generator")
    assert sink is not None

    sink({"type": "thinking_delta", "content": "stale reasoning", "delta": "stale reasoning"})
    sink({"type": "assistant_delta", "content": "stale answer", "delta": "stale answer"})
    sink(
        {
            "type": "model_retry",
            "attempt": 1,
            "error_type": "RemoteProtocolError",
            "error": "peer closed connection",
            "reasoning_chars": len("stale reasoning"),
            "content_chars": len("stale answer"),
        }
    )
    sink({"type": "thinking_delta", "content": "fresh reasoning", "delta": "fresh reasoning"})
    sink({"type": "assistant_delta", "content": "fresh answer", "delta": "fresh answer"})

    state = renderer._workers[0]
    assert state.thinking_text == "fresh reasoning"
    assert state.output_text == "fresh answer"
    assert any("retrying model stream" in item for item in state.log_lines)


def test_dashboard_wraps_long_error_logs_in_agent_panel():
    stream = io.StringIO()
    console = Console(
        file=stream,
        width=100,
        height=28,
        record=True,
        force_terminal=False,
        color_system=None,
    )
    renderer = LemmaTeamRenderer(console=console, screen=False)
    message = (
        "RemoteProtocolError: peer closed connection without sending complete "
        "message body while streaming the response"
    )

    renderer.update_phase(0, "generator", status="failed")
    renderer.log(0, message, module="generator", level="ERROR")
    console.print(renderer.render())
    text = console.export_text()

    assert "RemoteProtocolError" in text
    assert "without sending complete message" in text
    assert "body while streaming the response" in text
    assert "response" in text
