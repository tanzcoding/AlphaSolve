"""Append-only operational facts used to build curator comparison briefs."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .project import ProjectLayout


_LOCK = threading.Lock()


def append_event(layout: "ProjectLayout", kind: str, **fields: Any) -> dict[str, Any]:
    """Persist an internal event without treating it as knowledge-base content."""
    event = {
        "kind": str(kind),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **fields,
    }
    path = layout.curation_events_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def read_recent_events(layout: "ProjectLayout", *, limit: int = 40) -> list[dict[str, Any]]:
    path = layout.curation_events_path
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines[-max(0, int(limit)):]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def relative_path(path: Path, workspace_dir: Path) -> str:
    try:
        return path.resolve().relative_to(workspace_dir.resolve()).as_posix()
    except ValueError:
        return str(path)
