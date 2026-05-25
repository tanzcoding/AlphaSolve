from __future__ import annotations

from .common import _format_list_result


def _format_list_dir_result(path: str, entries: list[str], *, max_results: int) -> str:
    text = _format_list_result(
        f"ListDir results for {path}",
        entries,
        max_results=max_results,
    )
    entry_set = set(entries)
    if "problem.md" in entry_set and ("verified_propositions/" in entry_set or "knowledge/" in entry_set):
        text += (
            "\n\n<system>research workspace hint: this directory has problem.md plus research/proof folders. "
            "For progress review, run ResearchProgressReview on this directory before broad Read or InspectMarkdown sweeps; "
            "it looks for verified-style proof files whose conclusions are buried in proof tails rather than explicit statements. "
            "If such a buried conclusion affects the answer, stopping criterion, or planning, surface it as an explicit proposition before harder new work.</system>"
        )
    return text
