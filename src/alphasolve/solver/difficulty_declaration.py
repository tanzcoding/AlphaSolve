from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from alphasolve.agent.tools import ToolRegistry, ToolResult


_MAX_FIELD_LENGTH = 4000
_HANDOFF_SCHEMA_VERSION = 6
_NON_MATHEMATICAL_FAILURES = {"generator_protocol_failure", "execution_failed"}
_RECORDING_ROLES = {"generator", "reviser", "reasoning_subagent"}


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
    review_file: Path | None,
    proposition_file: Path | None,
    verified_file: Path | None,
) -> tuple[Path | None, dict[str, Any] | None]:
    """Package worker-local obstacle records for later curator review.

    A reasoning subagent's task-specific observation is retained as evidence.  When
    an outer generator or reviser has recorded a conclusion, that conclusion is the
    concise worker-level obstacle; otherwise the latest reasoning observation is used.
    """
    declaration = load_difficulty_declaration(declaration_path)
    if declaration is None or str(failure_kind or "") in _NON_MATHEMATICAL_FAILURES:
        return None, None
    records = _records_from_state(declaration)
    if not records:
        return None, None
    source = next((record for record in reversed(records) if record["role"] != "reasoning_subagent"), records[-1])
    obstacle = _clean(source.get("obstacle"))
    if not obstacle:
        return None, None
    context = declaration.get("runtime_context")
    if not isinstance(context, dict):
        context = {}
    evidence_refs = [
        str(path)
        for path in (declaration_path, review_file, proposition_file, verified_file)
        if path is not None
    ]
    handoff: dict[str, Any] = {
        "schema_version": _HANDOFF_SCHEMA_VERSION,
        "handoff_id": _handoff_id(worker_id),
        "worker_id": worker_id,
        "difficulty_id": difficulty_id or context.get("difficulty_id"),
        "method_id": method_id or context.get("method_id"),
        "assigned_target": _clean(context.get("assigned_target")),
        "execution_status": str(execution_status or "unknown"),
        "failure_kind": failure_kind,
        "obstacle": obstacle,
        "obstacle_source_role": source["role"],
        "obstacle_records": records,
        "evidence_refs": evidence_refs,
        "source_confidence": "worker_local_pending_curator_curation",
        "requires_checkpoint_curation": True,
    }
    path = difficulty_handoff_path(declaration_path)
    _atomic_write_json(path, handoff)
    return path, handoff


def register_difficulty_declaration_tool(
    registry: ToolRegistry,
    *,
    declaration_path: Path,
    role: str,
    record_context: dict[str, Any] | None = None,
) -> None:
    """Register the sole worker-local obstacle writer for one role.

    ``record_context`` is runtime-owned side information.  In particular, a
    reasoning subagent receives its delegated task and session identifier without
    having to repeat or choose either field in the tool call.
    """
    if role not in _RECORDING_ROLES:
        raise ValueError(f"unsupported difficulty declaration role: {role}")
    automatic_context = _record_context(record_context)

    def handler(args: dict[str, Any]) -> ToolResult:
        try:
            obstacle = _required_text(args, "obstacle")
            declaration_path.parent.mkdir(parents=True, exist_ok=True)
            runtime_context = _load_runtime_context(declaration_path)
            state = load_difficulty_declaration(declaration_path) or {
                "schema_version": _HANDOFF_SCHEMA_VERSION,
                "runtime_context": runtime_context,
                "records": [],
            }
            records = _records_from_state(state)
            record = {
                "index": len(records) + 1,
                "role": role,
                "obstacle": obstacle,
                **automatic_context,
            }
            records.append(record)
            state["schema_version"] = _HANDOFF_SCHEMA_VERSION
            state["runtime_context"] = runtime_context or state.get("runtime_context") or {}
            state["records"] = records
            state.pop("generator", None)
            state.pop("revisions", None)
            existing = declaration_path.read_text(encoding="utf-8") if declaration_path.exists() else "# Difficulty Records\n"
            declaration_path.write_text(
                existing.rstrip() + "\n\n" + _render_record(record) + "\n",
                encoding="utf-8",
            )
            _atomic_write_json(difficulty_json_path(declaration_path), state)
            return ToolResult(json.dumps({
                "recorded": True,
                "record_index": record["index"],
                "role": role,
                "path": str(declaration_path),
                "json_path": str(difficulty_json_path(declaration_path)),
            }, ensure_ascii=False))
        except ValueError as exc:
            return ToolResult(json.dumps({"error": str(exc)}, ensure_ascii=False), is_error=True)

    registry.register(
        name="RecordDifficulty",
        description=(
            "Record a concrete obstacle only when the assigned task cannot be completed and must be weakened or stopped. "
            "State the missing step, condition, construction, or repair and why it blocks this task. "
            "This is worker-local evidence for curator review, never a canonical DAG edit or research plan. "
            "Role, assigned target, artifacts, and (for reasoning subagents) delegated task context are attached automatically."
        ),
        parameters={
            "type": "object",
            "properties": {
                "obstacle": {
                    "type": "string",
                    "description": "Concrete current obstacle: the missing step or condition, and why it prevents completing the assigned task.",
                },
            },
            "required": ["obstacle"],
        },
        handler=handler,
    )


