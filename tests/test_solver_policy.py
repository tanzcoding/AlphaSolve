from __future__ import annotations

import json

import pytest

from alphasolve.solver.difficulty_dag import DifficultyDagStore
from alphasolve.solver.policy import SolverPolicy
from alphasolve.solver.project import ProjectLayout


def _layout(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    return layout


def test_solver_policy_parses_nested_dag_settings_and_applies_explicit_overrides():
    policy = SolverPolicy.from_settings(
        {
            "max_workers": 3,
            "verifier_agents": ["verifier_a", "verifier_b"],
            "difficulty_dag": {
                "max_persistent_depth": 2,
                "parent_direct_attack_outcome_interval": 7,
                "global_attack_verified_proposition_interval": 9,
            },
        }
    ).with_overrides(max_workers=4, max_verify_rounds=2)

    assert policy.max_workers == 4
    assert policy.max_verify_rounds == 2
    assert policy.verifier_agents == ("verifier_a", "verifier_b")
    assert policy.difficulty_dag.max_persistent_depth == 2
    assert policy.difficulty_dag.parent_direct_attack_outcome_interval == 7
    assert policy.difficulty_dag.global_attack_verified_proposition_interval == 9


def test_solver_policy_rejects_unknown_and_invalid_settings():
    with pytest.raises(ValueError, match="unknown solver setting"):
        SolverPolicy.from_settings({"max_wokers": 2})

    with pytest.raises(ValueError, match="free_seed_probability"):
        SolverPolicy.from_settings({"free_seed_probability": 1.1})

    with pytest.raises(ValueError, match="difficulty_dag.max_persistent_depth"):
        SolverPolicy.from_settings({"difficulty_dag": {"max_persistent_depth": -1}})


def test_difficulty_dag_store_uses_injected_policy(tmp_path):
    layout = _layout(tmp_path)
    policy = SolverPolicy.from_settings(
        {
            "difficulty_dag": {
                "max_persistent_depth": 1,
                "parent_direct_attack_outcome_interval": 2,
                "global_attack_verified_proposition_interval": 2,
            }
        }
    )
    dag = DifficultyDagStore(layout.workspace_dir, policy=policy.difficulty_dag)

    assert dag.selection_snapshot()["max_persistent_depth"] == 1
    assert dag.global_attack_preflight()["verified_proposition_interval"] == 2

    checkpoint = layout.progress_audits_dir / "checkpoint-0001"
    checkpoint.mkdir(parents=True)
    (checkpoint / "decision.json").write_text(
        json.dumps({"checkpoint_id": "checkpoint-0001", "status": "completed"}),
        encoding="utf-8",
    )
    difficulties = [
        {
            "difficulty_id": "root",
            "statement": "Resolve root.",
            "source_difficulty_ids": ["source-root"],
        },
        {
            "difficulty_id": "child",
            "statement": "Resolve child.",
            "parent_difficulty_ids": ["root"],
            "source_difficulty_ids": ["source-child"],
        },
        {
            "difficulty_id": "grandchild",
            "statement": "Resolve grandchild.",
            "parent_difficulty_ids": ["child"],
            "source_difficulty_ids": ["source-grandchild"],
        },
    ]
    with pytest.raises(ValueError, match="persistent difficulty depth exceeds 1"):
        dag.record_curation(checkpoint_id="checkpoint-0001", difficulties=difficulties)
