"""Curator-owned, restart-safe registry for repeated mathematical blockers."""
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


_LOCK = threading.RLock()
_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
_SCHEMA_VERSION = 2
_MAX_BLOCKERS = 20
_MAX_APPROACHES = 16
_RELATIONS = {"same", "distinct", "unresolved", "superseded"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _clean_id(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not _ID_PATTERN.fullmatch(text):
        raise ValueError(f"{field} must be a stable identifier")
    return text


def _clean_difficulty_id(value: Any, *, field: str) -> str:
    """Validate a current-difficulty row label without conflating it with its gate.

    A difficulty id is a short semantic slug.  The actionable route identity belongs
    in ``gate={direction_id,gap_id}``; accepting ``direction/gap`` here previously
    caused curator retries to fail permanently on the slash character.
    """
    text = str(value or "").strip()
    if not _ID_PATTERN.fullmatch(text):
        suffix = " Use a slug such as 'audit-fooling-universal-lis-lds-bound'; put direction/gap in gate."
        raise ValueError(f"{field} must be a stable identifier containing only letters, digits, '.', '_' or '-'.{suffix}")
    return text


def _clean_text(value: Any, *, field: str, max_length: int) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ValueError(f"{field} must not be empty")
    if len(text) > max_length:
        raise ValueError(f"{field} exceeds {max_length} characters")
    return text


class PersistentBlockerRegistry:
    """The curator's authoritative blocker grouping and dispatch-gate record.

    The registry deliberately stores outcome *sequences*, not an LLM-maintained count.
    Counts and per-approach provenance are reconstructed from those durable references.
    """

    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = Path(workspace_dir)
        self.path = self.workspace_dir / "curation_records" / "blocker_registry.json"
        self.outcomes_path = self.workspace_dir / "progress_audit_outcomes.jsonl"
        self.audits_dir = self.workspace_dir / "progress_audits"

    def load(self) -> dict[str, Any]:
        with _LOCK:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = {}
            if not isinstance(raw, dict):
                raw = {}
            raw_version = raw.get("schema_version", 1)
            raw.setdefault("blockers", {})
            raw.setdefault("curated_checkpoints", {})
            raw.setdefault("updated_at", "")
            if not isinstance(raw["blockers"], dict):
                raw["blockers"] = {}
            if not isinstance(raw["curated_checkpoints"], dict):
                raw["curated_checkpoints"] = {}
            if not isinstance(raw_version, int) or raw_version < _SCHEMA_VERSION:
                legacy_active_ids = sorted(
                    str(blocker_id)
                    for blocker_id, record in raw["blockers"].items()
                    if isinstance(record, dict) and record.get("status") == "active"
                )
                raw["schema_version"] = _SCHEMA_VERSION
                raw["reconciliation"] = {
                    "status": "pending" if legacy_active_ids else "complete",
                    "legacy_active_blocker_ids": legacy_active_ids,
                    "required_since": _now_iso(),
                }
            else:
                raw["schema_version"] = _SCHEMA_VERSION
                raw.setdefault("reconciliation", {"status": "complete", "legacy_active_blocker_ids": []})
            return raw

    def pending_checkpoint_ids(self) -> list[str]:
        state = self.load()
        curated = state.get("curated_checkpoints") or {}
        pending: list[str] = []
        for checkpoint_dir in sorted(self.audits_dir.glob("checkpoint-*")):
            decision_path = checkpoint_dir / "decision.json"
            try:
                decision = json.loads(decision_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(decision, dict) or decision.get("status") not in {"completed", "invalid_audit"}:
                continue
            checkpoint_id = str(decision.get("checkpoint_id") or checkpoint_dir.name)
            curation = curated.get(checkpoint_id)
            if not isinstance(curation, dict) or curation.get("protocol_version") != _SCHEMA_VERSION:
                pending.append(checkpoint_id)
        return pending

    def is_checkpoint_curated(self, checkpoint_id: str) -> bool:
        record = (self.load().get("curated_checkpoints") or {}).get(str(checkpoint_id))
        return isinstance(record, dict) and record.get("protocol_version") == _SCHEMA_VERSION

    def active_for_direction(self, direction_id: str | None) -> list[dict[str, Any]]:
        if not direction_id:
            return []
        direction = str(direction_id).strip()
        return [
            deepcopy(record)
            for record in (self.load().get("blockers") or {}).values()
            if isinstance(record, dict)
            and record.get("status") == "active"
            and record.get("gate", {}).get("direction_id") == direction
        ]

    def dispatch_preflight(
        self,
        *,
        direction_id: str | None,
        gap_id: str | None,
        blocker_override_reason: str | None = None,
        require_curation: bool,
    ) -> dict[str, Any]:
        if require_curation:
            pending = self.pending_checkpoint_ids()
            if pending:
                return {
                    "allowed": False,
                    "reason": "blocker_curation_pending",
                    "pending_checkpoints": pending,
                    "message": (
                        "Completed progress audits have not yet been curated into the persistent blocker registry. "
                        "Wait for curator classification before dispatching another targeted worker."
                    ),
                }
        if not direction_id or not gap_id:
            return {"allowed": True}
        override = str(blocker_override_reason or "").strip()
        active = self.active_for_direction(direction_id)
        mismatched = [item for item in active if item.get("gate", {}).get("gap_id") != gap_id]
        if mismatched and not override:
            return {
                "allowed": False,
                "reason": "active_curated_blocker",
                "blockers": mismatched,
                "message": (
                    "The curator has recorded a persistent repeated blocker for this direction. Dispatch its gate gap "
                    "directly, or provide concrete new evidence for an explicit pivot."
                ),
            }
        return {"allowed": True, "blockers": active}

    def record_curation(
        self,
        *,
        checkpoint_id: str,
        blockers: Any,
        resolved_blocker_ids: Any,
        current_difficulties: Any = None,
        blocker_relations: Any = None,
    ) -> dict[str, Any]:
        checkpoint_id = _clean_id(checkpoint_id, field="checkpoint_id")
        decision = self._checkpoint_decision(checkpoint_id)
        watermark = int(decision.get("watermark") or 0)
        normalized_blockers = self._normalize_blockers(blockers, watermark=watermark)
        resolved_ids = self._normalize_resolved_ids(resolved_blocker_ids)
        overlap = {item["blocker_id"] for item in normalized_blockers} & set(resolved_ids)
        if overlap:
            raise ValueError("a blocker cannot be active and resolved in the same curation")

        with _LOCK:
            state = self.load()
            records = state["blockers"]
            initial_active_ids = sorted(
                blocker_id for blocker_id, record in records.items()
                if isinstance(record, dict) and record.get("status") == "active"
            )
            difficulties = self._normalize_current_difficulties(
                current_difficulties,
                decision=decision,
                active_blocker_ids=initial_active_ids,
            )
            relations = self._normalize_blocker_relations(
                blocker_relations,
                difficulties=difficulties,
                active_blocker_ids=[item for item in initial_active_ids if item not in resolved_ids],
                known_blocker_ids=set(records) | {item["blocker_id"] for item in normalized_blockers},
            )
            self._validate_identity_decisions(
                difficulties=difficulties,
                relations=relations,
                normalized_blockers=normalized_blockers,
                resolved_ids=resolved_ids,
            )

            incoming_by_id = {item["blocker_id"]: item for item in normalized_blockers}
            same_groups: dict[str, list[str]] = {}
            for relation in relations:
                if relation["relation"] == "same":
                    same_groups.setdefault(relation["blocker_id"], []).append(relation["active_blocker_id"])
            merged_ids: list[str] = []
            for canonical_id, same_ids in same_groups.items():
                duplicate_ids = [item for item in same_ids if item != canonical_id]
                existing_records = [records.get(item) for item in same_ids if isinstance(records.get(item), dict)]
                existing = self._combine_existing_records(existing_records, canonical_id=canonical_id)
                incoming = incoming_by_id.get(canonical_id)
                if incoming is None:
                    raise ValueError(f"same relation requires blocker payload for canonical id: {canonical_id}")
                records[canonical_id] = self._merge_active_record(existing, incoming, checkpoint_id=checkpoint_id)
                for duplicate_id in duplicate_ids:
                    duplicate = records.get(duplicate_id)
                    if isinstance(duplicate, dict):
                        duplicate["status"] = "merged"
                        duplicate["merged_into"] = canonical_id
                        duplicate["merged_at"] = _now_iso()
                        duplicate["last_checkpoint_id"] = checkpoint_id
                        merged_ids.append(duplicate_id)

            for record in normalized_blockers:
                blocker_id = record["blocker_id"]
                if blocker_id in same_groups:
                    continue
                records[blocker_id] = self._merge_active_record(
                    records.get(blocker_id), record, checkpoint_id=checkpoint_id
                )

            superseded_ids: list[str] = []
            for relation in relations:
                if relation["relation"] != "superseded":
                    continue
                old = records.get(relation["active_blocker_id"])
                if isinstance(old, dict):
                    old["status"] = "superseded"
                    old["superseded_by"] = relation["blocker_id"]
                    old["superseded_at"] = _now_iso()
                    old["last_checkpoint_id"] = checkpoint_id
                    superseded_ids.append(relation["active_blocker_id"])

            for blocker_id in resolved_ids:
                existing = records.get(blocker_id)
                if not isinstance(existing, dict):
                    raise ValueError(f"cannot resolve unknown blocker_id: {blocker_id}")
                existing["status"] = "resolved"
                existing["resolved_at"] = _now_iso()
                existing["last_checkpoint_id"] = checkpoint_id

            state["reconciliation"] = {
                "status": "complete",
                "legacy_active_blocker_ids": [],
                "completed_at": _now_iso(),
                "checkpoint_id": checkpoint_id,
            }
            state["curated_checkpoints"][checkpoint_id] = {
                "protocol_version": _SCHEMA_VERSION,
                "curated_at": _now_iso(),
                "watermark": watermark,
                "active_blocker_ids_at_start": initial_active_ids,
                "current_difficulties": difficulties,
                "blocker_relations": relations,
                "active_blocker_ids": [item["blocker_id"] for item in normalized_blockers],
                "resolved_blocker_ids": resolved_ids,
                "merged_blocker_ids": sorted(set(merged_ids)),
                "superseded_blocker_ids": sorted(set(superseded_ids)),
            }
            state["updated_at"] = _now_iso()
            self._save_locked(state)
            result = {
                "recorded": True,
                "checkpoint_id": checkpoint_id,
                "active_blockers": [deepcopy(records[item["blocker_id"]]) for item in normalized_blockers],
                "resolved_blocker_ids": resolved_ids,
                "merged_blocker_ids": sorted(set(merged_ids)),
                "superseded_blocker_ids": sorted(set(superseded_ids)),
            }
        try:
            from .research_state import ResearchStateStore
            ResearchStateStore(self.workspace_dir / "verified_propositions").refresh_markdown_view()
        except Exception:
            # Registry durability must not depend on refreshing a derived Markdown view.
            pass
        return result

    def _normalize_current_difficulties(
        self,
        value: Any,
        *,
        decision: dict[str, Any],
        active_blocker_ids: list[str],
    ) -> list[dict[str, Any]]:
        if value is None:
            value = []
        if not isinstance(value, list) or len(value) > _MAX_BLOCKERS:
            raise ValueError(f"current_difficulties must be an array with at most {_MAX_BLOCKERS} items")
        if active_blocker_ids and not value:
            raise ValueError("current_difficulties must explain every active blocker before curation can finish")
        difficulties: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for index, raw in enumerate(value, start=1):
            if not isinstance(raw, dict):
                raise ValueError("current_difficulties items must be objects")
            difficulty_id = _clean_difficulty_id(
                raw.get("difficulty_id"), field=f"current_difficulties[{index}].difficulty_id"
            )
            blocker_id = _clean_id(raw.get("blocker_id"), field=f"current_difficulties[{index}].blocker_id")
            if difficulty_id in seen_ids:
                raise ValueError("current_difficulties must not contain duplicate difficulty_id values")
            seen_ids.add(difficulty_id)
            gate_raw = raw.get("gate")
            if not isinstance(gate_raw, dict):
                raise ValueError(f"current_difficulties[{index}].gate must be an object")
            difficulties.append({
                "difficulty_id": difficulty_id,
                "statement": _clean_text(
                    raw.get("statement"), field=f"current_difficulties[{index}].statement", max_length=2000
                ),
                "gate": {
                    "direction_id": _clean_id(
                        gate_raw.get("direction_id"), field=f"current_difficulties[{index}].gate.direction_id"
                    ),
                    "gap_id": _clean_id(
                        gate_raw.get("gap_id"), field=f"current_difficulties[{index}].gate.gap_id"
                    ),
                },
                "blocker_id": blocker_id,
            })
        candidate = decision.get("repeated_avoided_obligation")
        if isinstance(candidate, dict) and candidate.get("statement"):
            candidate_gate = (
                str(candidate.get("direction_id") or "").strip(),
                str(candidate.get("gap_id") or "").strip(),
            )
            # The audit gate is stable runtime identity; its prose is evidence that a
            # curator may accurately condense, normalize notation for, or translate.
            # Exact string equality made otherwise valid curation fail on harmless
            # formatting differences such as n₀ versus n0.
            if not any(
                (item["gate"]["direction_id"], item["gate"]["gap_id"]) == candidate_gate
                for item in difficulties
            ):
                raise ValueError(
                    "current_difficulties must include the process-audit blocker candidate gate "
                    "{direction_id,gap_id}, or explicitly resolve it before curation"
                )
        return difficulties

    def _normalize_blocker_relations(
        self,
        value: Any,
        *,
        difficulties: list[dict[str, Any]],
        active_blocker_ids: list[str],
        known_blocker_ids: set[str],
    ) -> list[dict[str, str]]:
        if value is None:
            value = []
        if not isinstance(value, list) or len(value) > _MAX_BLOCKERS * _MAX_BLOCKERS:
            raise ValueError("blocker_relations must be a bounded array")
        if not difficulties and active_blocker_ids:
            raise ValueError("blocker_relations cannot omit active blockers when no current difficulties are supplied")
        difficulty_ids = {item["difficulty_id"] for item in difficulties}
        difficulty_by_id = {item["difficulty_id"]: item for item in difficulties}
        expected_pairs = {(difficulty_id, blocker_id) for difficulty_id in difficulty_ids for blocker_id in active_blocker_ids}
        relations: list[dict[str, str]] = []
        seen_pairs: set[tuple[str, str]] = set()
        for index, raw in enumerate(value, start=1):
            if not isinstance(raw, dict):
                raise ValueError("blocker_relations items must be objects")
            difficulty_id = _clean_id(raw.get("difficulty_id"), field=f"blocker_relations[{index}].difficulty_id")
            active_blocker_id = _clean_id(
                raw.get("active_blocker_id"), field=f"blocker_relations[{index}].active_blocker_id"
            )
            relation = str(raw.get("relation") or "").strip()
            if relation not in _RELATIONS:
                raise ValueError(f"blocker_relations[{index}].relation must be one of {sorted(_RELATIONS)}")
            if difficulty_id not in difficulty_ids or active_blocker_id not in active_blocker_ids:
                raise ValueError("blocker_relations must compare a current difficulty with an active blocker at checkpoint start")
            pair = (difficulty_id, active_blocker_id)
            if pair in seen_pairs:
                raise ValueError("blocker_relations must contain each difficulty/blocker pair at most once")
            seen_pairs.add(pair)
            raw_replacement = raw.get("replacement_blocker_id")
            raw_canonical = raw.get("canonical_blocker_id")
            blocker_id = _clean_id(
                raw_canonical if relation == "same" else (
                    raw_replacement if relation == "superseded" else difficulty_by_id[difficulty_id]["blocker_id"]
                ),
                field=(
                    f"blocker_relations[{index}].canonical_blocker_id" if relation == "same" else
                    f"blocker_relations[{index}].replacement_blocker_id" if relation == "superseded" else
                    "difficulty blocker_id"
                ),
            )
            if relation in {"same", "superseded"} and blocker_id not in known_blocker_ids:
                raise ValueError(f"{relation} relation must reference an existing active blocker or current blocker payload")
            relations.append({
                "difficulty_id": difficulty_id,
                "active_blocker_id": active_blocker_id,
                "relation": relation,
                "blocker_id": blocker_id,
            })
        if seen_pairs != expected_pairs:
            missing = sorted(expected_pairs - seen_pairs)
            raise ValueError(f"blocker_relations must classify every current difficulty against every active blocker: missing {missing}")
        return relations

    def _validate_identity_decisions(
        self,
        *,
        difficulties: list[dict[str, Any]],
        relations: list[dict[str, str]],
        normalized_blockers: list[dict[str, Any]],
        resolved_ids: list[str],
    ) -> None:
        incoming_ids = [item["blocker_id"] for item in normalized_blockers]
        if len(set(incoming_ids)) != len(incoming_ids):
            raise ValueError("blockers must not contain duplicate blocker_id values")
        difficulty_blocker_ids = {item["blocker_id"] for item in difficulties}
        if set(incoming_ids) != difficulty_blocker_ids:
            raise ValueError("each current difficulty must have exactly one persistent blocker payload")
        difficulty_by_id = {item["difficulty_id"]: item for item in difficulties}
        same_by_difficulty: dict[str, set[str]] = {}
        for relation in relations:
            if relation["relation"] == "same":
                if relation["blocker_id"] != difficulty_by_id[relation["difficulty_id"]]["blocker_id"]:
                    raise ValueError("same relation canonical_blocker_id must match the current difficulty blocker_id")
                same_by_difficulty.setdefault(relation["difficulty_id"], set()).add(relation["active_blocker_id"])
            if relation["relation"] == "superseded" and relation["active_blocker_id"] in resolved_ids:
                raise ValueError("a blocker cannot be both resolved and superseded in one curation")
        for difficulty_id, same_ids in same_by_difficulty.items():
            if difficulty_by_id[difficulty_id]["blocker_id"] not in same_ids:
                raise ValueError("same relation must reuse one of the existing active blocker IDs as canonical")

    def _combine_existing_records(self, records: list[Any], *, canonical_id: str) -> dict[str, Any] | None:
        valid = [deepcopy(record) for record in records if isinstance(record, dict)]
        if not valid:
            return None
        combined = valid[0]
        combined["blocker_id"] = canonical_id
        for record in valid[1:]:
            combined = self._merge_active_record(combined, {
                "blocker_id": canonical_id,
                "statement": str(record.get("statement") or combined.get("statement") or "Merged blocker"),
                "gate": record.get("gate") or combined.get("gate") or {},
                "approaches": record.get("approaches") or [],
            }, checkpoint_id="legacy-merge")
        return combined

    def _checkpoint_decision(self, checkpoint_id: str) -> dict[str, Any]:
        path = self.audits_dir / checkpoint_id / "decision.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise ValueError(f"checkpoint decision is unavailable: {checkpoint_id}") from None
        if not isinstance(value, dict) or value.get("status") not in {"completed", "invalid_audit"}:
            raise ValueError(f"checkpoint is not ready for curation: {checkpoint_id}")
        return value

    def _normalize_blockers(self, value: Any, *, watermark: int) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > _MAX_BLOCKERS:
            raise ValueError(f"blockers must be an array with at most {_MAX_BLOCKERS} items")
        available = self._outcomes_through(watermark)
        normalized: list[dict[str, Any]] = []
        all_sequences: set[int] = set()
        for index, raw in enumerate(value, start=1):
            if not isinstance(raw, dict):
                raise ValueError("blockers items must be objects")
            blocker_id = _clean_id(raw.get("blocker_id"), field=f"blockers[{index}].blocker_id")
            statement = _clean_text(raw.get("statement"), field=f"blockers[{index}].statement", max_length=2000)
            gate_raw = raw.get("gate")
            if not isinstance(gate_raw, dict):
                raise ValueError(f"blockers[{index}].gate must be an object")
            gate = {
                "direction_id": _clean_id(gate_raw.get("direction_id"), field=f"blockers[{index}].gate.direction_id"),
                "gap_id": _clean_id(gate_raw.get("gap_id"), field=f"blockers[{index}].gate.gap_id"),
            }
            approaches_raw = raw.get("approaches")
            if not isinstance(approaches_raw, list) or not approaches_raw or len(approaches_raw) > _MAX_APPROACHES:
                raise ValueError(f"blockers[{index}].approaches must be a non-empty array with at most {_MAX_APPROACHES} items")
            approaches: list[dict[str, Any]] = []
            record_sequences: set[int] = set()
            for approach_index, approach_raw in enumerate(approaches_raw, start=1):
                if not isinstance(approach_raw, dict):
                    raise ValueError("blocker approaches must be objects")
                direction_id = _clean_id(
                    approach_raw.get("direction_id"), field=f"blockers[{index}].approaches[{approach_index}].direction_id"
                )
                gap_id = _clean_id(
                    approach_raw.get("gap_id"), field=f"blockers[{index}].approaches[{approach_index}].gap_id"
                )
                method_id = _clean_id(
                    approach_raw.get("method_id"), field=f"blockers[{index}].approaches[{approach_index}].method_id"
                )
                description = _clean_text(
                    approach_raw.get("description"),
                    field=f"blockers[{index}].approaches[{approach_index}].description",
                    max_length=800,
                )
                sequences = self._normalize_sequences(
                    approach_raw.get("outcome_sequences"),
                    available=available,
                    expected=(direction_id, gap_id, method_id),
                    field=f"blockers[{index}].approaches[{approach_index}].outcome_sequences",
                )
                duplicate = record_sequences & set(sequences)
                if duplicate:
                    raise ValueError(f"an outcome sequence may occur only once per blocker: {sorted(duplicate)}")
                record_sequences.update(sequences)
                approaches.append({
                    "direction_id": direction_id,
                    "gap_id": gap_id,
                    "method_id": method_id,
                    "description": description,
                    "outcome_sequences": sequences,
                    "difficulty_handoffs": self._difficulty_handoffs_for_sequences(sequences, available),
                    "occurrence_count": len(sequences),
                })
            overlap = all_sequences & record_sequences
            if overlap:
                raise ValueError(f"an outcome sequence may belong to only one blocker identity: {sorted(overlap)}")
            all_sequences.update(record_sequences)
            if len(record_sequences) < 2:
                raise ValueError("a persistent repeated blocker requires at least two outcome sequences")
            normalized.append({
                "blocker_id": blocker_id,
                "statement": statement,
                "gate": gate,
                "approaches": approaches,
                "outcome_sequences": sorted(record_sequences),
                "occurrence_count": len(record_sequences),
            })
        return normalized

    def _normalize_resolved_ids(self, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > _MAX_BLOCKERS:
            raise ValueError(f"resolved_blocker_ids must be an array with at most {_MAX_BLOCKERS} items")
        ids = [_clean_id(item, field="resolved_blocker_ids item") for item in value]
        if len(set(ids)) != len(ids):
            raise ValueError("resolved_blocker_ids must not contain duplicates")
        return ids

    def _normalize_sequences(
        self,
        value: Any,
        *,
        available: dict[int, dict[str, Any]],
        expected: tuple[str, str, str],
        field: str,
    ) -> list[int]:
        if not isinstance(value, list) or not value or len(value) > 100:
            raise ValueError(f"{field} must be a non-empty array with at most 100 items")
        sequences: list[int] = []
        for raw in value:
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
                raise ValueError(f"{field} items must be positive integers")
            outcome = available.get(raw)
            if outcome is None:
                raise ValueError(f"{field} references unavailable outcome sequence: {raw}")
            actual = (
                str(outcome.get("direction_id") or "").strip(),
                str(outcome.get("gap_id") or "").strip(),
                str(outcome.get("method_id") or "").strip(),
            )
            if actual != expected:
                raise ValueError(f"{field} sequence {raw} does not match its direction/gap/method provenance")
            sequences.append(raw)
        if len(set(sequences)) != len(sequences):
            raise ValueError(f"{field} must not contain duplicates")
        return sorted(sequences)

    @staticmethod
    def _difficulty_handoffs_for_sequences(
        sequences: list[int],
        available: dict[int, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        handoffs: list[dict[str, Any]] = []
        for sequence in sequences:
            raw = available.get(sequence, {}).get("difficulty_handoff")
            if not isinstance(raw, dict):
                continue
            obligation = " ".join(str(raw.get("blocking_obligation") or "").split())
            if not obligation:
                continue
            handoffs.append({
                "sequence": sequence,
                "worker_id": str(raw.get("worker_id") or available.get(sequence, {}).get("worker_id") or ""),
                "disposition": str(raw.get("disposition") or "unknown"),
                "blocking_obligation": obligation[:2000],
                "last_verified_step": " ".join(str(raw.get("last_verified_step") or "").split())[:1600],
                "why_current_route_fails": " ".join(str(raw.get("why_current_route_fails") or "").split())[:1600],
                "suggested_attack": " ".join(str(raw.get("suggested_attack") or "").split())[:1600],
                "evidence_refs": [str(item)[:1000] for item in raw.get("evidence_refs") or [] if str(item).strip()][:8],
            })
        return handoffs

    def _outcomes_through(self, watermark: int) -> dict[int, dict[str, Any]]:
        outcomes: dict[int, dict[str, Any]] = {}
        try:
            lines = self.outcomes_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict):
                continue
            sequence = value.get("sequence")
            if isinstance(sequence, int) and 0 < sequence <= watermark:
                outcomes[sequence] = value
        return outcomes

    def _merge_active_record(self, existing: Any, incoming: dict[str, Any], *, checkpoint_id: str) -> dict[str, Any]:
        prior_approaches = existing.get("approaches") if isinstance(existing, dict) else []
        by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
        for raw in prior_approaches if isinstance(prior_approaches, list) else []:
            if not isinstance(raw, dict):
                continue
            key = (str(raw.get("direction_id") or ""), str(raw.get("gap_id") or ""), str(raw.get("method_id") or ""))
            by_key[key] = deepcopy(raw)
        for incoming_approach in incoming["approaches"]:
            key = (
                incoming_approach["direction_id"],
                incoming_approach["gap_id"],
                incoming_approach["method_id"],
            )
            previous = by_key.get(key, {})
            sequences = sorted(set(previous.get("outcome_sequences") or []) | set(incoming_approach["outcome_sequences"]))
            handoffs_by_sequence = {
                int(item.get("sequence")): deepcopy(item)
                for item in previous.get("difficulty_handoffs") or []
                if isinstance(item, dict) and isinstance(item.get("sequence"), int)
            }
            for item in incoming_approach.get("difficulty_handoffs") or []:
                if isinstance(item, dict) and isinstance(item.get("sequence"), int):
                    handoffs_by_sequence[int(item["sequence"])] = deepcopy(item)
            by_key[key] = {
                **incoming_approach,
                "outcome_sequences": sequences,
                "difficulty_handoffs": [handoffs_by_sequence[sequence] for sequence in sequences if sequence in handoffs_by_sequence],
                "occurrence_count": len(sequences),
            }
        approaches = sorted(by_key.values(), key=lambda item: (item["direction_id"], item["gap_id"], item["method_id"]))
        sequences = sorted({sequence for item in approaches for sequence in item["outcome_sequences"]})
        source_checkpoints = set(existing.get("source_checkpoints") or []) if isinstance(existing, dict) else set()
        source_checkpoints.add(checkpoint_id)
        return {
            "blocker_id": incoming["blocker_id"],
            "statement": incoming["statement"],
            "gate": incoming["gate"],
            "status": "active",
            "approaches": approaches,
            "outcome_sequences": sequences,
            "occurrence_count": len(sequences),
            "source_checkpoints": sorted(source_checkpoints),
            "created_at": existing.get("created_at") if isinstance(existing, dict) else _now_iso(),
            "updated_at": _now_iso(),
        }

    def _save_locked(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp_path, self.path)


def register_curated_blocker_registry_tool(
    registry: ToolRegistry,
    *,
    blocker_registry: PersistentBlockerRegistry,
    checkpoint_id: str | None,
    replace: bool = False,
) -> None:
    """Give the curator a narrow write capability for the persistent control registry."""

    def handler(args: dict[str, Any]) -> ToolResult:
        try:
            if checkpoint_id is None:
                raise ValueError("CuratePersistentBlockers is available only during a portfolio checkpoint task")
            result = blocker_registry.record_curation(
                checkpoint_id=checkpoint_id,
                blockers=args.get("blockers"),
                resolved_blocker_ids=args.get("resolved_blocker_ids"),
                current_difficulties=args.get("current_difficulties"),
                blocker_relations=args.get("blocker_relations"),
            )
        except ValueError as exc:
            return ToolResult(json.dumps({"error": str(exc)}, ensure_ascii=False), is_error=True)
        return ToolResult(json.dumps(result, ensure_ascii=False))

    gate_schema = {
        "type": "object",
        "properties": {
            "direction_id": {"type": "string"},
            "gap_id": {"type": "string"},
        },
        "required": ["direction_id", "gap_id"],
    }
    current_difficulty_schema = {
        "type": "object",
        "properties": {
            "difficulty_id": {
                "type": "string",
                "description": "Semantic slug only; never use direction/gap here.",
            },
            "statement": {"type": "string"},
            "gate": gate_schema,
            "blocker_id": {"type": "string"},
        },
        "required": ["difficulty_id", "statement", "gate", "blocker_id"],
    }
    relation_schema = {
        "type": "object",
        "properties": {
            "difficulty_id": {"type": "string"},
            "active_blocker_id": {"type": "string"},
            "relation": {"type": "string", "enum": sorted(_RELATIONS)},
            "canonical_blocker_id": {"type": "string"},
            "replacement_blocker_id": {"type": "string"},
        },
        "required": ["difficulty_id", "active_blocker_id", "relation"],
    }
    approach_schema = {
        "type": "object",
        "properties": {
            "direction_id": {"type": "string"},
            "gap_id": {"type": "string"},
            "method_id": {"type": "string"},
            "description": {"type": "string"},
            "outcome_sequences": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["direction_id", "gap_id", "method_id", "description", "outcome_sequences"],
    }
    blocker_schema = {
        "type": "object",
        "properties": {
            "blocker_id": {"type": "string"},
            "statement": {"type": "string"},
            "gate": gate_schema,
            "approaches": {"type": "array", "items": approach_schema},
        },
        "required": ["blocker_id", "statement", "gate", "approaches"],
    }

    registry.register(
        name="CuratePersistentBlockers",
        description=(
            "Persist the curator's semantic grouping of repeated mathematical blockers for this audit checkpoint. "
            "Before choosing IDs, compare every current difficulty against every active historical blocker as same, distinct, "
            "unresolved, or superseded. `difficulty_id` is only a stable semantic slug (letters, digits, '.', '_' and '-'); "
            "never use a direction/gap path such as 'direction/gap' there. Put the exact route identity in "
            "gate={direction_id,gap_id}. A same relation must reuse an old canonical blocker_id; if two old blockers are the same, "
            "declare both same with one canonical ID so runtime merges them. Counts are derived from outcome sequences, never typed. "
            "Only this tool may write the persistent blocker control registry."
        ),
        parameters={
            "type": "object",
            "properties": {
                "current_difficulties": {
                    "type": "array",
                    "items": current_difficulty_schema,
                    "description": (
                        "Every material current difficulty, including the audit candidate when present. Each item requires "
                        "difficulty_id, statement, gate={direction_id,gap_id}, and blocker_id (the persistent canonical identity). "
                        "difficulty_id must be a stable semantic slug using only letters, digits, '.', '_' and '-' (for example "
                        "'audit-fooling-universal-lis-lds-bound'); never put 'direction/gap' in difficulty_id."
                    ),
                },
                "blocker_relations": {
                    "type": "array",
                    "items": relation_schema,
                    "description": (
                        "Complete matrix over current_difficulties × active blockers at checkpoint start. Each item: "
                        "{difficulty_id,active_blocker_id,relation=same|distinct|unresolved|superseded,canonical_blocker_id for same, "
                        "replacement_blocker_id for superseded}."
                    ),
                },
                "blockers": {
                    "type": "array",
                    "items": blocker_schema,
                    "description": (
                        "One active payload for each current difficulty's blocker_id: blocker_id, statement, gate={direction_id,gap_id}, "
                        "approaches=[{direction_id,gap_id,method_id,description,outcome_sequences:[positive audit sequence ids]}]."
                    ),
                },
                "resolved_blocker_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Existing blocker identities now resolved; omit when none.",
                },
            },
            "required": ["current_difficulties", "blocker_relations", "blockers"],
        },
        handler=handler,
        replace=replace,
    )
