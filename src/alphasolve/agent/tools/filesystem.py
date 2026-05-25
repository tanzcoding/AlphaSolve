from __future__ import annotations

from .common import _format_list_result


def _format_list_dir_result(path: str, entries: list[str], *, max_results: int) -> str:
    text = _format_list_result(
        f"ListDir results for {path}",
        entries,
        max_results=max_results,
    )
    return text
