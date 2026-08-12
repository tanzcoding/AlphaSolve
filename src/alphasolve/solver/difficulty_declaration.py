from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from alphasolve.agent.tools import ToolRegistry, ToolResult


_MAX_FIELD_LENGTH = 4000
_TARGET_STATUSES = {"YES", "NO", "PARTIAL"}
_REVISION_OUTCOMES = {"repaired", "weakened", "blocked", "refuted"}
_RELATIONS_TO_PARENT = {"prerequisite", "alternative", "weakened_target", "method_blocked", "refutes"}
_RESOLUTION_POLICIES = {"all_of", "any_of", "manual"}
_HANDOFF_SCHEMA_VERSION = 3
_NON_MATHEMATICAL_FAILURES = {"generator_protocol_failure", "execution_failed"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")


def difficulty_json_path(declaration_path: Path) -> Path:
    return declaration_path.with_name("difficulty_declaration.json")


def difficulty_handoff_path(declaration_path: Path) -> Path:
    return declaration_path.with_name("difficulty_handoff.json")


def load_difficulty_declaration(declaration_path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(difficulty_json_path(declaration_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def load_difficulty_handoff(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def materialize_difficulty_handoff(
    *,
    declaration_path: Path,
    worker_id: str,
    difficulty_id: str | None = None,
    method_id: str | None,
    execution_status: str,
    failure_kind: str | None,
    result_summary_file: Path | None,
    review_file: Path | None,
    proposition_file: Path | None,
    verified_file: Path | None,
    difficulty_assessment_file: Path | None = None,
) -> tuple[Path | None, dict[str, Any] | None]:
    """Package one worker-local difficulty proposal for checkpoint curation.

    This never assigns a canonical identity.  The worker only reports a proposed
    child relationship to its assigned canonical difficulty; the curator reconciles
    that proposal into the persistent difficulty DAG.
    """
    declaration = load_difficulty_declaration(declaration_path)
    if declaration is None or str(failure_kind or "") in _NON_MATHEMATICAL_FAILURES:
        return None, None
    revisions = declaration.get("revisions")
    latest = revisions[-1] if isinstance(revisions, list) and revisions and isinstance(revisions[-1], dict) else None
    source = latest or declaration.get("generator")
    if not isinstance(source, dict):
        return None, None
    target_status = str(source.get("target_status") or declaration.get("target_status") or "").strip()
    revision_outcome = str(source.get("revision_outcome") or "").strip() or None
    if target_status == "YES" and revision_outcome in {None, "repaired"}:
        disposition = "resolved"
    elif revision_outcome == "repaired":
        disposition = "partially_addressed"
    elif revision_outcome in {"weakened", "blocked", "refuted"}:
        disposition = "replaced" if revision_outcome == "refuted" else "active_candidate"
    else:
        disposition = "active_candidate"

    def clean(value: Any, *, limit: int = _MAX_FIELD_LENGTH) -> str:
        return " ".join(str(value or "").split())[:limit]

    context = declaration.get("runtime_context")
    if not isinstance(context, dict):
        context = {}
    source_difficulty_id = clean(
        source.get("source_difficulty_id") or context.get("source_difficulty_id") or f"worker-{worker_id}-difficulty",
        limit=80,
    )
    evidence_refs = [
        str(path)
        for path in (declaration_path, result_summary_file, difficulty_assessment_file, review_file, proposition_file, verified_file)
        if path is not None
    ]
    handoff: dict[str, Any] = {
        "schema_version": _HANDOFF_SCHEMA_VERSION,
        "source_difficulty_id": source_difficulty_id,
        "worker_id": worker_id,
        "difficulty_id": difficulty_id or context.get("difficulty_id"),
        "parent_difficulty_id": source.get("parent_difficulty_id") or context.get("difficulty_id"),
        "relation_to_parent": source.get("relation_to_parent") or "prerequisite",
        "parent_resolution_policy": source.get("parent_resolution_policy") or "manual",
        "method_id": method_id or context.get("method_id"),
        "assigned_target": clean(context.get("assigned_target")),
        "child_delta": clean(source.get("child_delta")),
        "execution_status": str(execution_status or "unknown"),
        "failure_kind": failure_kind,
        "target_status": target_status or "UNKNOWN",
        "revision_outcome": revision_outcome,
        "disposition": disposition,
        "blocking_obligation": clean(source.get("difficulty")),
        "last_verified_step": clean(source.get("last_verified_step")),
        "why_current_route_fails": clean(source.get("why_hard")),
        "suggested_attack": clean(source.get("suggested_attack")),
        "dead_ends": clean(source.get("dead_ends")),
        "evidence_refs": evidence_refs,
        "source_confidence": "worker_reconciled_pending_curator_curation",
        "requires_checkpoint_curation": disposition == "active_candidate",
    }
    path = difficulty_handoff_path(declaration_path)
    _atomic_write_json(path, handoff)
    return path, handoff


def register_difficulty_declaration_tool(
    registry: ToolRegistry,
    *,
    declaration_path: Path,
    role: str,
) -> None:
    if role not in {"generator", "reviser"}:
        raise ValueError(f"unsupported difficulty declaration role: {role}")

    def handler(args: dict[str, Any]) -> ToolResult:
        try:
            target_status = _required_choice(args, "target_status", _TARGET_STATUSES)
            difficulty = _required_text(args, "difficulty")
            why_hard = _required_text(args, "why_hard")
            suggested_attack = _required_text(args, "suggested_attack")
            last_verified_step = _optional_text(args, "last_verified_step")
            child_delta = _optional_text(args, "child_delta")
            dead_ends = _optional_text(args, "dead_ends")
            relation_to_parent = _optional_choice(args, "relation_to_parent", _RELATIONS_TO_PARENT) or "prerequisite"
            parent_resolution_policy = _optional_choice(args, "parent_resolution_policy", _RESOLUTION_POLICIES) or "manual"
            revision_outcome = _optional_text(args, "revision_outcome") or "blocked"
            if role == "reviser" and revision_outcome not in _REVISION_OUTCOMES:
                raise ValueError("revision_outcome must be one of " + ", ".join(sorted(_REVISION_OUTCOMES)))
            declaration_path.parent.mkdir(parents=True, exist_ok=True)
            context = _load_runtime_context(declaration_path)
            source_difficulty_id = _safe_source_id(
                args.get("source_difficulty_id") or context.get("source_difficulty_id") or f"worker-{context.get('worker_id') or 'unknown'}-difficulty"
            )
            _validate_targeted_child_handoff(
                context=context,
                source_difficulty_id=source_difficulty_id,
                difficulty=difficulty,
                last_verified_step=last_verified_step,
                child_delta=child_delta,
            )
            record = {
                "source_difficulty_id": source_difficulty_id,
                "parent_difficulty_id": context.get("difficulty_id"),
                "relation_to_parent": relation_to_parent,
                "parent_resolution_policy": parent_resolution_policy,
                "target_status": target_status,
                "difficulty": difficulty,
                "child_delta": child_delta,
                "last_verified_step": last_verified_step,
                "why_hard": why_hard,
                "suggested_attack": suggested_attack,
                "dead_ends": dead_ends,
            }
            state = load_difficulty_declaration(declaration_path) or {
                "schema_version": _HANDOFF_SCHEMA_VERSION,
                "runtime_context": context,
                "generator": None,
                "revisions": [],
            }
            state["schema_version"] = _HANDOFF_SCHEMA_VERSION
            state["runtime_context"] = context or state.get("runtime_context") or {}
            existing_source = (state.get("generator") or {}).get("source_difficulty_id")
            if existing_source and existing_source != source_difficulty_id:
                raise ValueError("source_difficulty_id must remain stable across generator and reviser records")
            if role == "generator":
                if declaration_path.exists():
                    raise ValueError("generator difficulty declaration already exists; preserve the original declaration for later comparison")
                declaration_path.write_text(_generator_declaration(**record), encoding="utf-8")
                state["generator"] = record
                record_kind = "generator_initial"
            else:
                existing = declaration_path.read_text(encoding="utf-8") if declaration_path.exists() else "# Difficulty Declaration\n\n_No generator declaration was recorded before revision._\n"
                update_number = existing.count("### Reviser Update ") + 1
                declaration_path.write_text(
                    existing.rstrip() + "\n\n" + _reviser_update(
                        update_number=update_number,
                        revision_outcome=revision_outcome,
                        **record,
                    ),
                    encoding="utf-8",
                )
                state.setdefault("revisions", []).append({**record, "revision_outcome": revision_outcome, "update_number": update_number})
                record_kind = "reviser_update"
            _atomic_write_json(difficulty_json_path(declaration_path), state)
            return ToolResult(json.dumps({
                "recorded": True,
                "record_kind": record_kind,
                "source_difficulty_id": source_difficulty_id,
                "parent_difficulty_id": context.get("difficulty_id"),
                "path": str(declaration_path),
                "json_path": str(difficulty_json_path(declaration_path)),
            }, ensure_ascii=False))
        except ValueError as exc:
            return ToolResult(json.dumps({"error": str(exc)}, ensure_ascii=False), is_error=True)

    registry.register(
        name="RecordDifficulty",
        description=(
            "Record a precise worker-local difficulty whenever the assigned target is weakened, blocked, or refuted. "
            "This is a proposal for the curator-owned recursive difficulty DAG, not a canonical edit. State the smallest "
            "unresolved obligation, the last verified step, why the attempted argument fails, and a concrete attack. For an "
            "assigned canonical difficulty, the proposed child must be strictly smaller than the assigned target: supply the "
            "verified boundary and the exact remaining delta; do not restate the parent or create a self-parenting record. "
            "Classify the proposed child as prerequisite, alternative, weakened_target, method_blocked, or refutes, and state "
            "how the parent would combine its children."
        ),
        parameters={
            "type": "object",
            "properties": {
                "source_difficulty_id": {"type": "string", "description": "Optional stable local slug; defaults to this worker's generated source id."},
                "target_status": {"type": "string", "enum": sorted(_TARGET_STATUSES)},
                "revision_outcome": {"type": "string", "enum": sorted(_REVISION_OUTCOMES)},
                "difficulty": {"type": "string", "description": "Exact smallest unresolved mathematical obligation."},
                "child_delta": {"type": "string", "description": "For a targeted worker, explain why this is strictly smaller than the parent and what inference remains."},
                "relation_to_parent": {"type": "string", "enum": sorted(_RELATIONS_TO_PARENT)},
                "parent_resolution_policy": {"type": "string", "enum": sorted(_RESOLUTION_POLICIES)},
                "last_verified_step": {"type": "string"},
                "why_hard": {"type": "string"},
                "suggested_attack": {"type": "string"},
                "dead_ends": {"type": "string"},
            },
            "required": ["target_status", "difficulty", "why_hard", "suggested_attack"],
        },
        handler=handler,
    )


def _validate_targeted_child_handoff(
    *,
    context: dict[str, Any],
    source_difficulty_id: str,
    difficulty: str,
    last_verified_step: str,
    child_delta: str,
) -> None:
    parent_id = str(context.get("difficulty_id") or "").strip()
    if not parent_id:
        return
    if source_difficulty_id == parent_id:
        raise ValueError("source_difficulty_id must differ from the assigned parent difficulty_id")
    if not last_verified_step:
        raise ValueError("last_verified_step is required for a targeted child difficulty")
    if not child_delta:
        raise ValueError("child_delta is required for a targeted child difficulty")
    assigned_target = str(context.get("assigned_target") or "").strip()
    if _is_restatement_of_assigned_target(difficulty, assigned_target):
        raise ValueError(
            "difficulty restates the assigned parent target; record a strictly smaller remaining obligation instead"
        )


def _is_restatement_of_assigned_target(candidate: str, assigned_target: str) -> bool:
    normalized_candidate = _normalize_obligation(candidate)
    normalized_target = _normalize_obligation(assigned_target)
    if not normalized_candidate or not normalized_target:
        return False
    if normalized_candidate == normalized_target:
        return True
    candidate_words = set(normalized_candidate.split())
    target_words = set(normalized_target.split())
    if len(candidate_words) < 6 or len(target_words) < 6:
        return False
    overlap = len(candidate_words & target_words) / min(len(candidate_words), len(target_words))
    return overlap >= 0.9


def _normalize_obligation(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def _load_runtime_context(declaration_path: Path) -> dict[str, Any]:
    try:
        value = json.loads((declaration_path.parent / "research_target.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    assigned_target = str(value.get("pinned_target") or value.get("difficulty_statement") or value.get("hint") or "").strip()
    worker_id = str(value.get("worker_id") or "").strip()
    return {
        "worker_id": worker_id,
        "difficulty_id": str(value.get("difficulty_id") or "").strip() or None,
        "source_difficulty_id": _safe_source_id(f"worker-{worker_id}-difficulty") if worker_id else None,
        "method_id": str(value.get("method_id") or "").strip() or None,
        "assigned_target": assigned_target[:_MAX_FIELD_LENGTH],
    }


def _safe_source_id(value: Any) -> str:
    text = str(value or "").strip()
    if not _ID.fullmatch(text):
        raise ValueError("source_difficulty_id must be a stable slug")
    return text


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _required_choice(args: dict[str, Any], field: str, choices: set[str]) -> str:
    value = str(args.get(field) or "").strip()
    if value not in choices:
        raise ValueError(f"{field} must be one of {sorted(choices)}")
    return value


def _optional_choice(args: dict[str, Any], field: str, choices: set[str]) -> str:
    value = str(args.get(field) or "").strip()
    if value and value not in choices:
        raise ValueError(f"{field} must be one of {sorted(choices)}")
    return value


def _required_text(args: dict[str, Any], field: str) -> str:
    value = _optional_text(args, field)
    if not value:
        raise ValueError(f"{field} must not be empty")
    return value


def _optional_text(args: dict[str, Any], field: str) -> str:
    value = str(args.get(field) or "").strip()
    if len(value) > _MAX_FIELD_LENGTH:
        raise ValueError(f"{field} exceeds {_MAX_FIELD_LENGTH} characters")
    return value


def _generator_declaration(**record: Any) -> str:
    return "\n".join([
        "# Difficulty Declaration",
        "",
        "## Generator Declaration",
        "",
        f"- Source difficulty id: {record['source_difficulty_id']}",
        f"- Parent difficulty id: {record.get('parent_difficulty_id') or '(root candidate)'}",
        f"- Relation to parent: {record['relation_to_parent']}",
        f"- Parent resolution policy: {record['parent_resolution_policy']}",
        f"- Target status: {record['target_status']}",
        "",
        "## Exact Difficulty",
        record["difficulty"],
        "",
        "## Strict Child Delta",
        record["child_delta"] or "Not supplied.",
        "",
        "## Last Verified Step",
        record["last_verified_step"] or "Not supplied.",
        "",
        "## Why Current Argument Fails",
        record["why_hard"],
        "",
        "## Suggested Attack",
        record["suggested_attack"],
        "",
        "## Dead Ends",
        record["dead_ends"] or "None recorded.",
        "",
    ])


def _reviser_update(*, update_number: int, revision_outcome: str, **record: Any) -> str:
    return "\n".join([
        f"### Reviser Update {update_number}",
        "",
        f"- Revision outcome: {revision_outcome}",
        f"- Source difficulty id: {record['source_difficulty_id']}",
        f"- Parent difficulty id: {record.get('parent_difficulty_id') or '(root candidate)'}",
        f"- Relation to parent: {record['relation_to_parent']}",
        f"- Parent resolution policy: {record['parent_resolution_policy']}",
        f"- Target status: {record['target_status']}",
        "",
        "**Exact Difficulty:** " + record["difficulty"],
        "",
        "**Strict Child Delta:** " + (record["child_delta"] or "Not supplied."),
        "",
        "**Last Verified Step:** " + (record["last_verified_step"] or "Not supplied."),
        "",
        "**Why Current Argument Fails:** " + record["why_hard"],
        "",
        "**Suggested Attack:** " + record["suggested_attack"],
        "",
        "**Dead Ends:** " + (record["dead_ends"] or "None recorded."),
    ])
