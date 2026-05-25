"""solver.role.Role 的单元测试：装配 + run + trace 形态。"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from alphasolve.agent import (
    Agent,
    AgentConfig,
    AgentRunResult,
    AgentSuite,
    Workspace,
)
from alphasolve.llm.types import Message
from alphasolve.solver.role import Role, RoleContext


class _StubClient:
    """A fake ChatClient that returns a canned plain-assistant response."""

    def __init__(self, text: str) -> None:
        self._text = text

    def complete(self, *, messages, tools, **_):
        from alphasolve.llm.types import CompletionResponse, Usage

        return CompletionResponse(
            message=Message(role="assistant", content=self._text, tool_calls=()),
            finish_reason="stop",
            usage=Usage(),
        )

    def stream(self, *, messages, tools, **_):
        # Streaming is optional; agent prefers complete() when available.
        raise NotImplementedError


def _build_suite(tmp_path: Path) -> AgentSuite:
    """Build a minimal suite covering generator/verifier_attempt/theorem_checker/reviser."""
    def cfg(name: str) -> AgentConfig:
        return AgentConfig(
            name=name,
            system_prompt=f"You are {name}.",
            tools=(),
            max_turns=1,
            tier="balanced",
        )

    return AgentSuite(
        path=tmp_path,
        agents={
            "generator": cfg("generator"),
            "verifier": cfg("verifier"),
            "verifier_citation": cfg("verifier_citation"),
            "theorem_checker": cfg("theorem_checker"),
            "reviser": cfg("reviser"),
        },
        subagents={},
        settings={},
    )


@pytest.fixture
def role_ctx(tmp_path) -> RoleContext:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    unverified = workspace_dir / "unverified_propositions"
    unverified.mkdir()
    worker_dir = unverified / "prop-abc12345"
    worker_dir.mkdir()
    workspace = Workspace(workspace_dir)

    captured_labels: list[str] = []

    def fake_event_sink_factory(label: str):
        captured_labels.append(label)
        return None  # no actual sink — Agent handles None gracefully

    ctx = RoleContext(
        workspace=workspace,
        worker_rel="unverified_propositions/prop-abc12345",
        worker_dir=worker_dir,
        suite=_build_suite(tmp_path),
        client_factory=lambda config: _StubClient(f"answer from {config.name}"),
        subagent_max_depth=0,  # no recursive subagents in unit test
        execution_gateway=None,
        curator_queue=None,
        log_session=None,
        stop_event=threading.Event(),
        event_sink_factory=fake_event_sink_factory,
        trace=[],
    )
    # Expose for assertions
    ctx._captured_labels = captured_labels  # type: ignore[attr-defined]
    return ctx


def test_role_for_generator_assembles_and_runs(role_ctx):
    role = Role.for_generator(role_ctx)
    assert role.name == "generator"

    result = role.run("Write a proposition")

    assert "answer from generator" in result.final_answer
    assert len(role_ctx.trace) == 1
    entry = role_ctx.trace[0]
    assert entry["role"] == "generator"
    assert entry["final_answer"] == result.final_answer
    assert "trace" in entry
    # event sink labelled with role
    assert "generator" in role_ctx._captured_labels


def test_role_for_verifier_attempt_records_workflow_and_attempt(role_ctx):
    config = role_ctx.suite.agents["verifier_citation"]
    role = Role.for_verifier_attempt(
        role_ctx,
        config=config,
        config_name="verifier_citation",
        workflow_index=2,
        attempt_index=3,
    )
    role.run("Verify the proposition")

    entry = role_ctx.trace[-1]
    assert entry["role"] == "verifier_attempt"
    assert entry["workflow"] == 2
    assert entry["attempt"] == 3
    assert entry["config"] == "verifier_citation"
    # event sink label includes workflow.attempt
    assert any("verifier_attempt w2.3" in label for label in role_ctx._captured_labels)


def test_role_for_theorem_checker_records_attempt(role_ctx):
    role = Role.for_theorem_checker(role_ctx, attempt_index=4)
    role.run("Check theorem")

    entry = role_ctx.trace[-1]
    assert entry["role"] == "theorem_checker"
    assert entry["attempt"] == 4


def test_role_for_reviser_records_workflow(role_ctx):
    role = Role.for_reviser(
        role_ctx,
        proposition_rel="unverified_propositions/prop-abc12345/proposition.md",
        workflow_index=5,
    )
    role.run("Revise proposition")

    entry = role_ctx.trace[-1]
    assert entry["role"] == "reviser"
    assert entry["workflow"] == 5


def test_role_run_returns_agent_run_result(role_ctx):
    role = Role.for_generator(role_ctx)
    result = role.run("hi")
    assert isinstance(result, AgentRunResult)
    assert result.final_answer  # canned response from _StubClient


def test_role_exposes_underlying_agent_for_tests(role_ctx):
    role = Role.for_generator(role_ctx)
    assert isinstance(role.agent, Agent)
    assert role.agent.config.name == "generator"
