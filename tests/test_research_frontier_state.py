from __future__ import annotations

import json

from alphasolve.solver.project import ProjectLayout
from alphasolve.solver.research_frontier_state import write_research_frontier_state


def test_state_file_renders_current_research_frontier(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n\nProve the target.\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    dag_state = {
        "nodes": {
            "frontier-leaf": {
                "difficulty_id": "frontier-leaf",
                "statement": "Prove the frontier bridge.",
                "parent_ids": [],
                "resolution_policy": "manual",
                "evidence_refs": ["verified_propositions/bridge.md"],
                "status": "advanced",
                "attack_outcomes": [
                    {
                        "recorded_at": "2026-08-06T00:00:00+00:00",
                        "outcome": "advanced",
                        "summary": "Verified the first bridge step.",
                    }
                ],
                "parent_direct_attacks": [],
            }
        },
        "aliases": {},
        "curated_checkpoints": {},
        "global_attack": {"last_attempt": None},
        "updated_at": "2026-08-06T00:00:00+00:00",
    }
    (layout.curation_records_dir / "difficulty_dag.json").write_text(
        json.dumps(dag_state), encoding="utf-8"
    )
    (layout.progress_audit_state_path).write_text(
        json.dumps(
            {
                "latest": {
                    "checkpoint_id": "checkpoint-0001",
                    "verdict": "STALLED",
                    "terminal_gap": "Close the bridge.",
                    "recommended_next_action": "Attack frontier-leaf.",
                    "audit_path": "progress_audits/checkpoint-0001/audit.md",
                }
            }
        ),
        encoding="utf-8",
    )
    (layout.verified_dir / "bridge.md").write_text("## Statement\n\nBridge fact.\n", encoding="utf-8")
    layout.curation_events_path.write_text(
        "\n".join([
            json.dumps({
                "kind": "frontier_deviation_started",
                "recorded_at": "2026-08-06T01:00:00Z",
                "worker_id": "worker-free",
                "reason": "Test an orthogonal dual construction.",
                "executable_difficulty_ids": ["frontier-leaf"],
            }),
            json.dumps({
                "kind": "frontier_deviation_completed",
                "recorded_at": "2026-08-06T01:01:00Z",
                "worker_id": "worker-free",
                "status": "rejected",
                "summary": "The dual construction repeats the known obstruction.",
            }),
            "",
        ]),
        encoding="utf-8",
    )

    path = write_research_frontier_state(
        layout.workspace_dir,
        runtime={
            "active_workers": [
                {
                    "worker_id": "worker-a",
                    "difficulty_id": "frontier-leaf",
                    "method_id": "direct_proof",
                    "progress": "running for 3s; current agent: generator",
                }
            ]
        },
    )

    text = path.read_text(encoding="utf-8")
    assert path == layout.research_frontier_state_path
    assert "# Research Frontier State" in text
    assert "`frontier-leaf`" in text
    assert "Prove the frontier bridge." in text
    assert "Close the bridge." in text
    assert "worker-a" in text
    assert "verified_propositions/bridge.md" in text
    assert "## Canonical Difficulty DAG" in text
    assert "policy=`manual`" in text
    assert "## Frontier Deviations" in text
    assert "Test an orthogonal dual construction." in text
    assert "The dual construction repeats the known obstruction." in text
    assert "excluding this file" in text
