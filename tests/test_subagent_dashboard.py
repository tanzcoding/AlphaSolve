"""委派事件应显示在父面板里，并保持父角色状态独立。"""
from __future__ import annotations

import io

import pytest
from rich.console import Console

from alphasolve.solver.ui.dashboard import (
    make_curator_event_sink,
    make_orchestrator_event_sink,
    make_worker_event_sink,
)
from alphasolve.solver.ui.team_renderer import PropositionTeamRenderer


@pytest.fixture(params=["orchestrator", "worker", "curator"])
def panel(request):
    console = Console(file=io.StringIO(), width=140, height=40, color_system=None)
    renderer = PropositionTeamRenderer(console=console)
    if request.param == "worker":
        renderer.register_worker("test")
        sink = make_worker_event_sink(renderer, worker_id="test", role="generator")
        state = renderer._workers["test"]
    elif request.param == "curator":
        sink = make_curator_event_sink(renderer)
        state = renderer._curator
    else:
        sink = make_orchestrator_event_sink(renderer)
        state = renderer._orchestrator
    sink({"type": "run_start"})
    sink({"type": "tool_call", "name": "Agent", "arguments": {"description": "Check claim"}})
    return renderer, sink, state


def emit(sink, kind, *, session="child", agent_type="reasoning_subagent", **values):
    sink({
        "type": "subagent_event",
        "session_id": session,
        "agent_type": agent_type,
        "event": {"type": kind, **values},
    })


def active_text(renderer, state):
    return "\n".join(line.plain for line in renderer._render_active_lines(state, width=120))


def test_subagent_activity_is_visible_without_overwriting_parent(panel):
    renderer, sink, state = panel
    started = renderer._worker_started
    emit(sink, "run_start", description="Check claim")
    emit(sink, "model_request")
    emit(sink, "thinking_delta", delta="checking ")
    emit(sink, "thinking_delta", content="checking boundary", delta="boundary")

    assert "reasoning_subagent" in active_text(renderer, state)
    assert "checking boundary" in active_text(renderer, state)
    assert state.active_tool == "Agent"
    assert state.status == "tool"
    assert renderer._worker_started == started

    emit(sink, "tool_call", name="RunPython", arguments={"code": "1 + 1"})
    assert "Using RunPython" in active_text(renderer, state)
    assert "1 + 1" in active_text(renderer, state)
    emit(sink, "tool_result", name="RunPython", content="2", is_error=False)
    assert any("✓ RunPython" in item.text for item in state.timeline)
    emit(sink, "assistant_delta", delta="Checked ")
    emit(sink, "assistant_delta", delta="the boundary")
    assert "Checked the boundary" in active_text(renderer, state)

    emit(sink, "run_finish", final_answer="Checked the boundary")
    assert not state.subagents
    assert "Using Agent" in active_text(renderer, state)
    assert state.status == "tool"
    sink({"type": "tool_result", "name": "Agent", "is_error": False})
    assert state.active_tool is None


@pytest.mark.parametrize("terminal", ["run_error", "run_stopped"])
def test_nested_subagent_cleanup_restores_still_running_delegate(panel, terminal):
    renderer, sink, state = panel
    emit(sink, "run_start")
    emit(sink, "tool_call", name="Agent", arguments={})
    emit(sink, "run_start", session="nested", agent_type="compute_subagent")
    emit(sink, "tool_call", session="nested", agent_type="compute_subagent", name="RunPython")
    assert "compute_subagent" in active_text(renderer, state)
    assert len(state.subagents) == 2

    emit(sink, terminal, session="nested", agent_type="compute_subagent", error="failed")
    assert list(state.subagents) == ["child"]
    assert "reasoning_subagent" in active_text(renderer, state)
    assert state.status == "tool"
    emit(sink, terminal, error="failed")
    assert not state.subagents


def test_subagent_tool_error_remains_visible_while_agent_continues(panel):
    renderer, sink, state = panel
    emit(sink, "run_start")
    emit(sink, "tool_result", name="RunPython", is_error=True, content="invalid code")
    assert "✗ RunPython invalid code" in active_text(renderer, state)
    assert any("invalid code" in item.text and item.style == "red" for item in state.timeline)
    assert state.status == "tool"
    assert state.subagents
