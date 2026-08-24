from __future__ import annotations

import json
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


def test_orchestrator_does_not_force_reviewer_for_simple_startup(tmp_path):
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
            if self.role == "orchestrator":
                assert any(tool.name == "RequestResearchPlan" for tool in tools)
                assert not any(tool.name == "Agent" for tool in tools)
                return _response("This simple problem needs no research plan.")
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


def test_orchestrator_allows_repeated_research_plans_when_new_evidence_requires_review(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n\nProve the target.\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    suite = load_agent_suite(Path(solver_pkg.__file__).parent / "config")
    orchestrator = Orchestrator(layout=layout, suite=suite, client_factory=lambda _config: None)
    calls: list[str] = []

    class PlanningService:
        def call(self, agent_type, description, prompt):
            assert agent_type == "research_reviewer"
            assert "process_audit" in prompt
            calls.append(description)
            return (
                "### Research Strategy JSON\n```json\n"
                '{"research_plan":{"objective":"Test the bridge direction.","strategy":"Keep one bridge route live.","tracks":[{"track_id":"bounded-bridge","priority":"primary","kind":"NEW_DIRECTION","difficulty_id":"","method_id":"direct_proof","route_label":"bounded-bridge","research_goal":"Determine whether the bridge route can close the target.","rationale":"It is the only currently supported direction.","avoid":"Do not repeat refuted routes."}]},"graph_observations":[]}\n'
                "```"
            )

    class Gateway:
        def reviewer_frontier(self):
            return {"frontier_revision": "test", "dispatchable": [], "graph": {}, "sources": {}}

    class Manager:
        results = []

    orchestrator._planning_subagents = PlanningService()
    orchestrator._reviewer_plan_gateway = Gateway()
    orchestrator._progress_audit_queue = None

    first = orchestrator._request_research_plan_tool(Manager(), {})
    second = orchestrator._request_research_plan_tool(Manager(), {})

    assert not first.is_error
    assert not second.is_error
    assert len(calls) == 2


def test_reviewer_projection_keeps_realtime_task_audit_and_route_attribution():
    orchestrator = object.__new__(Orchestrator)

    class Manager:
        completed_evidence = [{
            "worker_id": "worker-a",
            "status": "verified",
            "difficulty_id": "leaf",
            "method_id": "direct_proof",
            "route_label": "exact-variance",
            "reviewer_step_kind": "TARGET_NODE",
            "summary": "Proved an auxiliary estimate.",
            "verified_file": "verified_propositions/auxiliary.md",
            "task_audit": {
                "delivery": "off_target",
                "scope_drift": "The sharp bound was weakened.",
                "residual_obligation": "Prove the sharp bound.",
                "rejection_locus": "",
                "salvageable_content": "The auxiliary estimate.",
                "retry_assessment": "Bridge the auxiliary estimate to the sharp case.",
            },
        }]
        results = []

    projection = orchestrator._planning_worker_projection(Manager())

    assert projection == [{
        "worker_id": "worker-a",
        "status": "verified",
        "assigned_difficulty_id": "leaf",
        "method_id": "direct_proof",
        "route_label": "exact-variance",
        "reviewer_step_kind": "TARGET_NODE",
        "failure_kind": "",
        "delivery": "off_target",
        "scope_drift": "The sharp bound was weakened.",
        "residual_obligation": "Prove the sharp bound.",
        "rejection_locus": "",
        "salvageable_content": "The auxiliary estimate.",
        "retry_assessment": "Bridge the auxiliary estimate to the sharp case.",
        "pinned_target": "",
        "verified_file": "verified_propositions/auxiliary.md",
        "summary": "Proved an auxiliary estimate.",
        "difficulty_handoff": None,
    }]


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
    # 冷启动批先于 orchestrator LLM 运行，因此验收标准由运行时提供，而非 LLM 编写。
    from alphasolve.solver.cold_start import COLD_START_RUBRIC

    assert all(
        kwargs == {"difficulty_id": None, "method_id": "direct_proof", "rubric": COLD_START_RUBRIC}
        for _hint, kwargs in manager.calls
    )
    assert "first evidence" in runtime.context_for_orchestrator()

    for index in range(3):
        (layout.verified_dir / f"verified-{index}.md").write_text("## Statement\n", encoding="utf-8")
    non_cold_manager = Manager()

    assert ColdStartRuntime(layout=layout, max_workers=2, threshold=3).prepare(non_cold_manager) == []
    assert non_cold_manager.calls == []


def test_reviewer_can_repeat_compute_and_reasoning_checks_but_limits_numerical_experiments():
    service = SubagentService(
        suite=SimpleNamespace(subagents={}),
        client_factory=lambda _config: None,
    )
    service._reviewer_delegate_budget.value = dict(service.REVIEWER_LIMITED_DELEGATE_LIMITS)

    for _ in range(3):
        service._reserve_reviewer_delegate("reasoning_subagent")
        service._reserve_reviewer_delegate("compute_subagent")
    service._reserve_reviewer_delegate("numerical_experiment_subagent")

    with pytest.raises(RuntimeError, match="budget exhausted"):
        service._reserve_reviewer_delegate("numerical_experiment_subagent")
    with pytest.raises(PermissionError, match="may delegate only"):
        service._reserve_reviewer_delegate("curator")


def test_spawn_worker_uses_handoffs_as_evidence_for_local_follow_up(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    orchestrator = object.__new__(Orchestrator)
    orchestrator.layout = layout
    orchestrator._local_followup_handoff_ids = set()

    payload = {
        "completed": [
            {
                "worker_id": "worker-a",
                "difficulty_handoff": {
                    "handoff_id": "handoff-worker-a",
                    "worker_id": "worker-a",
                    "obstacle": "Bridge A remains unproved by the current route.",
                },
            },
            {
                "worker_id": "worker-b",
                "difficulty_handoff": {
                    "worker_id": "worker-b",
                    "obstacle": "Bridge B remains unproved by the current route.",
                },
            },
        ]
    }

    class NoResultsManager:
        results = []

    available = orchestrator._available_local_handoffs(NoResultsManager(), payload)

    assert set(available) == {"handoff-worker-a"}
    assert available["handoff-worker-a"]["obstacle"] == "Bridge A remains unproved by the current route."

    class Manager:
        results = []
        solved_result = None

        def spawn(self, hint, **kwargs):
            self.hint = hint
            self.kwargs = kwargs
            return {"spawned": True, "worker_id": "follow-up-a"}

    manager = Manager()
    orchestrator._available_local_handoffs = lambda _manager: {
        "handoff-worker-a": available["handoff-worker-a"],
    }
    orchestrator._refresh_research_frontier_state = lambda _manager: None
    orchestrator.session_id = "test-session"
    orchestrator._reviewer_plan_gateway = SimpleNamespace(global_attack_preflight=lambda: {"ready": False})
    first = orchestrator._spawn_difficulty_leaf(
        manager,
        {
            "method_id": "contradiction",
            "hint": "Construct a witness or prove the missing bridge lemma.",
            "rubric": "- The Statement exhibits an explicit witness or proves the bridge lemma.",
            "followup_handoff_ids": ["handoff-worker-a"],
            "evidence_refs": ["unverified_propositions/prop-a/difficulty_handoff.json"],
        },
    )
    first_payload = json.loads(first.content)
    assert first_payload["spawned"] is True
    assert manager.kwargs["difficulty_id"] is None
    assert manager.kwargs["method_id"] == "contradiction"
    assert "Bridge A remains unproved" in manager.hint
    assert "Relevant prior evidence" in manager.kwargs["frontier_note"]

    second = orchestrator._spawn_difficulty_leaf(
        manager,
        {
            "hint": "Retry.",
            "rubric": "- The Statement closes the previously reported obstacle.",
            "followup_handoff_ids": ["handoff-worker-a"],
        },
    )
    assert "local_handoff_already_followed_up" in second.content


def test_spawn_worker_requires_acceptance_rubric(tmp_path):
    """下发 hint 必须同时给出验收标准，否则任务审计无从判断"是否完成"。"""
    (tmp_path / "problem.md").write_text("# Problem\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    orchestrator = object.__new__(Orchestrator)
    orchestrator.layout = layout
    orchestrator._local_followup_handoff_ids = set()

    class Manager:
        results = []
        solved_result = None
        calls = 0

        def spawn(self, hint, **kwargs):
            type(self).calls += 1
            return {"spawned": True, "worker_id": "should-not-spawn"}

    result = orchestrator._spawn_difficulty_leaf(Manager(), {"hint": "Try something."})
    payload = json.loads(result.content)

    assert result.is_error
    assert payload["spawned"] is False
    assert payload["reason"] == "rubric_required"
    assert Manager.calls == 0
