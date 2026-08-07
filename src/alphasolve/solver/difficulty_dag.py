"""Curator-owned persistent difficulty DAG and deterministic leaf dispatcher."""
from __future__ import annotations

import json
import os
import re
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alphasolve.agent.tools import ToolRegistry, ToolResult

from .policy import DifficultyDagPolicy


_LOCK = threading.RLock()
_SCHEMA_VERSION = 4
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
_STATUSES = {"open", "advanced", "ready_for_synthesis", "resolved", "refuted", "superseded"}
_RESOLUTION_POLICIES = {"all_of", "any_of", "manual"}
_RELATIONS = {"prerequisite", "alternative", "weakened_target", "method_blocked", "refutes"}
_TERMINAL = {"resolved", "refuted", "superseded"}

# Backward-compatible exports for callers that rely on the built-in defaults.
_DEFAULT_POLICY = DifficultyDagPolicy()
MAX_PERSISTENT_DIFFICULTY_DEPTH = _DEFAULT_POLICY.max_persistent_depth
PARENT_DIRECT_ATTACK_OUTCOME_INTERVAL = _DEFAULT_POLICY.parent_direct_attack_outcome_interval
GLOBAL_ATTACK_VERIFIED_PROPOSITION_INTERVAL = _DEFAULT_POLICY.global_attack_verified_proposition_interval


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _id(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not _ID.fullmatch(text):
        raise ValueError(f"{field} must be a stable slug containing only letters, digits, '.', '_' or '-'")
    return text


def _source_id(value: Any, *, field: str) -> str:
    """Normalize legacy worker source labels without weakening canonical ID rules.

    Older checkpoint evidence identifies a local difficulty as ``direction/gap``.
    It is provenance, not a curator-chosen canonical ID, so accept that legacy
    spelling by converting separators to ``-`` before alias reconciliation.
    """
    text = str(value or "").strip()
    if _ID.fullmatch(text):
        return text
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip(".-_")
    if _ID.fullmatch(normalized):
        return normalized
    raise ValueError(
        f"{field} must be a stable source slug, or a legacy direction/gap label that can be normalized to one"
    )


def _text(value: Any, *, field: str, limit: int = 4000) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ValueError(f"{field} must not be empty")
    if len(text) > limit:
        raise ValueError(f"{field} exceeds {limit} characters")
    return text


def _string_list(value: Any, *, field: str, limit: int = 32) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"{field} must be an array with at most {limit} items")
    return [str(item).strip() for item in value if str(item).strip()]


