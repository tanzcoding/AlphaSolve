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
_SCHEMA_VERSION = 8
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
_STATUSES = {"open", "advanced", "ready_for_synthesis", "resolved", "refuted", "superseded"}
_RESOLUTION_POLICIES = {"all_of", "any_of", "manual"}
_RELATIONS = {"prerequisite", "alternative", "weakened_target", "method_blocked", "refutes"}
_TERMINAL = {"resolved", "refuted", "superseded"}
_CORRECTION_KINDS = {"remove_parent_edge", "supersede_node", "reopen_node"}

# Backward-compatible exports for callers that rely on the built-in defaults.
_DEFAULT_POLICY = DifficultyDagPolicy()
MAX_PERSISTENT_DIFFICULTY_DEPTH = _DEFAULT_POLICY.max_persistent_depth
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
        self.global_attack_path = self.workspace_dir / "curation_records" / "global_attack_state.json"
        self.audits_dir = self.workspace_dir / "progress_audits"
        self.evidence_checkpoints_dir = self.workspace_dir / "curation_records" / "evidence_checkpoints"

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
            state = {
                "schema_version": _SCHEMA_VERSION,
                "nodes": nodes,
                "aliases": aliases,
                "curated_checkpoints": curated,
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
        # Evidence checkpoints include targeted verified outcomes and explicit
        # maintenance corrections.  Discovery is marker-based rather than
        # name-based so historical graph repairs are queued just like ordinary
        # evidence curation.
        evidence_directories = (
            sorted(path for path in self.evidence_checkpoints_dir.iterdir() if path.is_dir())
            if self.evidence_checkpoints_dir.is_dir()
            else []
        )
        for directory in evidence_directories:
            try:
                ready = json.loads((directory / "curation_ready.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(ready, dict) or ready.get("status") != "ready":
                continue
            checkpoint_id = str(ready.get("checkpoint_id") or directory.name)
            if checkpoint_id not in curated:
                pending.append(checkpoint_id)
        return sorted(set(pending))

    def checkpoint_artifact_path(self, checkpoint_id: str) -> Path:
        evidence = self.evidence_checkpoints_dir / str(checkpoint_id) / "curator_brief.md"
        if evidence.is_file():
            return evidence
        return self.audits_dir / str(checkpoint_id) / "curator_brief.md"

    def checkpoint_task_kind(self, checkpoint_id: str) -> str:
        return "evidence_checkpoint" if (self.evidence_checkpoints_dir / str(checkpoint_id)).is_dir() else "portfolio_checkpoint"

    def is_checkpoint_curated(self, checkpoint_id: str) -> bool:
        return str(checkpoint_id) in self.load()["curated_checkpoints"]

    def _load_global_attack_state(self) -> dict[str, Any]:
        try:
            state = json.loads(self.global_attack_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
        return {"last_attempt": state.get("last_attempt") if isinstance(state.get("last_attempt"), dict) else None}

    def _save_global_attack_state(self, state: dict[str, Any]) -> None:
        self.global_attack_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.global_attack_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, self.global_attack_path)

    def global_attack_preflight(self) -> dict[str, Any]:
        """Return the count-based eligibility directive for a full-problem synthesis."""
        return self._global_attack_preflight(self._load_global_attack_state())

    def begin_global_attack(self, *, worker_id: str) -> dict[str, Any]:
        """Reserve the single global-attack slot outside the canonical difficulty graph."""
        worker_id = _id(worker_id, field="worker_id")
        with _LOCK:
            state = self._load_global_attack_state()
            directive = self._global_attack_preflight(state)
            if not directive["ready"]:
                raise ValueError(str(directive["message"]))
            attempt = {
                "attempt_id": f"global-{worker_id}",
                "worker_id": worker_id,
                "phase": "in_flight",
                "started_at": _now(),
                "completed_at": "",
                "status": "running",
                "outcome": "",
                "target_achieved": None,
                "solved_problem": False,
                "result_path": "",
                "verified_proposition_count_at_start": self._verified_proposition_count(),
                "verified_proposition_count_at_completion": None,
            }
            state["last_attempt"] = attempt
            self._save_global_attack_state(state)
            return self._global_attack_public(attempt, ready=False)

    def complete_global_attack(
        self,
        *,
        worker_id: str,
        status: str,
        target_achieved: bool | None,
        solved_problem: bool,
        result_path: str,
    ) -> dict[str, Any] | None:
        """Persist global-attack runtime state outside the canonical difficulty graph."""
        worker_id = _id(worker_id, field="worker_id")
        with _LOCK:
            state = self._load_global_attack_state()
            attempt = state.get("last_attempt")
            if not isinstance(attempt, dict) or attempt.get("worker_id") != worker_id:
                return None
            if attempt.get("phase") != "in_flight":
                return self._global_attack_public(attempt, ready=False)
            normalized_status = str(status or "failed").strip() or "failed"
            attempt.update({
                "phase": "completed",
                "completed_at": _now(),
                "status": normalized_status,
                "target_achieved": target_achieved,
                "solved_problem": bool(solved_problem),
                "outcome": "solved" if solved_problem else ("partial" if normalized_status == "verified" else "failed"),
                "result_path": str(result_path or "").strip(),
                "verified_proposition_count_at_completion": self._verified_proposition_count(),
            })
            self._save_global_attack_state(state)
            return self._global_attack_public(attempt, ready=False)

    def selection_snapshot(self) -> dict[str, Any]:
        state = self.load()
        leaves = self.executable_leaves(state=state)
        return {
            "dag_revision": state["updated_at"],
            "executable_difficulties": leaves,
            "reviewer_graph": self.reviewer_graph_projection(state=state),
            "reviewer_sources": self.reviewer_source_index(),
            "max_persistent_depth": self.policy.max_persistent_depth,
            "active_difficulty_count": sum(
                1 for node in state["nodes"].values()
                if isinstance(node, dict) and node.get("status") not in _TERMINAL
            ),
            "pending_checkpoints": self.pending_checkpoint_ids(),
            "global_consolidation_directive": self._global_attack_preflight(self._load_global_attack_state()),
        }

    def reviewer_graph_projection(self, *, state: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return every canonical node and edge without aliases or storage internals."""
        state = state or self.load()
        nodes = state["nodes"]
        children = self._children(nodes)
        public_nodes: list[dict[str, Any]] = []
        for difficulty_id, node in sorted(nodes.items()):
            if not isinstance(node, dict):
                continue
            progress = node.get("progress") if isinstance(node.get("progress"), dict) else {}
            attempts = [
                {
                    "recorded_at": str(item.get("recorded_at") or ""),
                    "method_id": str(item.get("method_id") or ""),
                    "status": str(item.get("status") or ""),
                    "verified_proposition_ref": str(item.get("verified_proposition_ref") or ""),
                }
                for item in progress.get("attempts") or []
                if isinstance(item, dict)
            ][-8:]
            public_nodes.append({
                "difficulty_id": difficulty_id,
                "statement": str(node.get("statement") or "")[:1600],
                "status": str(node.get("status") or "open"),
                "parent_ids": sorted(str(item) for item in node.get("parent_ids") or []),
                "child_ids": children.get(difficulty_id, []),
                "relation_to_parent": str(node.get("relation_to_parent") or "prerequisite"),
                "resolution_policy": str(node.get("resolution_policy") or "manual"),
                "evidence_refs": [str(item) for item in node.get("evidence_refs") or []][:16],
                "progress": {
                    "attempt_count": int(progress.get("attempt_count") or 0),
                    "last_attempt_at": str(progress.get("last_attempt_at") or ""),
                    "verified_proposition_refs": [str(item) for item in progress.get("verified_proposition_refs") or []][:16],
                    "recent_attempts": attempts,
                },
            })
        return {"nodes": public_nodes, "components": self._reviewer_components(public_nodes)}

    @staticmethod
    def _reviewer_components(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        lookup = {str(node["difficulty_id"]): node for node in nodes}
        adjacency = {difficulty_id: set() for difficulty_id in lookup}
        for node in nodes:
            difficulty_id = str(node["difficulty_id"])
            for parent_id in node.get("parent_ids") or []:
                if parent_id in adjacency:
                    adjacency[difficulty_id].add(parent_id)
                    adjacency[parent_id].add(difficulty_id)
        components: list[dict[str, Any]] = []
        remaining = set(lookup)
        while remaining:
            start = remaining.pop()
            stack, member_ids = [start], {start}
            while stack:
                current = stack.pop()
                for neighbor in adjacency[current]:
                    if neighbor not in member_ids:
                        member_ids.add(neighbor)
                        remaining.discard(neighbor)
                        stack.append(neighbor)
            members = [lookup[item] for item in sorted(member_ids)]
            statuses: dict[str, int] = {}
            for node in members:
                status = str(node["status"])
                statuses[status] = statuses.get(status, 0) + 1
            roots = sorted(node["difficulty_id"] for node in members if not node["parent_ids"])
            components.append({
                "root_ids": roots,
                "node_ids": sorted(member_ids),
                "status_counts": statuses,
                "attempt_count": sum(int((node.get("progress") or {}).get("attempt_count") or 0) for node in members),
            })
        return sorted(components, key=lambda item: item["root_ids"] or item["node_ids"])

    def reviewer_source_index(self) -> dict[str, list[dict[str, str]]]:
        return {
            "verified_propositions": self._source_index(self.workspace_dir / "verified_propositions", limit=160),
            "knowledge": self._source_index(self.workspace_dir / "knowledge", limit=160),
        }

    def _source_index(self, root: Path, *, limit: int) -> list[dict[str, str]]:
        try:
            paths = sorted(path for path in root.rglob("*.md") if path.is_file())
        except OSError:
            return []
        rows: list[dict[str, str]] = []
        for path in paths[:limit]:
            if path.name == "state.md":
                continue
            try:
                text = path.read_text(encoding="utf-8")[:1200]
            except OSError:
                continue
            title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), "")
            rows.append({
                "path": path.relative_to(self.workspace_dir).as_posix(),
                "title": title[:240],
            })
        return rows

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
        attempt = state.get("last_attempt")
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

    def dispatch_preflight(
        self,
        *,
        difficulty_id: str | None,
        method_id: str | None,
    ) -> dict[str, Any]:
        if not difficulty_id:
            return {"allowed": True}
        requested = _id(difficulty_id, field="difficulty_id")
        state = self.load()
        canonical = self._canonical_id(state, requested)
        node = state["nodes"].get(canonical)
        if not isinstance(node, dict):
            return {"allowed": False, "reason": "unknown_difficulty", "difficulty_id": requested}
        status = str(node.get("status") or "open")
        children = self._children(state["nodes"]).get(canonical, [])
        active_children = [child for child in children if state["nodes"][child].get("status") not in _TERMINAL]
        depth = self._depths(state["nodes"])[canonical]
        if status in _TERMINAL:
            return {"allowed": False, "reason": "difficulty_not_actionable", "difficulty": self._public_node(node, depth=depth)}
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
                    "This internal difficulty has active child difficulties; record why the parent-level attack adds evidence beyond them."
                ],
            }
        return {"allowed": True, "difficulty": self._public_node(node, dispatch_mode="direct", depth=depth)}

    def record_curation(
        self,
        *,
        checkpoint_id: str,
        difficulties: Any,
        resolved_difficulty_ids: Any = None,
        status_updates: Any = None,
        graph_corrections: Any = None,
    ) -> dict[str, Any]:
        checkpoint_id = _id(checkpoint_id, field="checkpoint_id")
        self._checkpoint_decision(checkpoint_id)
        resolved = [_id(item, field="resolved_difficulty_ids item") for item in _string_list(
            resolved_difficulty_ids, field="resolved_difficulty_ids", limit=64
        )]
        updates = self._normalize_status_updates(status_updates)
        corrections = self._normalize_graph_corrections(graph_corrections)
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
                for parent_id in item["parent_ids"]:
                    canonical_parent = self._canonical_id(state, parent_id)
                    if canonical_parent == item["difficulty_id"]:
                        raise ValueError(
                            "difficulty child must be a strict child of its parent; self-parenting or alias-equivalent edges are forbidden"
                        )
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
            self._sync_attempt_progress(state)
            for difficulty_id in resolved:
                canonical = self._canonical_id(state, difficulty_id)
                node = nodes.get(canonical)
                if not isinstance(node, dict):
                    raise ValueError(f"cannot resolve unknown difficulty_id: {difficulty_id}")
                if node.get("status") == "refuted":
                    raise ValueError(f"cannot resolve refuted difficulty_id: {difficulty_id}")
                node["status"] = "resolved"
                node["updated_at"] = _now()
            self._apply_status_updates(state, updates)
            self._apply_graph_corrections(state, corrections)
            self._validate_acyclic(nodes)
            self._validate_max_depth(nodes)
            self._validate_prerequisite_parent_status(nodes)
            self._propagate(state)
            state["curated_checkpoints"][checkpoint_id] = {
                "curated_at": _now(),
                "difficulty_ids": sorted(incoming_ids),
                "resolved_difficulty_ids": resolved,
                "status_updates": updates,
            }
            state["updated_at"] = _now()
            self._save(state)
        return {"recorded": True, "checkpoint_id": checkpoint_id, "selection": self.selection_snapshot()}

    def _sync_attempt_progress(self, state: dict[str, Any]) -> None:
        """Curator-side projection of immutable worker outcomes onto canonical nodes."""
        try:
            lines = (self.workspace_dir / "progress_audit_outcomes.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        attempts: dict[str, list[dict[str, Any]]] = {difficulty_id: [] for difficulty_id in state["nodes"]}
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or not record.get("difficulty_id"):
                continue
            canonical = self._canonical_id(state, str(record["difficulty_id"]))
            if canonical not in attempts:
                continue
            verified = str(record.get("verified_file") or "").strip()
            try:
                verified = Path(verified).resolve().relative_to(self.workspace_dir.resolve()).as_posix() if verified else ""
            except ValueError:
                pass
            attempts[canonical].append({
                "sequence": int(record.get("sequence") or 0),
                "recorded_at": str(record.get("recorded_at") or ""),
                "worker_id": str(record.get("worker_id") or ""),
                "method_id": str(record.get("method_id") or ""),
                "status": str(record.get("status") or ""),
                "verified_proposition_ref": verified,
            })
        for difficulty_id, node_attempts in attempts.items():
            node_attempts.sort(key=lambda item: (item["sequence"], item["recorded_at"]))
            verified_refs = sorted({item["verified_proposition_ref"] for item in node_attempts if item["verified_proposition_ref"]})
            node = state["nodes"][difficulty_id]
            node["progress"] = {
                "attempt_count": len(node_attempts),
                "attempts": node_attempts[-80:],
                "verified_proposition_refs": verified_refs[-80:],
                "last_attempt_at": node_attempts[-1]["recorded_at"] if node_attempts else "",
            }

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
            raw_handoff_ids = raw.get("source_handoff_ids")
            if raw_handoff_ids is None:
                raw_handoff_ids = raw.get("source_difficulty_ids")
            source_ids = [_source_id(item, field=f"difficulties[{index}].source_handoff_ids item") for item in _string_list(
                raw_handoff_ids, field=f"difficulties[{index}].source_handoff_ids"
            )]
            if not source_ids:
                raise ValueError("each curated difficulty requires source_handoff_ids from checkpoint handoffs")
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

    def _normalize_status_updates(self, value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 64:
            raise ValueError("status_updates must be an array with at most 64 items")
        updates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(value, 1):
            if not isinstance(raw, dict):
                raise ValueError("status_updates items must be objects")
            difficulty_id = _id(raw.get("difficulty_id"), field=f"status_updates[{index}].difficulty_id")
            if difficulty_id in seen:
                raise ValueError("status_updates must not contain duplicate difficulty_id values")
            seen.add(difficulty_id)
            status = str(raw.get("status") or "").strip()
            if status not in _STATUSES:
                raise ValueError(f"status_updates[{index}].status is invalid")
            evidence_refs = _string_list(
                raw.get("evidence_refs"), field=f"status_updates[{index}].evidence_refs", limit=20
            )
            if not evidence_refs:
                raise ValueError("each status update requires evidence_refs")
            updates.append({
                "difficulty_id": difficulty_id,
                "status": status,
                "evidence_refs": evidence_refs,
            })
        return updates

    def _apply_status_updates(self, state: dict[str, Any], updates: list[dict[str, Any]]) -> None:
        nodes = state["nodes"]
        for update in updates:
            difficulty_id = self._canonical_id(state, update["difficulty_id"])
            node = nodes.get(difficulty_id)
            if not isinstance(node, dict):
                raise ValueError(f"status update references unknown difficulty_id: {difficulty_id}")
            current = str(node.get("status") or "open")
            target = update["status"]
            if current in _TERMINAL and target != current:
                raise ValueError(
                    f"cannot replace terminal status {current!r} for {difficulty_id}; use an explicit graph correction if evidence overturns it"
                )
            if target == "open" and current != "open":
                raise ValueError("use graph_corrections.reopen_node to reopen a difficulty")
            node["status"] = target
            node["evidence_refs"] = sorted(set(node.get("evidence_refs") or []) | set(update["evidence_refs"]))
            node["updated_at"] = _now()
            if target == "refuted":
                self._supersede_descendants(nodes, difficulty_id)

    def _normalize_graph_corrections(self, value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 16:
            raise ValueError("graph_corrections must be an array with at most 16 items")
        corrections: list[dict[str, Any]] = []
        for index, raw in enumerate(value, 1):
            if not isinstance(raw, dict):
                raise ValueError("graph_corrections items must be objects")
            kind = str(raw.get("kind") or "").strip()
            if kind not in _CORRECTION_KINDS:
                raise ValueError(f"graph_corrections[{index}].kind is invalid")
            difficulty_id = _id(raw.get("difficulty_id"), field=f"graph_corrections[{index}].difficulty_id")
            parent_id = str(raw.get("parent_difficulty_id") or "").strip()
            if kind == "remove_parent_edge":
                parent_id = _id(parent_id, field=f"graph_corrections[{index}].parent_difficulty_id")
            elif parent_id:
                raise ValueError("parent_difficulty_id is only valid for remove_parent_edge")
            refs = _string_list(raw.get("evidence_refs"), field=f"graph_corrections[{index}].evidence_refs", limit=16)
            if not refs:
                raise ValueError("each graph correction requires evidence_refs")
            corrections.append({
                "kind": kind,
                "difficulty_id": difficulty_id,
                "parent_difficulty_id": parent_id,
                "evidence_refs": refs,
            })
        return corrections

    def _apply_graph_corrections(self, state: dict[str, Any], corrections: list[dict[str, Any]]) -> None:
        nodes = state["nodes"]
        for correction in corrections:
            difficulty_id = self._canonical_id(state, correction["difficulty_id"])
            node = nodes.get(difficulty_id)
            if not isinstance(node, dict):
                raise ValueError(f"graph correction references unknown difficulty_id: {difficulty_id}")
            kind = correction["kind"]
            if kind == "remove_parent_edge":
                parent_id = self._canonical_id(state, correction["parent_difficulty_id"])
                parents = list(node.get("parent_ids") or [])
                if parent_id not in parents:
                    raise ValueError(f"graph correction references absent parent edge: {parent_id} -> {difficulty_id}")
                node["parent_ids"] = [item for item in parents if item != parent_id]
            elif kind == "supersede_node":
                node["status"] = "superseded"
            elif kind == "reopen_node":
                node["status"] = "open"
            node["evidence_refs"] = sorted(set(node.get("evidence_refs") or []) | set(correction["evidence_refs"]))
            node["updated_at"] = _now()

    def _checkpoint_decision(self, checkpoint_id: str) -> None:
        try:
            value = json.loads((self.audits_dir / checkpoint_id / "decision.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            value = None
        if isinstance(value, dict) and value.get("status") in {"completed", "invalid_audit"}:
            return
        try:
            ready = json.loads(
                (self.evidence_checkpoints_dir / checkpoint_id / "curation_ready.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            ready = None
        if isinstance(ready, dict) and ready.get("status") == "ready":
            return
        raise ValueError(f"checkpoint is not ready for curation: {checkpoint_id}")

    def _normalize(self, state: dict[str, Any]) -> None:
        for difficulty_id, node in list(state["nodes"].items()):
            if not isinstance(node, dict):
                del state["nodes"][difficulty_id]
                continue
            node.setdefault("difficulty_id", difficulty_id)
            node.pop("route_id", None)
            node.setdefault("parent_ids", [])
            node.setdefault("resolution_policy", "manual")
            node.setdefault("relation_to_parent", "prerequisite")
            node.setdefault("evidence_refs", [])
            node.setdefault("source_ids", [])
            node.pop("attack_outcomes", None)
            node.pop("parent_direct_attacks", None)
            node.pop("split_required", None)
            node.setdefault("progress", {
                "attempt_count": 0,
                "attempts": [],
                "verified_proposition_refs": [],
                "last_attempt_at": "",
            })
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
            "progress": {
                "attempt_count": 0,
                "attempts": [],
                "verified_proposition_refs": [],
                "last_attempt_at": "",
            },
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
            resolved_refutations = [
                child
                for child in child_ids
                if nodes[child].get("relation_to_parent") == "refutes"
                and nodes[child].get("status") == "resolved"
            ]
            if resolved_refutations:
                node["status"] = "refuted"
                self._supersede_descendants(nodes, difficulty_id)
                changed = True
                continue
            resolved_weakenings = [
                child
                for child in child_ids
                if nodes[child].get("relation_to_parent") == "weakened_target"
                and nodes[child].get("status") == "resolved"
            ]
            if resolved_weakenings:
                if node.get("status") in {"open", "ready_for_synthesis"}:
                    node["status"] = "advanced"
                    changed = True
                continue
            completion_children = [
                child
                for child in child_ids
                if nodes[child].get("relation_to_parent") in {"prerequisite", "alternative"}
            ]
            if not completion_children:
                continue
            child_statuses = [str(nodes[child].get("status") or "open") for child in completion_children]
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
            elif all(status == "resolved" for status in child_statuses):
                if node.get("status") != "ready_for_synthesis":
                    node["status"] = "ready_for_synthesis"
                    changed = True
            elif node.get("status") == "ready_for_synthesis":
                node["status"] = "advanced"
                changed = True
        return changed

    def _supersede_descendants(self, nodes: dict[str, Any], root_id: str) -> None:
        children = self._children(nodes)
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
            if node.get("status") not in _TERMINAL:
                node["status"] = "superseded"
            stack.extend(children.get(difficulty_id, []))

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
                "do bounded worker-local reasoning, add a sibling/alternative, or create an independent component instead"
            )

    @staticmethod
    def _validate_prerequisite_parent_status(nodes: dict[str, Any]) -> None:
        """A resolved/refuted/superseded obligation cannot retain an open prerequisite.

        A child with ``relation_to_parent=prerequisite`` represents work required to
        settle its parent.  Route discovery belongs beside the route's target, not
        beneath a completed reduction node, so this invariant catches inverted
        dependency edges before they are persisted.
        """
        for child_id, child in nodes.items():
            if str(child.get("relation_to_parent") or "prerequisite") != "prerequisite":
                continue
            if str(child.get("status") or "open") in _TERMINAL:
                continue
            for parent_id in child.get("parent_ids") or []:
                parent = nodes.get(parent_id)
                if parent is None or str(parent.get("status") or "open") not in _TERMINAL:
                    continue
                raise ValueError(
                    f"open prerequisite {child_id!r} cannot have terminal parent {parent_id!r}; "
                    "attach it to the unresolved obligation it serves, or use a non-prerequisite relation"
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

    @staticmethod
    def _public_node(
        node: dict[str, Any],
        *,
        dispatch_mode: str | None = None,
        depth: int | None = None,
    ) -> dict[str, Any]:
        result = {
            "difficulty_id": node.get("difficulty_id"),
            "statement": node.get("statement"),
            "parent_difficulty_ids": list(node.get("parent_ids") or []),
            "relation_to_parent": node.get("relation_to_parent"),
            "resolution_policy": node.get("resolution_policy"),
            "status": node.get("status"),
            "evidence_refs": list(node.get("evidence_refs") or []),
            "progress": dict(node.get("progress") or {}),
        }
        if depth is not None:
            result["depth"] = depth
        if dispatch_mode:
            result["dispatch_mode"] = dispatch_mode
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
                status_updates=args.get("status_updates"),
                graph_corrections=args.get("graph_corrections"),
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
            "source_handoff_ids": {"type": "array", "items": {"type": "string"}},
            "source_difficulty_ids": {"type": "array", "items": {"type": "string"}, "description": "Legacy alias for source_handoff_ids."},
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["difficulty_id", "statement"],
    }
    registry.register(
        name="CurateDifficultyDag",
        description=(
            "At this checkpoint, reconcile worker obstacle handoffs and verified evidence into the canonical recursive difficulty DAG. "
            "Read the current DAG before choosing placement. For each new obligation, use source_handoff_ids copied from runtime handoffs; "
            "merge only evidence-supported handoffs into an existing canonical node, and create a node only for a separately checkable obligation. "
            "Existing handoff aliases are authoritative: reuse their canonical difficulty instead of reinterpreting them. A child must be strictly "
            "smaller than its parent; add a parent edge only when cited mathematics establishes that dependency. Parentless nodes are valid independent "
            "components. status_updates changes an existing node only with cited evidence; use status=refuted when a verified proposition directly contradicts "
            "its statement. The curator also refreshes attempt counts and verified-proposition references from the immutable outcome ledger. graph_corrections "
            "may only remove one evidenced parent edge, supersede one node, or reopen one node; they are for verified corrections, never route planning. "
            "The runtime validates acyclicity and computes executable leaves; do not invent edges or statuses from obstacle wording alone."
        ),
        parameters={
            "type": "object",
            "properties": {
                "difficulties": {"type": "array", "items": difficulty_schema},
                "resolved_difficulty_ids": {"type": "array", "items": {"type": "string"}},
                "status_updates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "difficulty_id": {"type": "string"},
                            "status": {"type": "string", "enum": sorted(_STATUSES)},
                            "evidence_refs": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["difficulty_id", "status", "evidence_refs"],
                    },
                },
                "graph_corrections": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": sorted(_CORRECTION_KINDS)},
                            "difficulty_id": {"type": "string"},
                            "parent_difficulty_id": {"type": "string"},
                            "evidence_refs": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["kind", "difficulty_id", "evidence_refs"],
                    },
                },
            },
            "required": ["difficulties"],
        },
        handler=handler,
        replace=replace,
    )


def register_orchestrator_difficulty_tools(
    registry: ToolRegistry,
    *,
    snapshot_handler,
) -> None:
    registry.register(
        name="DifficultyFrontier",
        description="Return curator-owned executable leaf difficulties, pending checkpoint status, and the global_consolidation_directive. Dispatch targeted workers only against a returned leaf; launch a full-problem global attack only when that directive reports ready=true.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=snapshot_handler,
    )
