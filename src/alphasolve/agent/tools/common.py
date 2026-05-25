from __future__ import annotations

from typing import Any


def _tool_error(message: str) -> str:
    return f"ERROR: {message}"


def _format_list_result(title: str, items: list[str], *, max_results: int) -> str:
    lines = [title, f"returned: {len(items)}", f"limit: {max_results}"]
    if len(items) >= max_results:
        lines.append("note: result hit max_results; rerun with a narrower path/pattern or higher max_results if needed.")
    if items:
        lines.extend(f"- {item}" for item in items)
    else:
        lines.append("(no results)")
    return "\n".join(lines)


def _format_grep_result(hits: list[dict[str, Any]], *, pattern: str, path: str, max_results: int) -> str:
    lines = [
        "Grep results",
        f"pattern: {pattern}",
        f"path: {path}",
        f"returned: {len(hits)}",
        f"limit: {max_results}",
    ]
    if len(hits) >= max_results:
        lines.append("note: result hit max_results; rerun with a narrower path/pattern or higher max_results if needed.")
    if not hits:
        lines.append("(no matches)")
        return "\n".join(lines)
    for hit in hits:
        hit_path = hit.get("path", "")
        line = hit.get("line", "")
        text = hit.get("text", "")
        lines.append(f"{hit_path}:{line}: {text}")
        context = str(hit.get("context") or "").strip()
        if context and context != f"{line}: {text}":
            lines.append(context)
    return "\n".join(lines)

