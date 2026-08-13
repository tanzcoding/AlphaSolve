from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import alphasolve.solver as solver_pkg
from alphasolve.solver.cold_start import ColdStartRuntime
from alphasolve.solver.subagent_service import SubagentService
from alphasolve.agent import load_agent_suite
from alphasolve.llm.types import CompletionResponse, Message
from alphasolve.solver.orchestrator import Orchestrator
from alphasolve.solver.project import ProjectLayout


def _response(content: str) -> CompletionResponse:
    return CompletionResponse(message=Message(role="assistant", content=content), finish_reason="stop")


def test_orchestrator_starts_without_preemptive_reviewer_and_exposes_reviewer_tool(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n\nProve the target.\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    for index in range(3):
        (layout.verified_dir / f"existing-evidence-{index}.md").write_text(
            "## Statement\n\nExisting evidence.\n\n## Proof\n\nTrivial.\n",
            encoding="utf-8",
        )
    suite = load_agent_suite(Path(solver_pkg.__file__).parent / "config")
    calls: dict[str, list[list[Message]]] = {"research_reviewer": [], "orchestrator": []}

    class Client:
        def __init__(self, role: str) -> None:
            self.role = role

        def complete(self, *, messages, tools):
            calls.setdefault(self.role, []).append(list(messages))
            combined = "\n".join(str(message.content or "") for message in messages)
            if self.role == "orchestrator":
                agent_tool = next(tool for tool in tools if tool.name == "Agent")
                assert "research_reviewer" in agent_tool.parameters["properties"]["type"]["enum"]
                assert "# Initial Independent Research Reviewer Assessment" not in combined
                return _response("I started directly; no reviewer is needed for this test.")
            raise AssertionError(f"unexpected role: {self.role}")

    orchestrator = Orchestrator(
        layout=layout,
        suite=suite,
        client_factory=lambda config: Client(config.name),
        max_workers=1,
        max_verify_rounds=1,
        verifier_scaling_factor=1,
        subagent_max_depth=0,
    )

    result = orchestrator.run()

    assert result.worker_results == []
    assert calls["research_reviewer"] == []
    assert len(calls["orchestrator"]) == 1


def test_reviewer_is_limited_to_one_orchestrator_phase_call():
    orchestrator = object.__new__(Orchestrator)
    orchestrator._reviewer_call_count = 0

    orchestrator._guard_subagent_call("research_reviewer", 0)

    with pytest.raises(RuntimeError, match="at most once"):
        orchestrator._guard_subagent_call("research_reviewer", 0)


def test_cold_start_runtime_uses_verified_proposition_threshold_before_orchestration(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n\nProve the target.\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()

    class Manager:
        def __init__(self) -> None:
            self.calls: list[tuple[object, dict[str, object]]] = []
            self.active: dict[object, object] = {}

        def has_available_worker_slot(self) -> bool:
            return len(self.calls) < 2

        def spawn(self, hint, **kwargs):
            self.calls.append((hint, kwargs))
            return {"spawned": True, "worker_id": f"worker-{len(self.calls)}"}

        def wait(self):
            return {
                "completed": [
                    {"worker_id": "worker-1", "status": "partial", "summary": "first evidence"},
                    {"worker_id": "worker-2", "status": "partial", "summary": "second evidence"},
                ]
            }

    manager = Manager()
    runtime = ColdStartRuntime(layout=layout, max_workers=2, threshold=3)

    evidence = runtime.prepare(manager)

    assert [item["worker_id"] for item in evidence] == ["worker-1", "worker-2"]
    assert all(hint is None for hint, _kwargs in manager.calls)
    assert all(kwargs == {"difficulty_id": None, "method_id": "direct_proof"} for _hint, kwargs in manager.calls)
    assert "first evidence" in runtime.context_for_orchestrator()

    for index in range(3):
        (layout.verified_dir / f"verified-{index}.md").write_text("## Statement\n", encoding="utf-8")
    non_cold_manager = Manager()

    assert ColdStartRuntime(layout=layout, max_workers=2, threshold=3).prepare(non_cold_manager) == []
    assert non_cold_manager.calls == []


def test_reviewer_delegate_budget_allows_one_of_each_type():
    service = SubagentService(
        suite=SimpleNamespace(subagents={}),
        client_factory=lambda _config: None,
    )
    service._reviewer_delegate_budget.value = dict(service.REVIEWER_DELEGATE_LIMITS)

    service._reserve_reviewer_delegate("reasoning_subagent")
    service._reserve_reviewer_delegate("numerical_experiment_subagent")

    with pytest.raises(RuntimeError, match="budget exhausted"):
        service._reserve_reviewer_delegate("reasoning_subagent")
    with pytest.raises(PermissionError, match="may delegate only"):
        service._reserve_reviewer_delegate("compute_subagent")


def test_task_output_handoffs_do_not_trigger_another_reviewer(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    orchestrator = object.__new__(Orchestrator)
    orchestrator.layout = layout

    payload = {
        "completed": [
            {
                "worker_id": "worker-a",
                "difficulty_handoff": {
                    "worker_id": "worker-a",
                    "event_kind": "blocked",
                    "blocking_obligation": "Prove bridge A.",
                },
            },
            {
                "worker_id": "worker-b",
                "difficulty_handoff": {
                    "worker_id": "worker-b",
                    "event_kind": "blocked",
                    "blocking_obligation": "Prove bridge B.",
                },
            },
        ]
    }

    portfolio = orchestrator._difficulty_portfolio_payload(payload)

    assert portfolio is not None
    assert portfolio["review_status"] == "orchestrator_decision_required"
    assert "research_reviewer_report" not in portfolio
    assert portfolio["candidate_worker_ids"] == ["worker-a", "worker-b"]
