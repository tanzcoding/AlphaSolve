"""Validated runtime policy for the AlphaSolve solver."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Mapping


_DEFAULT_VERIFIER_AGENTS = (
    "verifier_format_references",
    "verifier_citation",
    "verifier_failure_modes",
    "verifier_stepwise",
    "verifier_premise_chain",
)


@dataclass(frozen=True)
class DifficultyDagPolicy:
    """Scheduling limits for the persistent mathematical difficulty DAG."""

    max_persistent_depth: int = 5
    global_attack_verified_proposition_interval: int = 5

    def __post_init__(self) -> None:
        _require_int("difficulty_dag.max_persistent_depth", self.max_persistent_depth, minimum=0)
        _require_int(
            "difficulty_dag.global_attack_verified_proposition_interval",
            self.global_attack_verified_proposition_interval,
            minimum=1,
        )


@dataclass(frozen=True)
class SolverPolicy:
    """Single immutable source of runtime scheduling and verification policy.

    Values originate from the suite's ``settings`` mapping. CLI and Python API
    callers may apply explicit overrides through :meth:`with_overrides`.
    """

    max_workers: int = 2
    tool_executor_size: int = 4
    max_verify_rounds: int = 6
    verifier_scaling_factor: int = 5
    verifier_agents: tuple[str, ...] = _DEFAULT_VERIFIER_AGENTS
    subagent_max_depth: int = 1
    max_orchestrator_restarts: int = 50
    progress_audit_every_n_outcomes: int = 5
    # Consecutive outcome-audit checkpoints citing the same repeated_avoided_obligation
    # before the runtime force-pivots the next proposal's primary track scope away from
    # LOCAL_REPAIR/NODE_ROUTE (and away from GLOBAL_SYNTHESIS as an escape hatch).
    stagnation_force_pivot_streak: int = 4
    cold_start_verified_proposition_threshold: int = 3
    worker_wait_timeout_seconds: float = 3600.0
    verified_propositions_organization_threshold: int = 20
    free_seed_probability: float = 0.6
    theorem_check_attempts: int = 5
    curator_health_check_interval: int = 8
    curator_oversized_entry_line_limit: int = 250
    curator_digest_batch_window_seconds: float = 0.2
    curator_digest_max_batch: int = 8
    difficulty_dag: DifficultyDagPolicy = field(default_factory=DifficultyDagPolicy)

    def __post_init__(self) -> None:
        _require_int("max_workers", self.max_workers, minimum=1)
        _require_int("tool_executor_size", self.tool_executor_size, minimum=1)
        _require_int("max_verify_rounds", self.max_verify_rounds, minimum=1)
        _require_int("verifier_scaling_factor", self.verifier_scaling_factor, minimum=1)
        _require_name_list("verifier_agents", self.verifier_agents)
        _require_int("subagent_max_depth", self.subagent_max_depth, minimum=0)
        _require_int("max_orchestrator_restarts", self.max_orchestrator_restarts, minimum=1)
        _require_int("progress_audit_every_n_outcomes", self.progress_audit_every_n_outcomes, minimum=1)
        _require_int("stagnation_force_pivot_streak", self.stagnation_force_pivot_streak, minimum=1)
        _require_int("cold_start_verified_proposition_threshold", self.cold_start_verified_proposition_threshold, minimum=0)
        _require_float("worker_wait_timeout_seconds", self.worker_wait_timeout_seconds, minimum=1200.0)
        _require_int(
            "verified_propositions_organization_threshold",
            self.verified_propositions_organization_threshold,
            minimum=1,
        )
        _require_probability("free_seed_probability", self.free_seed_probability)
        _require_int("theorem_check_attempts", self.theorem_check_attempts, minimum=1)
        _require_int("curator_health_check_interval", self.curator_health_check_interval, minimum=1)
        _require_int("curator_oversized_entry_line_limit", self.curator_oversized_entry_line_limit, minimum=1)
        _require_float("curator_digest_batch_window_seconds", self.curator_digest_batch_window_seconds, minimum=0.0)
        _require_int("curator_digest_max_batch", self.curator_digest_max_batch, minimum=1)
        if not isinstance(self.difficulty_dag, DifficultyDagPolicy):
            raise ValueError("difficulty_dag must be a mapping of DAG policy values")

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any] | None = None) -> "SolverPolicy":
        """Parse the suite ``settings`` mapping and reject misspelled policy keys."""
        raw = dict(settings or {})
        allowed = {
            "max_workers",
            "tool_executor_size",
            "max_verify_rounds",
            "verifier_scaling_factor",
            "verifier_agents",
            "subagent_max_depth",
            "max_orchestrator_restarts",
            "progress_audit_every_n_outcomes",
            "stagnation_force_pivot_streak",
            "cold_start_verified_proposition_threshold",
            "worker_wait_timeout_seconds",
            "verified_propositions_organization_threshold",
            "free_seed_probability",
            "theorem_check_attempts",
            "curator_health_check_interval",
            "curator_oversized_entry_line_limit",
            "curator_digest_batch_window_seconds",
            "curator_digest_max_batch",
            "difficulty_dag",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(f"unknown solver setting(s): {', '.join(unknown)}")

        dag_raw = raw.pop("difficulty_dag", {})
        if dag_raw is None:
            dag_raw = {}
        if not isinstance(dag_raw, Mapping):
            raise ValueError("difficulty_dag must be a mapping")
        dag_allowed = {
            "max_persistent_depth",
            "global_attack_verified_proposition_interval",
        }
        unknown_dag = sorted(set(dag_raw) - dag_allowed)
        if unknown_dag:
            raise ValueError(f"unknown difficulty_dag setting(s): {', '.join(unknown_dag)}")

        values = cls().to_dict()
        values.update(raw)
        values["verifier_agents"] = _parse_name_list(
            values["verifier_agents"], field="verifier_agents"
        )
        values["difficulty_dag"] = DifficultyDagPolicy(**dict(dag_raw))
        return cls(**values)

    def with_overrides(self, **overrides: Any) -> "SolverPolicy":
        """Apply explicit CLI/API overrides while retaining the validated base policy."""
        values = {name: value for name, value in overrides.items() if value is not None}
        if "verifier_agents" in values:
            values["verifier_agents"] = _parse_name_list(values["verifier_agents"], field="verifier_agents")
        return replace(self, **values)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable policy snapshot for logs and run manifests."""
        return asdict(self)


def _require_int(field: str, value: Any, *, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        comparator = ">=" if minimum else ">="
        raise ValueError(f"{field} must be an integer {comparator} {minimum}")


def _require_float(field: str, value: Any, *, minimum: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) < minimum:
        raise ValueError(f"{field} must be a number >= {minimum:g}")


def _require_probability(field: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"{field} must be a number in [0, 1]")


def _parse_name_list(value: Any, *, field: str) -> tuple[str, ...]:
    if isinstance(value, str):
        names = tuple(item.strip() for item in value.split(",") if item.strip())
    elif isinstance(value, (list, tuple)):
        names = tuple(str(item).strip() for item in value if str(item).strip())
    else:
        raise ValueError(f"{field} must be a list of non-empty agent names")
    _require_name_list(field, names)
    return names


def _require_name_list(field: str, value: Any) -> None:
    if not isinstance(value, tuple) or not value or any(not isinstance(name, str) or not name for name in value):
        raise ValueError(f"{field} must contain at least one non-empty agent name")
    if len(set(value)) != len(value):
        raise ValueError(f"{field} must not contain duplicate agent names")