def _records_from_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    raw_records = state.get("records")
    if isinstance(raw_records, list):
        records: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_records, 1):
            if not isinstance(raw, dict):
                continue
            obstacle = _clean(raw.get("obstacle"))
            role = str(raw.get("role") or "").strip()
            if not obstacle or role not in _RECORDING_ROLES:
                continue
            record = {
                "index": int(raw.get("index") or index),
                "role": role,
                "obstacle": obstacle,
            }
            for key in ("delegated_description", "delegated_task", "subagent_session_id"):
                value = _clean(raw.get(key))
                if value:
                    record[key] = value
            records.append(record)
        return records[-32:]

    # Read old declarations so upgrades do not discard existing worker evidence.
    legacy: list[dict[str, Any]] = []
    generator = state.get("generator")
    if isinstance(generator, dict) and _clean(generator.get("obstacle")):
        legacy.append({"index": 1, "role": "generator", "obstacle": _clean(generator.get("obstacle"))})
    revisions = state.get("revisions")
    if isinstance(revisions, list):
        for raw in revisions:
            if isinstance(raw, dict) and _clean(raw.get("obstacle")):
                legacy.append({"index": len(legacy) + 1, "role": "reviser", "obstacle": _clean(raw.get("obstacle"))})
    return legacy[-32:]


def _record_context(value: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key in ("delegated_description", "delegated_task", "subagent_session_id"):
        text = _clean(value.get(key))
        if text:
            result[key] = text
    return result


def _load_runtime_context(declaration_path: Path) -> dict[str, Any]:
    try:
        value = json.loads((declaration_path.parent / "research_target.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    assigned_target = str(value.get("pinned_target") or value.get("difficulty_statement") or value.get("hint") or "").strip()
    return {
        "worker_id": str(value.get("worker_id") or "").strip() or None,
        "difficulty_id": str(value.get("difficulty_id") or "").strip() or None,
        "method_id": str(value.get("method_id") or "").strip() or None,
        "assigned_target": assigned_target[:_MAX_FIELD_LENGTH],
    }


def _clean(value: Any, *, limit: int = _MAX_FIELD_LENGTH) -> str:
    return " ".join(str(value or "").split())[:limit]


def _handoff_id(worker_id: str) -> str:
    """Return the runtime-owned stable identity of a worker obstacle record."""
    normalized = "".join(char if char.isalnum() or char in "._-" else "-" for char in str(worker_id or "unknown"))
    return f"handoff-{normalized[:64].strip('.-_') or 'unknown'}"


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _required_text(args: dict[str, Any], field: str) -> str:
    value = _clean(args.get(field))
    if not value:
        raise ValueError(f"{field} must not be empty")
    return value


def _render_record(record: dict[str, Any]) -> str:
    lines = [
        f"### Record {record['index']} — {record['role']}",
        "",
        "## Obstacle",
        "",
        record["obstacle"],
    ]
    if record.get("delegated_description"):
        lines.extend(["", "## Delegated Task", "", record["delegated_description"]])
    if record.get("delegated_task"):
        lines.extend(["", "## Task Context", "", record["delegated_task"]])
    return "\n".join(lines)