class DifficultyDagStore:
    """The sole durable authority for recursive mathematical difficulties.

    Workers only emit local candidate records.  The curator gives them canonical
    identities and parent edges at checkpoints; this store validates and persists
    that structure.  The scheduler consumes only the deterministic executable leaves.
    """

    def __init__(
        self,
        workspace_dir: Path,
        *,
        policy: DifficultyDagPolicy | None = None,
    ) -> None:
        self.workspace_dir = Path(workspace_dir)
        self.policy = policy or DifficultyDagPolicy()
        self.path = self.workspace_dir / "curation_records" / "difficulty_dag.json"
        self.audits_dir = self.workspace_dir / "progress_audits"

    def load(self) -> dict[str, Any]:
        with _LOCK:
            try:
                state = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = self._migrate_legacy_blockers()
            if not isinstance(state, dict):
                state = {}
            nodes = state.get("nodes")
            if not isinstance(nodes, dict):
                nodes = {}
            curated = state.get("curated_checkpoints")
            if not isinstance(curated, dict):
                curated = {}
            aliases = state.get("aliases")
            if not isinstance(aliases, dict):
                aliases = {}
            global_attack = state.get("global_attack")
            if not isinstance(global_attack, dict):
                global_attack = {}
            state = {
                "schema_version": _SCHEMA_VERSION,
                "nodes": nodes,
                "aliases": aliases,
                "curated_checkpoints": curated,
                "global_attack": global_attack,
                "updated_at": str(state.get("updated_at") or ""),
            }
            self._normalize(state)
            return state

    def _migrate_legacy_blockers(self) -> dict[str, Any]:
        """Import prior blocker identities once as unparented roots, if present."""
        legacy_path = self.workspace_dir / "curation_records" / "blocker_registry.json"
        try:
            legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            legacy = {}
        nodes: dict[str, Any] = {}
        for raw_id, raw in (legacy.get("blockers") or {}).items() if isinstance(legacy, dict) else []:
            if not isinstance(raw, dict) or raw.get("status") not in {"active", "resolved"}:
                continue
            try:
                difficulty_id = _id(raw.get("blocker_id") or raw_id, field="legacy blocker_id")
                statement = _text(raw.get("statement"), field="legacy blocker statement", limit=4000)
            except ValueError:
                continue
            nodes[difficulty_id] = self._new_node(
                difficulty_id=difficulty_id,
                statement=statement,
                parent_ids=[],
                resolution_policy="manual",
                evidence_refs=[],
                source_ids=[f"legacy-{difficulty_id}"],
                status="resolved" if raw.get("status") == "resolved" else "open",
            )
        return {
            "schema_version": _SCHEMA_VERSION,
            "nodes": nodes,
            "aliases": {},
            "curated_checkpoints": {},
            "global_attack": {"last_attempt": None},
            "updated_at": _now() if nodes else "",
        }

    def pending_checkpoint_ids(self) -> list[str]:
        state = self.load()
        curated = state["curated_checkpoints"]
        pending: list[str] = []
        for directory in sorted(self.audits_dir.glob("checkpoint-*")):
            try:
                decision = json.loads((directory / "decision.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(decision, dict) or decision.get("status") not in {"completed", "invalid_audit"}:
                continue
            checkpoint_id = str(decision.get("checkpoint_id") or directory.name)
            if checkpoint_id not in curated:
                pending.append(checkpoint_id)
        return pending

    def is_checkpoint_curated(self, checkpoint_id: str) -> bool:
        return str(checkpoint_id) in self.load()["curated_checkpoints"]

    def global_attack_preflight(self) -> dict[str, Any]:
        """Return the count-based eligibility directive for a full-problem synthesis."""
        return self._global_attack_preflight(self.load())

    def begin_global_attack(self, *, worker_id: str) -> dict[str, Any]:
        """Reserve the single global-attack slot immediately before worker execution."""
        worker_id = _id(worker_id, field="worker_id")
        with _LOCK:
            state = self.load()
            directive = self._global_attack_preflight(state)
            if not directive["ready"]:
                raise ValueError(str(directive["message"]))
            verified_count = self._verified_proposition_count()
            attempt_id = f"global-{worker_id}"
            state["global_attack"]["last_attempt"] = {
                "attempt_id": attempt_id,
                "worker_id": worker_id,
                "phase": "in_flight",
                "started_at": _now(),
                "completed_at": "",
                "status": "running",
                "outcome": "",
                "target_achieved": None,
                "solved_problem": False,
                "result_path": "",
                "verified_proposition_count_at_start": verified_count,
                "verified_proposition_count_at_completion": None,
            }
            state["updated_at"] = _now()
            self._save(state)
            return self._global_attack_public(state["global_attack"]["last_attempt"], ready=False)

    def complete_global_attack(
        self,
        *,
        worker_id: str,
        status: str,
        target_achieved: bool | None,
        solved_problem: bool,
        result_path: str,
    ) -> dict[str, Any] | None:
        """Persist a finished global attack and its verified-proposition count baseline."""
        worker_id = _id(worker_id, field="worker_id")
        with _LOCK:
            state = self.load()
            attempt = state["global_attack"].get("last_attempt")
            if not isinstance(attempt, dict) or attempt.get("worker_id") != worker_id:
                return None
            if attempt.get("phase") != "in_flight":
                return self._global_attack_public(attempt, ready=False)
            normalized_status = str(status or "failed").strip() or "failed"
            attempt["phase"] = "completed"
            attempt["completed_at"] = _now()
            attempt["status"] = normalized_status
            attempt["target_achieved"] = target_achieved
            attempt["solved_problem"] = bool(solved_problem)
            attempt["outcome"] = "solved" if solved_problem else (
                "partial" if normalized_status == "verified" else "failed"
            )
            attempt["result_path"] = str(result_path or "").strip()
            # Take the baseline after completion so propositions created by this
            # attack do not immediately schedule another full-problem attempt.
            attempt["verified_proposition_count_at_completion"] = self._verified_proposition_count()
            state["updated_at"] = _now()
            self._save(state)
            return self._global_attack_public(attempt, ready=False)

    def selection_snapshot(self) -> dict[str, Any]:
        state = self.load()
        if self._propagate(state):
            state["updated_at"] = _now()
            self._save(state)
        leaves = self.executable_leaves(state=state)
        parent_attacks = self.parent_direct_candidates(state=state)
        return {
            "dag_revision": state["updated_at"],
            # Leaf work is the default high-frequency dispatch frontier.
            "executable_difficulties": leaves,
            # Internal claims remain available as low-frequency, fixed-target
            # consolidation attempts. They never replace leaf work as default.
            "parent_direct_difficulties": parent_attacks,
            "max_persistent_depth": self.policy.max_persistent_depth,
            "active_difficulty_count": sum(
                1 for node in state["nodes"].values()
                if isinstance(node, dict) and node.get("status") not in _TERMINAL
            ),
            "pending_checkpoints": self.pending_checkpoint_ids(),
            "global_consolidation_directive": self._global_attack_preflight(state),
        }

    def _verified_proposition_count(self) -> int:
        verified_dir = self.workspace_dir / "verified_propositions"
        try:
            return sum(
                1 for path in verified_dir.rglob("*.md")
                if path.is_file() and path.name not in {"index.md", "state.md"}
            )
        except OSError:
            return 0

    def _global_attack_preflight(self, state: dict[str, Any]) -> dict[str, Any]:
        verified_count = self._verified_proposition_count()
        interval = self.policy.global_attack_verified_proposition_interval
        attempt = state.get("global_attack", {}).get("last_attempt")
        if not isinstance(attempt, dict):
            remaining = max(0, interval - verified_count)
            return {
                "ready": verified_count >= interval,
                "reason": "initial_verified_proposition_threshold_met"
                if verified_count >= interval else "initial_verified_proposition_threshold_not_met",
                "message": "The initial global consolidation threshold is met."
                if verified_count >= interval else (
                    f"Wait for {remaining} more verified proposition(s) before the first global consolidation."
                ),
                "verified_proposition_interval": interval,
                "verified_proposition_count": verified_count,
                "verified_propositions_since_last_global_attack": verified_count,
                "remaining_verified_propositions": remaining,
                "last_attempt": None,
            }
        public = self._global_attack_public(attempt, ready=False)
        if attempt.get("phase") == "in_flight":
            return {
                **public,
                "ready": False,
                "reason": "global_attack_in_flight",
                "message": "Wait for the active global consolidation to finish.",
            }
        if attempt.get("solved_problem"):
            return {
                **public,
                "ready": False,
                "reason": "problem_already_solved",
                "message": "The latest global consolidation solved the original problem.",
            }
        baseline = max(0, int(attempt.get("verified_proposition_count_at_completion") or 0))
        since_last = max(0, verified_count - baseline)
        remaining = max(0, interval - since_last)
        return {
            **public,
            "ready": since_last >= interval,
            "reason": "verified_proposition_interval_met"
            if since_last >= interval else "verified_proposition_interval_not_met",
            "message": "Enough new verified propositions have accumulated for another global consolidation."
            if since_last >= interval else (
                f"Wait for {remaining} more verified proposition(s) before another global consolidation."
            ),
            "verified_proposition_interval": interval,
            "verified_proposition_count": verified_count,
            "verified_propositions_since_last_global_attack": since_last,
            "remaining_verified_propositions": remaining,
        }

    @staticmethod
    def _global_attack_public(attempt: dict[str, Any], *, ready: bool) -> dict[str, Any]:
        return {
            "ready": ready,
            "last_attempt": {
                "attempt_id": attempt.get("attempt_id"),
                "phase": attempt.get("phase"),
                "status": attempt.get("status"),
                "outcome": attempt.get("outcome"),
                "target_achieved": attempt.get("target_achieved"),
                "solved_problem": bool(attempt.get("solved_problem")),
                "result_path": attempt.get("result_path"),
                "verified_proposition_count_at_start": attempt.get("verified_proposition_count_at_start"),
                "verified_proposition_count_at_completion": attempt.get("verified_proposition_count_at_completion"),
            },
        }

    def executable_leaves(self, *, state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        state = state or self.load()
        self._propagate(state)
        nodes = state["nodes"]
        children = self._children(nodes)
        depths = self._depths(nodes)
        result: list[dict[str, Any]] = []
        for difficulty_id, node in sorted(nodes.items()):
            if not isinstance(node, dict):
                continue
            status = str(node.get("status") or "open")
            active_children = [child for child in children.get(difficulty_id, []) if nodes[child].get("status") not in _TERMINAL]
            if status in {"open", "advanced"} and not active_children:
                result.append(self._public_node(node, dispatch_mode="direct", depth=depths[difficulty_id]))
            elif status == "ready_for_synthesis" and not active_children:
                result.append(self._public_node(node, dispatch_mode="consolidation", depth=depths[difficulty_id]))
        return result

    def parent_direct_candidates(self, *, state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Return budget-eligible internal claims for fixed-target direct attack."""
        state = state or self.load()
        self._propagate(state)
        nodes = state["nodes"]
        children = self._children(nodes)
        depths = self._depths(nodes)
        result: list[dict[str, Any]] = []
        for difficulty_id, node in sorted(nodes.items()):
            if not isinstance(node, dict) or node.get("status") not in {"open", "advanced"}:
                continue
            active_children = [child for child in children.get(difficulty_id, []) if nodes[child].get("status") not in _TERMINAL]
            if not active_children:
                continue
            budget = self._parent_direct_budget(state, difficulty_id)
            if budget["available"]:
                result.append(self._public_node(
                    node,
                    dispatch_mode="parent_direct",
                    depth=depths[difficulty_id],
                    parent_direct_budget=budget,
                ))
        return result

    def dispatch_preflight(
        self,
        *,
        difficulty_id: str | None,
        method_id: str | None,
        require_curation: bool,
        parent_direct_attack: bool = False,
    ) -> dict[str, Any]:
        if require_curation:
            pending = self.pending_checkpoint_ids()
            if pending:
                return {
                    "allowed": False,
                    "reason": "difficulty_curation_pending",
                    "pending_checkpoints": pending,
                    "message": "Completed checkpoints must be curated into the difficulty DAG before targeted dispatch.",
                }
        if not difficulty_id:
            return {"allowed": True}
        requested = _id(difficulty_id, field="difficulty_id")
        state = self.load()
        canonical = self._canonical_id(state, requested)
        node = state["nodes"].get(canonical)
        if not isinstance(node, dict):
            return {"allowed": False, "reason": "unknown_difficulty", "difficulty_id": requested}
        if self._propagate(state):
            state["updated_at"] = _now()
            self._save(state)
        status = str(node.get("status") or "open")
        children = self._children(state["nodes"]).get(canonical, [])
        active_children = [child for child in children if state["nodes"][child].get("status") not in _TERMINAL]
        depth = self._depths(state["nodes"])[canonical]
        if status in _TERMINAL:
            return {"allowed": False, "reason": "difficulty_not_actionable", "difficulty": self._public_node(node, depth=depth)}
        if parent_direct_attack:
            if not active_children:
                return {
                    "allowed": False,
                    "reason": "parent_direct_attack_requires_active_children",
                    "difficulty": self._public_node(node, depth=depth),
                }
            budget = self._parent_direct_budget(state, canonical)
            warnings = [
                "Leaf-first is recommended: this fixed-target parent attack runs while child difficulties remain active."
            ]
            if not budget["available"]:
                warnings.append(
                    "The parent-direct interval is not yet replenished; the dispatch is permitted but should be justified against active leaf work."
                )
            return {
                "allowed": True,
                "difficulty": self._public_node(
                    node,
                    dispatch_mode="parent_direct",
                    depth=depth,
                    parent_direct_budget=budget,
                ),
                "open_child_difficulty_ids": active_children,
                "dispatch_warnings": warnings,
            }
        if status == "ready_for_synthesis" and not active_children:
            warnings = []
            if method_id != "consolidation":
                warnings.append(
                    "This ready-for-synthesis difficulty was normalized to fixed-target consolidation."
                )
            return {
                "allowed": True,
                "difficulty": self._public_node(node, dispatch_mode="consolidation", depth=depth),
                "child_difficulty_ids": children,
                "dispatch_warnings": warnings,
            }
        if active_children:
            return {
                "allowed": True,
                "difficulty": self._public_node(node, dispatch_mode="direct", depth=depth),
                "open_child_difficulty_ids": active_children,
                "dispatch_warnings": [
                    "Leaf-first is recommended: this internal difficulty still has active child difficulties."
                ],
            }
        return {"allowed": True, "difficulty": self._public_node(node, dispatch_mode="direct", depth=depth)}

    def register_parent_direct_attack(self, *, difficulty_id: str, worker_id: str) -> dict[str, Any]:
        """Reserve one low-frequency direct-parent attempt after a successful spawn."""
        state = self.load()
        canonical = self._canonical_id(state, _id(difficulty_id, field="difficulty_id"))
        node = state["nodes"].get(canonical)
        if not isinstance(node, dict):
            raise ValueError(f"unknown difficulty_id: {difficulty_id}")
        budget = self._parent_direct_budget(state, canonical)
        log = node.setdefault("parent_direct_attacks", [])
        log.append({
            "worker_id": _id(worker_id, field="worker_id"),
            "descendant_outcomes_at_dispatch": budget["descendant_outcomes"],
            "dispatched_at": _now(),
            "within_recommended_interval": bool(budget["available"]),
        })
        del log[:-20]
        node["updated_at"] = _now()
        state["updated_at"] = _now()
        self._save(state)
        return {
            "registered": True,
            "difficulty_id": canonical,
            "within_recommended_interval": bool(budget["available"]),
            "warning": None if budget["available"] else (
                "Parent-direct interval was not replenished; the attempt was recorded as an explicit leaf-first deviation."
            ),
            "budget": self._parent_direct_budget(self.load(), canonical),
        }

    def record_attack_outcome(
        self,
        *,
        difficulty_id: str,
        worker_id: str,
        outcome: str,
        summary: str,
        evidence_refs: Any = None,
    ) -> dict[str, Any]:
        if outcome not in {"resolved", "advanced", "unchanged", "refuted"}:
            raise ValueError("outcome must be resolved, advanced, unchanged, or refuted")
        state = self.load()
        canonical = self._canonical_id(state, _id(difficulty_id, field="difficulty_id"))
        node = state["nodes"].get(canonical)
        if not isinstance(node, dict):
            raise ValueError(f"unknown difficulty_id: {difficulty_id}")
        if node.get("status") in _TERMINAL:
            raise ValueError(f"difficulty {canonical} is already {node.get('status')}")
        event = {
            "worker_id": _id(worker_id, field="worker_id"),
            "outcome": outcome,
            "summary": _text(summary, field="summary", limit=4000),
            "evidence_refs": _string_list(evidence_refs, field="evidence_refs", limit=20),
            "recorded_at": _now(),
        }
        node.setdefault("attack_outcomes", []).append(event)
        del node["attack_outcomes"][:-80]
        if outcome == "resolved":
            node["status"] = "resolved"
        elif outcome == "refuted":
            node["status"] = "refuted"
        elif outcome == "advanced" and node.get("status") == "open":
            node["status"] = "advanced"
        node["updated_at"] = _now()
        self._propagate(state)
        state["updated_at"] = _now()
        self._save(state)
        return {"recorded": True, "difficulty": self._public_node(state["nodes"][canonical]), "selection": self.selection_snapshot()}

    def record_curation(
        self,
        *,
        checkpoint_id: str,
        difficulties: Any,
        resolved_difficulty_ids: Any = None,
    ) -> dict[str, Any]:
        checkpoint_id = _id(checkpoint_id, field="checkpoint_id")
        self._checkpoint_decision(checkpoint_id)
        resolved = [_id(item, field="resolved_difficulty_ids item") for item in _string_list(
            resolved_difficulty_ids, field="resolved_difficulty_ids", limit=64
        )]
        with _LOCK:
            state = self.load()
            # Existing source aliases are historical identity decisions. A later
            # checkpoint may add evidence, but cannot silently reinterpret a
            # source under a different canonical difficulty.
            normalized = self._reconcile_existing_aliases(
                state, self._normalize_difficulties(difficulties)
            )
            nodes = state["nodes"]
            incoming_ids = {item["difficulty_id"] for item in normalized}
            for item in normalized:
                if item["difficulty_id"] in nodes:
                    self._merge_node(nodes[item["difficulty_id"]], item)
                else:
                    nodes[item["difficulty_id"]] = self._new_node(**item)
                for source_id in item["source_ids"]:
                    state["aliases"][source_id] = item["difficulty_id"]
            for item in normalized:
                for parent_id in item["parent_ids"]:
                    canonical_parent = self._canonical_id(state, parent_id)
                    if canonical_parent not in nodes:
                        raise ValueError(f"difficulty {item['difficulty_id']} references unknown parent {parent_id}")
                    nodes[item["difficulty_id"]]["parent_ids"] = sorted(set(
                        nodes[item["difficulty_id"]].get("parent_ids") or []
                    ) | {canonical_parent})
            for difficulty_id in resolved:
                canonical = self._canonical_id(state, difficulty_id)
                node = nodes.get(canonical)
                if not isinstance(node, dict):
                    raise ValueError(f"cannot resolve unknown difficulty_id: {difficulty_id}")
                node["status"] = "resolved"
                node["updated_at"] = _now()
            self._validate_acyclic(nodes)
            self._validate_max_depth(nodes)
            self._propagate(state)
            state["curated_checkpoints"][checkpoint_id] = {
                "curated_at": _now(),
                "difficulty_ids": sorted(incoming_ids),
                "resolved_difficulty_ids": resolved,
            }
            state["updated_at"] = _now()
            self._save(state)
        return {"recorded": True, "checkpoint_id": checkpoint_id, "selection": self.selection_snapshot()}

    def _reconcile_existing_aliases(
        self,
        state: dict[str, Any],
        difficulties: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Pin curation proposals to existing source aliases before mutation.

        The curator may have a stale canonical label in a later checkpoint. If all
        cited sources already identify one canonical node, that identity wins. If
        the sources identify different nodes, the relationship is genuinely
        ambiguous and must remain a curator decision rather than being merged by
        runtime convenience.
        """
        rewrites: dict[str, str] = {}
        aliases = state.get("aliases") or {}
        for item in difficulties:
            proposed = self._canonical_id(state, item["difficulty_id"])
            existing = {
                self._canonical_id(state, str(aliases[source_id]))
                for source_id in item["source_ids"]
                if source_id in aliases
            }
            if len(existing) > 1:
                raise ValueError(
                    f"source difficulties for {item['difficulty_id']!r} already map to different canonical difficulties: "
                    f"{sorted(existing)}; submit them as separate curated difficulties"
                )
            rewrites[item["difficulty_id"]] = next(iter(existing), proposed)

        def rewrite(value: str) -> str:
            seen: set[str] = set()
            while value in rewrites and value not in seen:
                seen.add(value)
                next_value = rewrites[value]
                if next_value == value:
                    break
                value = next_value
            return self._canonical_id(state, value)

        merged: dict[str, dict[str, Any]] = {}
        for item in difficulties:
            rewritten = deepcopy(item)
            rewritten["difficulty_id"] = rewrite(item["difficulty_id"])
            rewritten["parent_ids"] = sorted({rewrite(parent_id) for parent_id in item["parent_ids"]})
            current = merged.get(rewritten["difficulty_id"])
            if current is None:
                merged[rewritten["difficulty_id"]] = rewritten
                continue
            current["source_ids"] = sorted(set(current["source_ids"]) | set(rewritten["source_ids"]))
            current["evidence_refs"] = sorted(set(current["evidence_refs"]) | set(rewritten["evidence_refs"]))
            current["parent_ids"] = sorted(set(current["parent_ids"]) | set(rewritten["parent_ids"]))
            if current["status"] == "open" and rewritten["status"] != "open":
                current["status"] = rewritten["status"]
        return list(merged.values())

    def _normalize_difficulties(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or len(value) > 64:
            raise ValueError("difficulties must be an array with at most 64 items")
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(value, 1):
            if not isinstance(raw, dict):
                raise ValueError("difficulties items must be objects")
            difficulty_id = _id(raw.get("difficulty_id"), field=f"difficulties[{index}].difficulty_id")
            if difficulty_id in seen:
                raise ValueError("difficulties must not contain duplicate difficulty_id values")
            seen.add(difficulty_id)
            policy = str(raw.get("resolution_policy") or "manual").strip()
            if policy not in _RESOLUTION_POLICIES:
                raise ValueError(f"difficulties[{index}].resolution_policy must be one of {sorted(_RESOLUTION_POLICIES)}")
            relation = str(raw.get("relation_to_parent") or "prerequisite").strip()
            if relation not in _RELATIONS:
                raise ValueError(f"difficulties[{index}].relation_to_parent must be one of {sorted(_RELATIONS)}")
            status = str(raw.get("status") or "open").strip()
            if status not in {"open", "advanced", "resolved", "refuted"}:
                raise ValueError("curator may set only open, advanced, resolved, or refuted status")
            parents = [_id(item, field=f"difficulties[{index}].parent_difficulty_ids item") for item in _string_list(
                raw.get("parent_difficulty_ids"), field=f"difficulties[{index}].parent_difficulty_ids"
            )]
            source_ids = [_source_id(item, field=f"difficulties[{index}].source_difficulty_ids item") for item in _string_list(
                raw.get("source_difficulty_ids"), field=f"difficulties[{index}].source_difficulty_ids"
            )]
            if not source_ids:
                raise ValueError("each curated difficulty requires source_difficulty_ids from worker handoffs")
            items.append({
                "difficulty_id": difficulty_id,
                "statement": _text(raw.get("statement"), field=f"difficulties[{index}].statement"),
                "parent_ids": parents,
                "resolution_policy": policy,
                "relation_to_parent": relation,
                "evidence_refs": _string_list(raw.get("evidence_refs"), field=f"difficulties[{index}].evidence_refs", limit=20),
                "source_ids": source_ids,
                "status": status,
            })
        return items

    def _checkpoint_decision(self, checkpoint_id: str) -> None:
        try:
            value = json.loads((self.audits_dir / checkpoint_id / "decision.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise ValueError(f"checkpoint decision is unavailable: {checkpoint_id}") from None
        if not isinstance(value, dict) or value.get("status") not in {"completed", "invalid_audit"}:
            raise ValueError(f"checkpoint is not ready for curation: {checkpoint_id}")

    def _normalize(self, state: dict[str, Any]) -> None:
        global_attack = state.setdefault("global_attack", {})
        if not isinstance(global_attack, dict):
            global_attack = {}
            state["global_attack"] = global_attack
        attempt = global_attack.get("last_attempt")
        if not isinstance(attempt, dict):
            global_attack["last_attempt"] = None
        else:
            attempt.setdefault("attempt_id", "")
            attempt.setdefault("worker_id", "")
            attempt.setdefault("phase", "completed")
            attempt.setdefault("started_at", "")
            attempt.setdefault("completed_at", "")
            attempt.setdefault("status", "")
            attempt.setdefault("outcome", "")
            attempt.setdefault("target_achieved", None)
            attempt.setdefault("solved_problem", False)
            attempt.setdefault("result_path", "")
            attempt.setdefault("verified_proposition_count_at_start", 0)
            # Older global-attack records did not have a count baseline. Treat
            # them as due for a fresh count-based cycle after this upgrade.
            attempt.setdefault("verified_proposition_count_at_completion", 0)
        for difficulty_id, node in list(state["nodes"].items()):
            if not isinstance(node, dict):
                del state["nodes"][difficulty_id]
                continue
            node.setdefault("difficulty_id", difficulty_id)
            node.setdefault("parent_ids", [])
            node.setdefault("resolution_policy", "manual")
            node.setdefault("relation_to_parent", "prerequisite")
            node.setdefault("evidence_refs", [])
            node.setdefault("source_ids", [])
            node.setdefault("attack_outcomes", [])
            node.setdefault("parent_direct_attacks", [])
            node.setdefault("status", "open")
            node.setdefault("created_at", "")
            node.setdefault("updated_at", "")

    def _new_node(
        self,
        *,
        difficulty_id: str,
        statement: str,
        parent_ids: list[str],
        resolution_policy: str,
        relation_to_parent: str = "prerequisite",
        evidence_refs: list[str],
        source_ids: list[str],
        status: str = "open",
    ) -> dict[str, Any]:
        return {
            "difficulty_id": difficulty_id,
            "statement": statement,
            "parent_ids": sorted(set(parent_ids)),
            "resolution_policy": resolution_policy,
            "relation_to_parent": relation_to_parent,
            "evidence_refs": sorted(set(evidence_refs)),
            "source_ids": sorted(set(source_ids)),
            "status": status,
            "attack_outcomes": [],
            "parent_direct_attacks": [],
            "created_at": _now(),
            "updated_at": _now(),
        }

    def _merge_node(self, node: dict[str, Any], incoming: dict[str, Any]) -> None:
        node["statement"] = incoming["statement"]
        node["resolution_policy"] = incoming["resolution_policy"]
        node["relation_to_parent"] = incoming["relation_to_parent"]
        node["parent_ids"] = sorted(set(node.get("parent_ids") or []) | set(incoming["parent_ids"]))
        node["source_ids"] = sorted(set(node.get("source_ids") or []) | set(incoming["source_ids"]))
        node["evidence_refs"] = sorted(set(node.get("evidence_refs") or []) | set(incoming["evidence_refs"]))
        if node.get("status") not in _TERMINAL:
            node["status"] = incoming["status"]
        node["updated_at"] = _now()

    @staticmethod
    def _children(nodes: dict[str, Any]) -> dict[str, list[str]]:
        children: dict[str, list[str]] = {difficulty_id: [] for difficulty_id in nodes}
        for child_id, node in nodes.items():
            if not isinstance(node, dict):
                continue
            for parent_id in node.get("parent_ids") or []:
                if parent_id in children:
                    children[parent_id].append(child_id)
        return {key: sorted(value) for key, value in children.items()}

    def _propagate(self, state: dict[str, Any]) -> bool:
        changed = False
        nodes = state["nodes"]
        children = self._children(nodes)
        depths = self._depths(nodes)
        for difficulty_id in sorted(nodes, key=lambda item: depths[item], reverse=True):
            node = nodes[difficulty_id]
            child_ids = children.get(difficulty_id, [])
            if not child_ids or node.get("status") in _TERMINAL:
                continue
            child_statuses = [str(nodes[child].get("status") or "open") for child in child_ids]
            policy = str(node.get("resolution_policy") or "manual")
            if policy == "any_of":
                if "resolved" in child_statuses:
                    if node.get("status") != "resolved":
                        node["status"] = "resolved"
                        changed = True
                    for child in child_ids:
                        if nodes[child].get("status") not in _TERMINAL:
                            nodes[child]["status"] = "superseded"
                            changed = True
                # Exhausting known alternatives refutes the decomposition routes,
                # not the parent theorem. Keep the parent open for replanning or a
                # fixed-target direct attack.
            elif all(status == "resolved" for status in child_statuses):
                if node.get("status") != "ready_for_synthesis":
                    node["status"] = "ready_for_synthesis"
                    changed = True
            elif node.get("status") == "ready_for_synthesis":
                # A later curator checkpoint can add or reopen a child; synthesis is then no
                # longer the current frontier and must not remain exposed as an executable leaf.
                node["status"] = "advanced"
                changed = True
            # A refuted prerequisite or weakened target similarly invalidates only
            # that child route. It must never auto-refute an ancestor claim.
        return changed

    @staticmethod
    def _depths(nodes: dict[str, Any]) -> dict[str, int]:
        memo: dict[str, int] = {}
        def depth(difficulty_id: str, seen: set[str]) -> int:
            if difficulty_id in memo:
                return memo[difficulty_id]
            if difficulty_id in seen:
                return 0
            parents = nodes[difficulty_id].get("parent_ids") or []
            value = 0 if not parents else 1 + max((depth(parent, seen | {difficulty_id}) for parent in parents if parent in nodes), default=0)
            memo[difficulty_id] = value
            return value
        return {difficulty_id: depth(difficulty_id, set()) for difficulty_id in nodes}

    def _validate_max_depth(self, nodes: dict[str, Any]) -> None:
        max_depth = self.policy.max_persistent_depth
        depths = self._depths(nodes)
        too_deep = sorted(
            difficulty_id for difficulty_id, depth in depths.items()
            if depth > max_depth
        )
        if too_deep:
            raise ValueError(
                f"persistent difficulty depth exceeds {max_depth}: {too_deep}; "
                "do bounded worker-local reasoning, add a sibling/alternative, or replan an ancestor instead"
            )

    def _validate_acyclic(self, nodes: dict[str, Any]) -> None:
        visited: set[str] = set()
        stack: set[str] = set()
        def visit(difficulty_id: str) -> None:
            if difficulty_id in stack:
                raise ValueError("difficulty DAG must not contain a cycle")
            if difficulty_id in visited:
                return
            stack.add(difficulty_id)
            for parent_id in nodes[difficulty_id].get("parent_ids") or []:
                if parent_id in nodes:
                    visit(parent_id)
            stack.remove(difficulty_id)
            visited.add(difficulty_id)
        for difficulty_id in nodes:
            visit(difficulty_id)

    @staticmethod
    def _canonical_id(state: dict[str, Any], difficulty_id: str) -> str:
        return str(state.get("aliases", {}).get(difficulty_id) or difficulty_id)

    def _parent_direct_budget(self, state: dict[str, Any], difficulty_id: str) -> dict[str, Any]:
        node = state["nodes"][difficulty_id]
        interval = self.policy.parent_direct_attack_outcome_interval
        descendant_outcomes = self._descendant_outcome_count(state["nodes"], difficulty_id)
        history = node.get("parent_direct_attacks") or []
        last = history[-1] if isinstance(history, list) and history else None
        previous_outcomes = int(last.get("descendant_outcomes_at_dispatch") or 0) if isinstance(last, dict) else 0
        new_outcomes = descendant_outcomes - previous_outcomes
        return {
            "available": last is None or new_outcomes >= interval,
            "interval": interval,
            "descendant_outcomes": descendant_outcomes,
            "new_descendant_outcomes": new_outcomes,
            "next_available_after": max(0, interval - new_outcomes),
            "direct_attack_count": len(history) if isinstance(history, list) else 0,
        }

    def _descendant_outcome_count(self, nodes: dict[str, Any], root_id: str) -> int:
        children = self._children(nodes)
        count = 0
        stack = list(children.get(root_id, []))
        seen: set[str] = set()
        while stack:
            difficulty_id = stack.pop()
            if difficulty_id in seen:
                continue
            seen.add(difficulty_id)
            node = nodes.get(difficulty_id)
            if not isinstance(node, dict):
                continue
            count += len(node.get("attack_outcomes") or [])
            stack.extend(children.get(difficulty_id, []))
        return count

    @staticmethod
    def _public_node(
        node: dict[str, Any],
        *,
        dispatch_mode: str | None = None,
        depth: int | None = None,
        parent_direct_budget: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = {
            "difficulty_id": node.get("difficulty_id"),
            "statement": node.get("statement"),
            "parent_difficulty_ids": list(node.get("parent_ids") or []),
            "resolution_policy": node.get("resolution_policy"),
            "status": node.get("status"),
            "evidence_refs": list(node.get("evidence_refs") or []),
            "attack_count": len(node.get("attack_outcomes") or []),
        }
        if depth is not None:
            result["depth"] = depth
        if dispatch_mode:
            result["dispatch_mode"] = dispatch_mode
        if parent_direct_budget is not None:
            result["parent_direct_budget"] = parent_direct_budget
        return result

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)


def register_curated_difficulty_dag_tool(
    registry: ToolRegistry,
    *,
    difficulty_dag: DifficultyDagStore,
    checkpoint_id: str | None,
    replace: bool = False,
) -> None:
    """Give the curator the sole structure-writing capability for the DAG."""
    def handler(args: dict[str, Any]) -> ToolResult:
        try:
            if checkpoint_id is None:
                raise ValueError("CurateDifficultyDag is available only during a portfolio checkpoint task")
            result = difficulty_dag.record_curation(
                checkpoint_id=checkpoint_id,
                difficulties=args.get("difficulties"),
                resolved_difficulty_ids=args.get("resolved_difficulty_ids"),
            )
        except ValueError as exc:
            return ToolResult(json.dumps({"error": str(exc)}, ensure_ascii=False), is_error=True)
        return ToolResult(json.dumps(result, ensure_ascii=False))

    difficulty_schema = {
        "type": "object",
        "properties": {
            "difficulty_id": {"type": "string"},
            "statement": {"type": "string"},
            "parent_difficulty_ids": {"type": "array", "items": {"type": "string"}},
            "relation_to_parent": {"type": "string", "enum": sorted(_RELATIONS)},
            "resolution_policy": {"type": "string", "enum": sorted(_RESOLUTION_POLICIES)},
            "status": {"type": "string", "enum": ["open", "advanced", "resolved", "refuted"]},
            "source_difficulty_ids": {"type": "array", "items": {"type": "string"}},
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["difficulty_id", "statement", "source_difficulty_ids"],
    }
    registry.register(
        name="CurateDifficultyDag",
        description=(
            "At this checkpoint, make worker-local difficulty records into the canonical recursive difficulty DAG. "
            "Choose stable canonical IDs, merge only evidence-supported aliases through source_difficulty_ids, and attach each "
            "new obligation to its actual parent difficulty. Existing source aliases are authoritative: reuse their canonical "
            "difficulty instead of reinterpreting them. Legacy direction/gap source labels are accepted and normalized to slugs. "
            "Use all_of, any_of, or manual only when the evidence establishes that relation. The runtime validates acyclicity "
            "and computes executable leaves; do not invent edges from wording alone."
        ),
        parameters={
            "type": "object",
            "properties": {
                "difficulties": {"type": "array", "items": difficulty_schema},
                "resolved_difficulty_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["difficulties"],
        },
        handler=handler,
        replace=replace,
    )


def register_orchestrator_difficulty_tools(
    registry: ToolRegistry,
    *,
    outcome_handler,
    snapshot_handler,
) -> None:
    registry.register(
        name="RecordDifficultyOutcome",
        description=(
            "Record the evidence-backed outcome of one worker assigned to a canonical difficulty. This does not edit "
            "parent/child structure: only the curator does that at checkpoints. Use resolved only when the exact assigned "
            "difficulty is established, refuted only with a checkable counterexample, advanced for a genuine narrowed boundary, "
            "and unchanged otherwise."
        ),
        parameters={
            "type": "object",
            "properties": {
                "worker_id": {"type": "string"},
                "outcome": {"type": "string", "enum": ["resolved", "advanced", "unchanged", "refuted"]},
                "summary": {"type": "string"},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["worker_id", "outcome", "summary"],
        },
        handler=outcome_handler,
    )
    registry.register(
        name="DifficultyFrontier",
        description="Return curator-owned executable leaf difficulties, pending checkpoint status, and the global_consolidation_directive. Dispatch targeted workers only against a returned leaf; launch a full-problem global attack only when that directive reports ready=true.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=snapshot_handler,
    )
