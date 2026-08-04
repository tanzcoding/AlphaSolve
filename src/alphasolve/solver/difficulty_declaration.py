from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from alphasolve.agent.tools import ToolRegistry, ToolResult


_MAX_FIELD_LENGTH = 4000
_TARGET_STATUSES = {"YES", "NO", "PARTIAL"}
_REVISION_OUTCOMES = {"repaired", "weakened", "blocked", "refuted"}
_HANDOFF_SCHEMA_VERSION = 1
_NON_MATHEMATICAL_FAILURES = {"generator_protocol_failure", "execution_failed"}


def difficulty_json_path(declaration_path: Path) -> Path:
    """Return the structured companion path for a human-readable declaration."""
    return declaration_path.with_name("difficulty_declaration.json")


def difficulty_handoff_path(declaration_path: Path) -> Path:
    """Return the worker-final, runtime-consumable difficulty handoff path."""
    return declaration_path.with_name("difficulty_handoff.json")


def load_difficulty_declaration(declaration_path: Path) -> dict[str, Any] | None:
    """Read a declaration sidecar without making worker completion depend on it."""
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
    direction_id: str | None,
    gap_id: str | None,
    method_id: str | None,
    execution_status: str,
    failure_kind: str | None,
    result_summary_file: Path | None,
    review_file: Path | None,
    proposition_file: Path | None,
    verified_file: Path | None,
) -> tuple[Path | None, dict[str, Any] | None]:
    """Create a structured handoff from the latest worker declaration.

    A declaration is an agent's local observation and may be stale after revision. This
    handoff deliberately does not promote it to a canonical blocker: it marks the item
    as a candidate for orchestrator/reviewer/curator comparison and preserves evidence
    paths needed to check the claim against the final trail.
    """
    declaration = load_difficulty_declaration(declaration_path)
    if declaration is None:
        return None, None
    if str(failure_kind or "") in _NON_MATHEMATICAL_FAILURES:
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

    runtime_context = declaration.get("runtime_context")
    if not isinstance(runtime_context, dict):
        runtime_context = {}
    evidence_refs = [
        str(path)
        for path in (declaration_path, result_summary_file, review_file, proposition_file, verified_file)
        if path is not None
    ]
    handoff: dict[str, Any] = {
        "schema_version": _HANDOFF_SCHEMA_VERSION,
        "worker_id": worker_id,
        "direction_id": direction_id or runtime_context.get("direction_id"),
        "gap_id": gap_id or runtime_context.get("gap_id"),
        "method_id": method_id or runtime_context.get("method_id"),
        "assigned_target": clean(runtime_context.get("assigned_target")),
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
        "source_confidence": "worker_reconciled_pending_portfolio_review",
        "requires_portfolio_comparison": disposition == "active_candidate",
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
    """Register the fixed-path blocker record tool for a generator or reviser."""
    if role not in {"generator", "reviser"}:
        raise ValueError(f"unsupported difficulty declaration role: {role}")

    def handler(args: dict[str, Any]) -> ToolResult:
        try:
            target_status = _required_choice(args, "target_status", _TARGET_STATUSES)
            difficulty = _required_text(args, "difficulty")
            why_hard = _required_text(args, "why_hard")
            suggested_attack = _required_text(args, "suggested_attack")
            last_verified_step = _optional_text(args, "last_verified_step")
            dead_ends = _optional_text(args, "dead_ends")
            revision_outcome = _optional_text(args, "revision_outcome") or "blocked"
            if role == "reviser" and revision_outcome not in _REVISION_OUTCOMES:
                raise ValueError(
                    "revision_outcome must be one of "
                    + ", ".join(sorted(_REVISION_OUTCOMES))
                )

            declaration_path.parent.mkdir(parents=True, exist_ok=True)
            context = _load_runtime_context(declaration_path)
            record = {
                "target_status": target_status,
                "difficulty": difficulty,
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
            state["runtime_context"] = context or state.get("runtime_context") or {}
            if role == "generator":
                if declaration_path.exists():
                    raise ValueError(
                        "generator difficulty declaration already exists; preserve the original declaration for later comparison"
                    )
                declaration_path.write_text(
                    _generator_declaration(**record),
                    encoding="utf-8",
                )
                state["generator"] = record
                record_kind = "generator_initial"
            else:
                if declaration_path.exists():
                    existing = declaration_path.read_text(encoding="utf-8")
                else:
                    existing = "# Difficulty Declaration\n\n_No generator declaration was recorded before revision._\n"
                update_number = existing.count("### Reviser Update ") + 1
                declaration_path.write_text(
                    existing.rstrip()
                    + "\n\n"
                    + _reviser_update(
                        update_number=update_number,
                        revision_outcome=revision_outcome,
                        **record,
                    ),
                    encoding="utf-8",
                )
                state.setdefault("revisions", []).append({
                    **record,
                    "revision_outcome": revision_outcome,
                    "update_number": update_number,
                })
                record_kind = "reviser_update"
            _atomic_write_json(difficulty_json_path(declaration_path), state)
            return ToolResult(
                json.dumps(
                    {
                        "recorded": True,
                        "record_kind": record_kind,
                        "path": str(declaration_path),
                        "json_path": str(difficulty_json_path(declaration_path)),
                    },
                    ensure_ascii=False,
                )
            )
        except ValueError as exc:
            return ToolResult(json.dumps({"error": str(exc)}, ensure_ascii=False), is_error=True)

    registry.register(
        name="RecordDifficulty",
        description=(
            "Record a precise mathematical blocker in the fixed worker-local `difficulty_declaration.md` "
            "and its structured JSON sidecar. Use this when the assigned target is only partially solved, weakened, "
            "blocked by a substantive gap, or refuted. Generator records the original declaration once; reviser appends "
            "an update without overwriting that original evidence. State the exact unresolved obligation, the last "
            "verified step, why the route fails, and a concrete next attack. This declaration is later reconciled against "
            "the final review before the orchestrator, research reviewer, and curator compare it with other attempts."
        ),
        parameters={
            "type": "object",
            "properties": {
                "target_status": {
                    "type": "string",
                    "enum": sorted(_TARGET_STATUSES),
                    "description": "Whether the originally assigned target was solved exactly: YES, NO, or PARTIAL.",
                },
                "revision_outcome": {
                    "type": "string",
                    "enum": sorted(_REVISION_OUTCOMES),
                    "description": "Reviser only: repaired, weakened, blocked, or refuted. Ignored for generator.",
                },
                "difficulty": {
                    "type": "string",
                    "description": "Exact mathematical claim, estimate, construction, or case distinction that remains unresolved or was avoided.",
                },
                "last_verified_step": {
                    "type": "string",
                    "description": "Optional strongest rigorous step already established before this difficulty; do not state an unverified inference.",
                },
                "why_hard": {
                    "type": "string",
                    "description": "Why current arguments do not resolve this exact difficulty, including failed attempts if known.",
                },
                "suggested_attack": {
                    "type": "string",
                    "description": "A concrete next attack entry point, or 'Requires new mathematical tools' if none is known.",
                },
                "dead_ends": {
                    "type": "string",
                    "description": "Optional definitive dead ends and their reasons; leave empty if none are known.",
                },
            },
            "required": ["target_status", "difficulty", "why_hard", "suggested_attack"],
        },
        handler=handler,
    )


def _load_runtime_context(declaration_path: Path) -> dict[str, Any]:
    target_path = declaration_path.parent / "research_target.json"
    try:
        value = json.loads(target_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    assigned_target = str(value.get("pinned_target") or value.get("hint") or "").strip()
    return {
        "worker_id": str(value.get("worker_id") or "").strip(),
        "direction_id": str(value.get("direction_id") or "").strip() or None,
        "gap_id": str(value.get("gap_id") or "").strip() or None,
        "method_id": str(value.get("method_id") or "").strip() or None,
        "assigned_target": assigned_target[:_MAX_FIELD_LENGTH],
    }


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _required_choice(args: dict[str, Any], field: str, choices: set[str]) -> str:
    value = str(args.get(field) or "").strip()
    if value not in choices:
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


def _generator_declaration(
    *,
    target_status: str,
    difficulty: str,
    last_verified_step: str,
    why_hard: str,
    suggested_attack: str,
    dead_ends: str,
) -> str:
    return "\n".join(
        [
            "# Difficulty Declaration",
            "",
            "## Generator Declaration",
            "",
            f"### Target Solved?\n{target_status}",
            "",
            f"### Difficulty Avoided\n{difficulty}",
            "",
            f"### Last Verified Step\n{last_verified_step or '_Not recorded._'}",
            "",
            f"### Why Is the Difficulty Hard?\n{why_hard}",
            "",
            f"### Suggested Attack Entry Point\n{suggested_attack}",
            "",
            f"### Dead Ends\n{dead_ends or '_None recorded._'}",
            "",
        ]
    )


def _reviser_update(
    *,
    update_number: int,
    target_status: str,
    revision_outcome: str,
    difficulty: str,
    last_verified_step: str,
    why_hard: str,
    suggested_attack: str,
    dead_ends: str,
) -> str:
    return "\n".join(
        [
            f"### Reviser Update {update_number}",
            "",
            f"- Outcome: {revision_outcome}",
            f"- Target Solved?: {target_status}",
            "",
            f"#### Difficulty Avoided or Remaining\n{difficulty}",
            "",
            f"#### Last Verified Step\n{last_verified_step or '_Not recorded._'}",
            "",
            f"#### Why It Remains Hard\n{why_hard}",
            "",
            f"#### Suggested Attack Entry Point\n{suggested_attack}",
            "",
            f"#### Dead Ends\n{dead_ends or '_None recorded._'}",
            "",
        ]
    )
