from __future__ import annotations

import json

from alphasolve.solver.difficulty_dag import DifficultyDagStore
from alphasolve.solver.orchestrator import Orchestrator
from alphasolve.solver.project import ProjectLayout
from alphasolve.solver.research_planning import ReviewerPlanGateway


class _Manager:
    solved_result = None
    solution_path = None

    def __init__(self):
        self.results = []
        self.calls = []

    def spawn(self, hint, **kwargs):
        self.calls.append((hint, kwargs))
        return {"spawned": True, "worker_id": "worker-1", "difficulty_id": kwargs.get("difficulty_id")}

    def spawn_free_exploration_if_available(self, *, exploration_constraints):
        self.calls.append(("free", {"exploration_constraints": exploration_constraints}))
        return {"spawned": True, "worker_id": "worker-free", "difficulty_id": None}


def _layout(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    checkpoint = layout.progress_audits_dir / "checkpoint-0001"
    checkpoint.mkdir(parents=True)
    (checkpoint / "decision.json").write_text(
        json.dumps({"checkpoint_id": "checkpoint-0001", "status": "completed", "watermark": 1}),
        encoding="utf-8",
    )
    return layout


def _orchestrator(layout):
    orchestrator = object.__new__(Orchestrator)
    orchestrator.layout = layout
    orchestrator.curator_queue = None
    orchestrator.difficulty_dag = DifficultyDagStore(layout.workspace_dir)
    orchestrator._reviewer_plan_gateway = ReviewerPlanGateway(layout.workspace_dir)
    orchestrator._research_plans = {}
    orchestrator._planning_subagents = None
    orchestrator._progress_audit_queue = None
    orchestrator._has_dispatched_worker = False
    orchestrator._local_followup_handoff_ids = set()
    return orchestrator


def _curate_leaf(dag):
    dag.record_curation(
        checkpoint_id="checkpoint-0001",
        difficulties=[{
            "difficulty_id": "packing-leaf",
            "statement": "Control the global packing term.",
            "source_difficulty_ids": ["worker-source-packing"],
        }],
    )


_RUBRIC = "- The Statement bounds the global packing term as required."


def test_orchestrator_dispatches_only_curated_leaf(tmp_path):
    layout = _layout(tmp_path)
    orchestrator = _orchestrator(layout)
    manager = _Manager()
    unknown = json.loads(
        orchestrator._spawn_tool(manager, {"difficulty_id": "unknown", "rubric": _RUBRIC}).content
    )
    assert unknown["reason"] == "unknown_difficulty"

    _curate_leaf(orchestrator.difficulty_dag)
    payload = json.loads(
        orchestrator._spawn_tool(manager, {"difficulty_id": "packing-leaf", "rubric": _RUBRIC}).content
    )
    assert payload["spawned"] is True
    assert manager.calls[0][1]["difficulty_id"] == "packing-leaf"
    # preflight 校验通过后，节点的 canonical statement 必须随派发一起下发给 worker，
    # 否则 worker 只拿到一个无数学含义的溯源标签。
    assert manager.calls[0][1]["difficulty_statement"] == "Control the global packing term."
    # 验收标准随派发落到 worker 结果里，供短程任务审计核对。
    assert manager.calls[0][1]["rubric"] == _RUBRIC


def test_orchestrator_refuses_dispatch_against_terminal_difficulty(tmp_path):
    layout = _layout(tmp_path)
    orchestrator = _orchestrator(layout)
    manager = _Manager()
    _curate_leaf(orchestrator.difficulty_dag)

    ready = layout.workspace_dir / "curation_records" / "evidence_checkpoints" / "evidence-0001"
    ready.mkdir(parents=True)
    (ready / "curation_ready.json").write_text(
        json.dumps({"checkpoint_id": "evidence-0001", "status": "ready"}),
        encoding="utf-8",
    )
    orchestrator.difficulty_dag.record_curation(
        checkpoint_id="evidence-0001",
        difficulties=[],
        status_updates=[{
            "difficulty_id": "packing-leaf",
            "status": "refuted",
            "evidence_refs": ["verified_propositions/counterexample.md"],
        }],
    )

    payload = json.loads(
        orchestrator._spawn_tool(manager, {"difficulty_id": "packing-leaf", "rubric": _RUBRIC}).content
    )

    assert payload["spawned"] is False
    assert payload["reason"] == "difficulty_not_actionable"
    assert manager.calls == []


def test_orchestrator_can_start_independent_direction(tmp_path):
    layout = _layout(tmp_path)
    orchestrator = _orchestrator(layout)
    manager = _Manager()
    _curate_leaf(orchestrator.difficulty_dag)

    payload = json.loads(orchestrator._spawn_free_exploration_tool(manager, {
        "reason": "Test an independent dual certificate.",
    }).content)
    assert payload["spawned"] is True
    assert "independent" in manager.calls[-1][1]["exploration_constraints"]


def test_orchestrator_exposes_no_dag_outcome_writer():
    assert not hasattr(Orchestrator, "_record_difficulty_outcome_tool")
