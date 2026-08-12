from __future__ import annotations

import json

from alphasolve.solver.difficulty_dag import DifficultyDagStore
from alphasolve.solver.orchestrator import Orchestrator
from alphasolve.solver.project import ProjectLayout


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
    orchestrator._research_plans = {}
    orchestrator._planning_subagents = None
    orchestrator._progress_audit_queue = None
    orchestrator._has_dispatched_worker = False
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


def test_orchestrator_dispatches_only_curated_leaf(tmp_path):
    layout = _layout(tmp_path)
    orchestrator = _orchestrator(layout)
    manager = _Manager()
    assert json.loads(orchestrator._spawn_tool(manager, {"difficulty_id": "unknown"}).content)["reason"] == "unknown_difficulty"

    _curate_leaf(orchestrator.difficulty_dag)
    payload = json.loads(orchestrator._spawn_tool(manager, {"difficulty_id": "packing-leaf"}).content)
    assert payload["spawned"] is True
    assert manager.calls[0][1]["difficulty_id"] == "packing-leaf"


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
