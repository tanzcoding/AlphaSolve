from __future__ import annotations

import pytest

from alphasolve.solver.policy import DifficultyDagPolicy, SolverPolicy


def test_policy_reads_remaining_dag_limits():
    policy = SolverPolicy.from_settings({
        "difficulty_dag": {
            "max_persistent_depth": 7,
            "global_attack_verified_proposition_interval": 9,
        }
    })
    assert policy.difficulty_dag.max_persistent_depth == 7
    assert policy.difficulty_dag.global_attack_verified_proposition_interval == 9


def test_removed_split_and_parent_budget_settings_are_rejected():
    with pytest.raises(ValueError, match="unknown difficulty_dag setting"):
        SolverPolicy.from_settings({"difficulty_dag": {"unchanged_outcomes_before_forced_split": 2}})
    with pytest.raises(ValueError, match="unknown difficulty_dag setting"):
        SolverPolicy.from_settings({"difficulty_dag": {"parent_direct_attack_outcome_interval": 2}})


def test_dag_policy_validates_remaining_limits():
    with pytest.raises(ValueError, match="max_persistent_depth"):
        DifficultyDagPolicy(max_persistent_depth=-1)
