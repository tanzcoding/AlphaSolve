from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from alphasolve.agent.tools import ToolRegistry, ToolResult


_MAX_FIELD_LENGTH = 4000
_HANDOFF_SCHEMA_VERSION = 6
_NON_MATHEMATICAL_FAILURES = {"generator_protocol_failure", "execution_failed"}
_RECORDING_ROLES = {"generator", "reviser", "reasoning_subagent"}
_OBSTACLE_SCOPES = frozenset({"local", "global", "unclear"})
# 记录里跟随 obstacle 一起保留的自由文本字段，顺序即 Markdown 渲染顺序。
_RECORD_TEXT_FIELDS = (
    ("delivered_instead", "Delivered Instead Of Assigned Target"),
    ("obstacle_scope", "Obstacle Scope"),
    ("delegated_description", "Delegated Task"),
    ("delegated_task", "Task Context"),
)


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
    unmet_obligation: str | None = None,
) -> tuple[Path | None, dict[str, Any] | None]:
    """Package worker-local obstacle records for later curator review.

    A reasoning subagent's task-specific observation is retained as evidence.  When
    an outer generator or reviser has recorded a conclusion, that conclusion is the
    concise worker-level obstacle; otherwise the latest reasoning observation is used.

    ``unmet_obligation`` is the task auditor's residual obligation.  When the audit
    found the assigned task undelivered but no role recorded an obstacle, the runtime
    substitutes it so the gap reaches the curator as an explicit, attributed record
    instead of disappearing.
    """
    if str(failure_kind or "") in _NON_MATHEMATICAL_FAILURES:
        return None, None
    declaration = load_difficulty_declaration(declaration_path)
    records = _records_from_state(declaration) if declaration is not None else []
    if not records:
        # 结构化状态缺失时不丢弃 worker 证据：从可读日志恢复。
        records = _records_from_markdown(declaration_path)
    source = next(
        (record for record in reversed(records) if record["role"] != "reasoning_subagent"),
        records[-1] if records else None,
    )
    obstacle = _clean(source.get("obstacle")) if source is not None else ""
    reporting = "worker_recorded"
    if not obstacle:
        # 没有任何角色记录障碍时，用审计的残留义务补一条可归因的占位记录，
        # 让"应记未记"这件事本身成为 curator 可见的证据。
        obstacle = _clean(unmet_obligation)
        if not obstacle:
            return None, None
        source = {
            "index": len(records) + 1,
            "role": "task_auditor",
            "obstacle": obstacle,
            "delivered_instead": "not stated by the worker; reconstructed from the task audit",
            "obstacle_scope": "unclear",
        }
        records = [*records, source]
        reporting = "runtime_reconstructed_from_task_audit"
    context = (declaration or {}).get("runtime_context")
    if not isinstance(context, dict) or not context:
        context = _load_runtime_context(declaration_path)
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
        "delivered_instead": _clean(source.get("delivered_instead")),
        "obstacle_scope": _normalized_scope(source.get("obstacle_scope")),
        "obstacle_source_role": source["role"],
        "obstacle_reporting": reporting,
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
            delivered = _required_text(args, "delivered_instead")
            scope = _normalized_scope(args.get("obstacle_scope"))
            declaration_path.parent.mkdir(parents=True, exist_ok=True)
            runtime_context = _load_runtime_context(declaration_path)
            state = load_difficulty_declaration(declaration_path)
            if state is None:
                state = {
                    "schema_version": _HANDOFF_SCHEMA_VERSION,
                    "runtime_context": runtime_context,
                    # 结构化状态缺失时从可读日志续接，避免记录号回退覆盖历史。
                    "records": _records_from_markdown(declaration_path),
                }
            records = _records_from_state(state)
            record = {
                "index": len(records) + 1,
                "role": role,
                "obstacle": obstacle,
                "delivered_instead": delivered,
                "obstacle_scope": scope,
                **automatic_context,
            }
            records.append(record)
            state["schema_version"] = _HANDOFF_SCHEMA_VERSION
            state["runtime_context"] = runtime_context or state.get("runtime_context") or {}
            state["records"] = records
            state.pop("generator", None)
            state.pop("revisions", None)
            # JSON 是唯一状态源；Markdown 每次由完整记录集重新渲染，
            # 因此两个文件不会再各自漂移，任一被清理都能从另一个恢复。
            _atomic_write_json(difficulty_json_path(declaration_path), state)
            _write_declaration_markdown(declaration_path, records)
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
            "Record a concrete research obstacle. Call it in either case: (a) what you deliver is not the assigned "
            "target, or (b) you completed the task but ruled out a nameable route on the way. "
            "Write prose, not keywords: what is missing and why it blocks; which routes you tried and how each one "
            "died; what would be needed to cross it; and whether the obstacle is local to this task or a global "
            "obstruction of the research problem. "
            "This is worker-local evidence for curator review, never a canonical DAG edit or research plan. "
            "Role, assigned target, artifacts, and (for reasoning subagents) delegated task context are attached automatically."
        ),
        parameters={
            "type": "object",
            "properties": {
                "obstacle": {
                    "type": "string",
                    "description": (
                        "Prose account of the obstacle: the missing step, condition, construction, or repair and why it "
                        "blocks the assigned target; each route already tried and the concrete reason it failed (cite a "
                        "verified proposition or an explicit witness when one refutes a route); and what would be needed "
                        "to cross it. Name routes a later attempt should not repeat."
                    ),
                },
                "delivered_instead": {
                    "type": "string",
                    "description": (
                        "What this attempt actually establishes, compared with the assigned target: the weakened or "
                        "substituted statement proved (added hypothesis, narrowed class, non-sharp constant, "
                        "counterexample) and which part of the target remains unproved. Write 'assigned target proved; "
                        "obstacle was encountered and bypassed' when you did complete the target, or 'nothing "
                        "delivered' when you stopped without a result."
                    ),
                },
                "obstacle_scope": {
                    "type": "string",
                    "enum": sorted(_OBSTACLE_SCOPES),
                    "description": (
                        "'local' if the obstacle is a technical step inside this bounded task; 'global' if it obstructs "
                        "the research problem itself and any route must cross it. Use 'unclear' only when the evidence "
                        "genuinely does not distinguish the two."
                    ),
                },
            },
            "required": ["obstacle", "delivered_instead", "obstacle_scope"],
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
            for key, _heading in _RECORD_TEXT_FIELDS:
                value = _clean(raw.get(key))
                if value:
                    record[key] = value
            session_id = _clean(raw.get("subagent_session_id"))
            if session_id:
                record["subagent_session_id"] = session_id
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


_RECORD_HEADING = re.compile(r"^###\s+Record\s+\d+\s+[—-]\s*(\S+)\s*$", re.MULTILINE)
_SECTION_HEADING = re.compile(r"^##\s+(.+?)\s*$")


def _records_from_markdown(declaration_path: Path) -> list[dict[str, Any]]:
    """Recover obstacle records from the readable log when the JSON state is unavailable.

    The JSON file is the machine-readable source of truth and the Markdown log is
    rendered from it, so the log stays a faithful fallback and keeps worker evidence
    from being silently dropped when one of the two files is cleaned up.
    """
    try:
        text = declaration_path.read_text(encoding="utf-8")
    except OSError:
        return []
    parts = _RECORD_HEADING.split(text)
    records: list[dict[str, Any]] = []
    for role, body in zip(parts[1::2], parts[2::2]):
        role = role.strip()
        if role not in _RECORDING_ROLES:
            continue
        sections = _markdown_sections(body)
        obstacle = _clean(sections.get("obstacle"))
        if not obstacle:
            continue
        record: dict[str, Any] = {"index": len(records) + 1, "role": role, "obstacle": obstacle}
        for key, heading in _RECORD_TEXT_FIELDS:
            value = _clean(sections.get(heading.lower()))
            if value:
                record[key] = value
        records.append(record)
    return records[-32:]


def _markdown_sections(body: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    heading: str | None = None
    buffer: list[str] = []
    for line in body.splitlines():
        match = _SECTION_HEADING.match(line)
        if match is None:
            buffer.append(line)
            continue
        if heading is not None:
            sections[heading] = "\n".join(buffer).strip()
        heading = match.group(1).strip().lower()
        buffer = []
    if heading is not None:
        sections[heading] = "\n".join(buffer).strip()
    return sections


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


def _write_declaration_markdown(declaration_path: Path, records: list[dict[str, Any]]) -> None:
    """Render the human-readable log from the full record set (JSON is the state)."""
    body = "\n\n".join(_render_record(record) for record in records)
    declaration_path.write_text("# Difficulty Records\n\n" + body.rstrip() + "\n", encoding="utf-8")


def _render_record(record: dict[str, Any]) -> str:
    lines = [
        f"### Record {record['index']} — {record['role']}",
        "",
        "## Obstacle",
        "",
        record["obstacle"],
    ]
    for key, heading in _RECORD_TEXT_FIELDS:
        value = _clean(record.get(key))
        if value:
            lines.extend(["", f"## {heading}", "", value])
    return "\n".join(lines)


def _normalized_scope(value: Any) -> str:
    scope = _clean(value).lower()
    return scope if scope in _OBSTACLE_SCOPES else "unclear"
