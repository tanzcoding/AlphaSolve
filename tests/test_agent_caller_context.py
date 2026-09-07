"""caller_context: 第二层只透传、不解释。"""
from __future__ import annotations

from typing import Any

from alphasolve.agent import Agent, AgentConfig, ToolRegistry
from alphasolve.llm.types import Message
from tests.response_fakes import CompletionResponse


class _StubClient:
    def complete(self, *, messages, tools, delta_sink=None):
        return CompletionResponse(
            message=Message(role="assistant", content="done"),
            finish_reason="stop",
        )


def test_caller_context_propagates_to_events():
    events: list[dict[str, Any]] = []
    agent = Agent(
        config=AgentConfig(name="t", system_prompt="hi"),
        client=_StubClient(),  # type: ignore[arg-type]
        tool_registry=ToolRegistry(),
        event_sink=events.append,
        caller_context={"parent_agent": "orch", "depth": 1},
    )
    agent.run("question")
    assert events, "expected at least one emitted event"
    assert all("caller_context" in e for e in events)
    assert all(e["caller_context"] == {"parent_agent": "orch", "depth": 1} for e in events)


def test_caller_context_none_is_no_op():
    events: list[dict[str, Any]] = []
    agent = Agent(
        config=AgentConfig(name="t", system_prompt="hi"),
        client=_StubClient(),  # type: ignore[arg-type]
        tool_registry=ToolRegistry(),
        event_sink=events.append,
        # caller_context 不传，默认 None
    )
    agent.run("question")
    assert events, "expected at least one emitted event"
    assert all("caller_context" not in e for e in events)


def test_caller_context_is_copied_on_init():
    """传入 dict 被外部 mutate 时 agent 拷贝的不应跟着变。"""
    src = {"parent_agent": "orch", "depth": 1}
    agent = Agent(
        config=AgentConfig(name="t", system_prompt="hi"),
        client=_StubClient(),  # type: ignore[arg-type]
        tool_registry=ToolRegistry(),
        caller_context=src,
    )
    src["depth"] = 999
    assert agent.caller_context == {"parent_agent": "orch", "depth": 1}
