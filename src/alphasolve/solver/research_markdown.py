from __future__ import annotations

import re
from typing import Any, Mapping

from alphasolve.agent import WorkspaceLike
from alphasolve.agent.tools.common import _tool_error
from alphasolve.agent.tools.types import ToolResult


_MARKDOWN_SECTION_HEADING_RE = re.compile(
    r"^\s{0,3}#{1,6}\s+.*("
    r"statement|proposition|claim|result|conclusion|summary|progress|"
    r"insight|finding|next|open"
    r").*$",
    re.IGNORECASE,
)
_MARKDOWN_IMPORTANT_LINE_RE = re.compile(
    r"("
    r"therefore|hence|thus|consequently|it follows|we get|we obtain|"
    r"proved|shown|strict|strictly|contradiction|answer|conclusion|"
    r"boxed|\\ge|\\le|\\gt|\\lt|>=|<=|>|<|=|\u2265|\u2264"
    r")",
    re.IGNORECASE,
)
_MARKDOWN_PROOF_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+.*(proof|argument|verification).*$", re.IGNORECASE)
_MARKDOWN_STATEMENT_HEADING_RE = re.compile(
    r"^\s{0,3}#{1,6}\s+.*(statement|proposition|claim|result|conclusion).*$",
    re.IGNORECASE,
)
_MARKDOWN_REVIEW_HEADING_RE = re.compile(
    r"^\s{0,3}#{1,6}\s+.*("
    r"current|progress|insight|status|summary|overview|todo|next|recommend|"
    r"remaining|open|gap|block|blocked|warning|fail|failed|review|decision|"
    r"risk|issue|missing|known|unknown|assessment"
    r").*$",
    re.IGNORECASE,
)
_MARKDOWN_REVIEW_LINE_RE = re.compile(
    r"("
    r"current|progress|insight|status|todo|next|recommend|remaining|open|"
    r"gap|block|blocked|warning|fail|failed|review|decision|risk|issue|"
    r"missing|known|unknown|should|must|cannot|not yet|not proved"
    r")",
    re.IGNORECASE,
)
_FOCUS_TERM_STOPWORDS = {
    "and",
    "are",
    "for",
    "from",
    "here",
    "into",
    "left",
    "let",
    "over",
    "right",
    "such",
    "that",
    "the",
    "then",
    "this",
    "where",
    "with",
}
_TAIL_COMPARISON_STOPWORDS = {
    "consequently",
    "hence",
    "therefore",
    "thus",
}


def _parse_numbered_read_output(output: str) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for raw_line in output.splitlines():
        prefix, separator, text = raw_line.partition("\t")
        if not separator:
            continue
        try:
            line_no = int(prefix.strip())
        except ValueError:
            continue
        rows.append((line_no, text))
    return rows


def _extract_markdown_section(
    rows: list[tuple[int, str]],
    *,
    start_index: int,
    max_lines: int,
) -> list[tuple[int, str]]:
    heading = rows[start_index][1]
    marker = re.match(r"^\s{0,3}(#{1,6})\s+", heading)
    level = len(marker.group(1)) if marker else 6
    out: list[tuple[int, str]] = []
    for index in range(start_index, min(len(rows), start_index + max_lines)):
        line_no, text = rows[index]
        if index > start_index:
            next_marker = re.match(r"^\s{0,3}(#{1,6})\s+", text)
            if next_marker and len(next_marker.group(1)) <= level:
                break
        out.append((line_no, text))
    return out


def _format_markdown_rows(rows: list[tuple[int, str]], *, max_chars: int = 16000) -> str:
    text = "\n".join(f"{line_no}: {line}" for line_no, line in rows)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 80].rstrip() + "\n...[truncated by InspectMarkdown output budget]"


def _normalize_markdown_review_text(text: str) -> str:
    return re.sub(r"\s+", "", text.lower())


def _markdown_content_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in re.findall(r"\\?[A-Za-z0-9_]+", text):
        if raw.startswith("\\"):
            continue
        token = raw.lower()
        if token:
            tokens.append(token)
    return tokens


def _tail_line_is_redundant(text: str, statement_text: str) -> bool:
    tokens = [token for token in _markdown_content_tokens(text) if token not in _TAIL_COMPARISON_STOPWORDS]
    if len(tokens) < 3:
        return False
    statement_tokens = set(_markdown_content_tokens(statement_text))
    return all(token in statement_tokens for token in tokens)


def _markdown_statement_span(rows: list[tuple[int, str]]) -> tuple[int, int] | None:
    start_index: int | None = None
    start_level = 6
    for index, (_line_no, text) in enumerate(rows):
        if not _MARKDOWN_STATEMENT_HEADING_RE.search(text):
            continue
        marker = re.match(r"^\s{0,3}(#{1,6})\s+", text)
        start_index = index
        start_level = len(marker.group(1)) if marker else 6
        break
    if start_index is None:
        return None
    end_index = len(rows)
    for index in range(start_index + 1, len(rows)):
        marker = re.match(r"^\s{0,3}(#{1,6})\s+", rows[index][1])
        if marker and len(marker.group(1)) <= start_level:
            end_index = index
            break
    return start_index, end_index


def _markdown_underclaimed_tail_highlights(
    rows: list[tuple[int, str]],
    *,
    max_highlights: int = 10,
) -> list[tuple[int, str]]:
    has_proof = any(_MARKDOWN_PROOF_HEADING_RE.search(text) for _line_no, text in rows)
    statement_span = _markdown_statement_span(rows)
    if statement_span is None or not has_proof:
        return []

    start, end = statement_span
    statement_text = "\n".join(text for _line_no, text in rows[start:end])
    normalized_statement = _normalize_markdown_review_text(statement_text)

    tail_window = rows[-min(45, len(rows)):]
    conclusion_rows = [
        (line_no, text)
        for line_no, text in tail_window
        if _MARKDOWN_IMPORTANT_LINE_RE.search(text)
    ]
    novel_rows = [
        (line_no, text)
        for line_no, text in conclusion_rows
        if _normalize_markdown_review_text(text) not in normalized_statement
        and not _tail_line_is_redundant(text, statement_text)
    ]
    if not novel_rows:
        return []

    last_line_no = rows[-1][0]
    novel_rows = sorted(
        novel_rows,
        key=lambda item: _markdown_tail_priority(item[0], item[1], last_line_no=last_line_no),
        reverse=True,
    )[:max_highlights]
    return sorted(novel_rows, key=lambda item: item[0])


def _markdown_tail_priority(line_no: int, text: str, *, last_line_no: int) -> tuple[int, int]:
    lower = text.lower()
    score = 0
    if any(token in lower for token in ("therefore", "hence", "consequently", "it follows", "we get", "we obtain", "since")):
        score += 5
    if any(token in lower for token in ("strict", "strictly", ">", "<", "\u2265", "\u2264", r"\ge", r"\le")):
        score += 4
    if any(token in lower for token in ("boxed", "conclusion", "proved", "shown", "answer")):
        score += 3
    if "=" in text:
        score += 1
    return score, line_no - last_line_no


def _markdown_read_review_hint(path: str, result_output: str, result_message: str) -> str:
    if not path.lower().endswith((".md", ".markdown")):
        return ""
    rows = _parse_numbered_read_output(result_output)
    if not rows:
        return ""

    has_statement = any(_MARKDOWN_STATEMENT_HEADING_RE.search(text) for _line_no, text in rows)
    has_proof = any(_MARKDOWN_PROOF_HEADING_RE.search(text) for _line_no, text in rows)
    if "more lines remain" in result_message.lower() and (has_statement or has_proof):
        return (
            "<system>Markdown review hint: this file continues beyond the lines shown. "
            "For research/proof review, inspect the proof ending as well as the statement; "
            "important conclusions are often near the tail. Use a later line_offset, read_all=true, "
            "or InspectMarkdown.</system>"
        )

    novel_rows = _markdown_underclaimed_tail_highlights(rows, max_highlights=10)
    if not novel_rows:
        return ""

    lines = [
        "<system>Markdown proof-review hint: compare the Statement section with the proof tail. "
        "The tail contains conclusion-like or comparison-heavy lines that are not verbatim in the statement. "
        "For research progress review, treat this as a possible underclaimed proof. "
        "If a highlighted tail conclusion changes an answer, best-known objective value or estimate, stopping condition, or planning premise, "
        "the next proposition should normally be an explicit statement of that already-established conclusion, "
        "before proposing harder downstream work, so future reviewers and orchestrators can see it from the statement. "
        "When asked what proposition to do next, prefer this consolidation step unless another already-read Statement explicitly records the same conclusion.",
        "tail highlights:",
    ]
    lines.extend(f"{line_no}: {text}" for line_no, text in novel_rows)
    lines.append("</system>")
    return "\n".join(lines)


def _inspect_markdown_file(
    workspace: WorkspaceLike,
    path: str,
    *,
    statement_lines: int,
    tail_lines: int,
) -> str:
    result = workspace.read_text_page(path, line_offset=1, read_all=True)
    rows = _parse_numbered_read_output(result.output)
    lines = [f"== {path} ==", result.message]
    if not rows:
        lines.append("(empty file)")
        return "\n".join(lines)

    headings = [(line_no, text) for line_no, text in rows if re.match(r"^\s{0,3}#{1,6}\s+", text)]
    if headings:
        lines.append("headings:")
        for line_no, text in headings[:60]:
            lines.append(f"- {line_no}: {text.strip()}")
        if len(headings) > 60:
            lines.append(f"... {len(headings) - 60} more headings omitted")

    selected_sections: list[list[tuple[int, str]]] = []
    seen_starts: set[int] = set()
    for index, (_line_no, text) in enumerate(rows):
        if _MARKDOWN_SECTION_HEADING_RE.search(text):
            section = _extract_markdown_section(rows, start_index=index, max_lines=statement_lines)
            if section and section[0][0] not in seen_starts:
                selected_sections.append(section)
                seen_starts.add(section[0][0])
        if len(selected_sections) >= 8:
            break
    if selected_sections:
        lines.append("selected statement/progress sections:")
        for section in selected_sections:
            lines.append(_format_markdown_rows(section, max_chars=2200))

    tail = rows[-tail_lines:]
    important_tail = [(line_no, text) for line_no, text in tail if _MARKDOWN_IMPORTANT_LINE_RE.search(text)]
    if important_tail:
        lines.append("important-looking lines near file end:")
        lines.append(_format_markdown_rows(important_tail, max_chars=1600))
    lines.append(f"file tail (last {min(tail_lines, len(rows))} lines):")
    lines.append(_format_markdown_rows(tail, max_chars=2200))
    return "\n".join(lines)


def _run_inspect_markdown(workspace: WorkspaceLike, args: dict[str, Any]) -> ToolResult:
    path = str(args.get("path", "."))
    max_files = int(args.get("max_files", 8))
    tail_lines = int(args.get("tail_lines", 40))
    statement_lines = int(args.get("statement_lines", 80))
    try:
        if path.lower().endswith(".md"):
            files = [path]
        else:
            files = [
                item for item in workspace.glob("**/*.md", path=path, max_results=max_files)
                if not item.endswith("/")
            ]
        parts = [
            "Markdown inspection",
            f"path: {path}",
            f"files inspected: {len(files)}",
            f"file limit: {max_files}",
            (
                "purpose: inspect a specific Markdown file or narrow directory by surfacing statements, progress sections, and endings; "
                "for a whole workspace, run ResearchProgressReview first and then inspect the cited files."
            ),
        ]
        if len(files) >= max_files and not path.lower().endswith(".md"):
            parts.append("note: file list hit max_files; inspect a narrower directory if important files may be omitted.")
        if not files:
            parts.append("(no markdown files found)")
        candidates = _collect_underclaimed_proof_candidates(workspace, files, max_candidates=8)
        if candidates:
            parts.append(_format_underclaimed_proof_candidates(
                candidates,
                heading="progress audit: possible underclaimed proof statements",
                include_rule=True,
            ))
        for file_path in files:
            parts.append(_inspect_markdown_file(
                workspace,
                file_path,
                statement_lines=statement_lines,
                tail_lines=tail_lines,
            ))
        return ToolResult("\n\n".join(parts))
    except Exception as exc:
        return ToolResult(_tool_error(str(exc)), is_error=True)


def _underclaimed_candidate_score(file_path: str, highlights: list[tuple[int, str]]) -> tuple[int, int]:
    text = f"{file_path}\n" + "\n".join(line for _line_no, line in highlights)
    lower = text.lower()
    normalized_path = file_path.strip("/").replace("\\", "/")
    path_parts = [part for part in normalized_path.split("/") if part]
    score = 0
    if len(path_parts) <= 2:
        score += 6
    elif len(path_parts) <= 3:
        score += 2
    if "/" not in normalized_path:
        score += 4
    for token in (
        "answer",
        "target",
        "original problem",
        "result",
        "goal",
        "objective",
        "value",
        "stopping",
        "stop condition",
    ):
        if token in lower:
            score += 4
    if any(token in lower for token in (r"\ge", ">=", r"\le", "<=")) and any(
        token in lower for token in ("strict", "strictly", ">", "<", r"\gt", r"\lt")
    ):
        score += 5
    for token in ("strict", "strictly", ">", "<", r"\gt", r"\lt"):
        if token in lower:
            score += 3
    for token in ("therefore", "hence", "consequently", "it follows", "we obtain", "we get"):
        if token in lower:
            score += 2
    if "proposition" in lower or "statement" in lower:
        score += 1
    return score, -len(file_path)


def _collect_underclaimed_proof_candidates(
    workspace: WorkspaceLike,
    files: list[str],
    *,
    max_candidates: int,
) -> list[tuple[str, list[tuple[int, str]]]]:
    candidates: list[tuple[str, list[tuple[int, str]]]] = []
    for file_path in files:
        try:
            result = workspace.read_text_page(file_path, line_offset=1, read_all=True)
        except Exception:
            continue
        rows = _parse_numbered_read_output(result.output)
        highlights = _markdown_underclaimed_tail_highlights(rows, max_highlights=6)
        if highlights:
            candidates.append((file_path, highlights))
    candidates.sort(
        key=lambda item: _underclaimed_candidate_score(item[0], item[1]),
        reverse=True,
    )
    return candidates[:max_candidates]


def _format_underclaimed_proof_candidates(
    candidates: list[tuple[str, list[tuple[int, str]]]],
    *,
    heading: str,
    include_rule: bool,
) -> str:
    lines = [heading, f"count shown: {len(candidates)}"]
    if include_rule:
        lines.append(
            "general next-action rule: if a highlighted proof tail already establishes a conclusion that matters for the original problem, "
            "stopping criterion, answer, best-known objective value, best-known estimate, or future planning, the next proposition should normally be an explicit statement of that "
            "already-established conclusion before pursuing harder downstream work. When choosing the next proposition, prefer this "
            "consolidation step unless another already-read Statement explicitly records the same conclusion."
        )
        lines.append("why shown: each listed file has conclusion-like or comparison-heavy proof-tail lines not verbatim in the Statement section.")
    for file_path, highlights in candidates:
        lines.append(f"- {file_path}")
        for line_no, text in highlights:
            lines.append(f"  tail {line_no}: {text}")
    return "\n".join(lines)


def _contains_word(text: str, tokens: tuple[str, ...], *, split_on_hyphen: bool = True) -> bool:
    word_chars = "A-Za-z0-9_" if split_on_hyphen else "A-Za-z0-9_-"
    for token in tokens:
        pattern = rf"(?<![{word_chars}])" + re.escape(token) + rf"(?![{word_chars}])"
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def _extract_focus_terms(rows: list[tuple[int, str]], *, max_terms: int = 80) -> set[str]:
    terms: set[str] = set()
    for _line_no, text in rows:
        for raw in re.findall(r"[A-Za-z0-9_\\]+", text):
            if "\\" in raw:
                continue
            term = raw.strip("\\").lower()
            if len(term) < 3 or term in _FOCUS_TERM_STOPWORDS:
                continue
            has_digit = any(char.isdigit() for char in term)
            letters = [char for char in term if char.isalpha()]
            if term.isdigit():
                if len(term) < 3:
                    continue
            elif "_" in term:
                if len(letters) < 3:
                    continue
            elif has_digit:
                if len(letters) < 3:
                    continue
            else:
                continue
            terms.add(term)
            if len(terms) >= max_terms:
                return terms
    return terms


def _problem_focus_terms(workspace: WorkspaceLike, root: str) -> set[str]:
    prefix = "" if root in {"", "."} else root.rstrip("/") + "/"
    return _extract_focus_terms(_safe_read_markdown_rows(workspace, prefix + "problem.md"))


def _candidate_focus_text(candidate: tuple[str, list[tuple[int, str]]]) -> str:
    file_path, highlights = candidate
    return f"{file_path}\n" + "\n".join(text for _line_no, text in highlights)


def _focus_term_present(text: str, term: str) -> bool:
    pattern = rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _focus_term_weights(
    candidates: list[tuple[str, list[tuple[int, str]]]],
    focus_terms: set[str],
) -> dict[str, int]:
    if not candidates or not focus_terms:
        return {}
    candidate_texts = [_candidate_focus_text(candidate).lower() for candidate in candidates]
    common_cutoff = max(3, len(candidate_texts) // 5)
    weights: dict[str, int] = {}
    for term in focus_terms:
        document_count = sum(1 for text in candidate_texts if _focus_term_present(text, term))
        if document_count == 0 or document_count > common_cutoff:
            continue
        if document_count == 1:
            weights[term] = 8
        elif document_count <= 3:
            weights[term] = 6
        else:
            weights[term] = 3
    return weights


def _focus_term_score(text: str, focus_weights: Mapping[str, int]) -> int:
    if not focus_weights:
        return 0
    score = 0
    for term, weight in focus_weights.items():
        if _focus_term_present(text, term):
            score += weight
    return min(24, score)


def _has_focused_strict_line(highlights: list[tuple[int, str]], focus_weights: Mapping[str, int]) -> bool:
    if not focus_weights:
        return False
    for _line_no, text in highlights:
        lower = text.lower()
        if not any(_focus_term_present(lower, term) for term in focus_weights):
            continue
        if any(token in lower for token in ("strict", "strictly", ">", "<", r"\gt", r"\lt")):
            return True
    return False


def _has_focused_comparison_line(highlights: list[tuple[int, str]], focus_weights: Mapping[str, int]) -> bool:
    if not focus_weights:
        return False
    for _line_no, text in highlights:
        lower = text.lower()
        if not any(_focus_term_present(lower, term) for term in focus_weights):
            continue
        if any(token in lower for token in (r"\ge", ">=", r"\le", "<=", "\u2265", "\u2264", ">", "<", r"\gt", r"\lt")):
            return True
    return False


def _underclaimed_default_candidate_score(
    file_path: str,
    highlights: list[tuple[int, str]],
    *,
    focus_weights: Mapping[str, int] | None = None,
) -> int:
    normalized_path = file_path.strip("/").replace("\\", "/")
    path_parts = [part for part in normalized_path.split("/") if part]
    highlights_text = "\n".join(line for _line_no, line in highlights)
    lower_path = file_path.lower()
    lower_highlights = highlights_text.lower()
    lower = f"{lower_path}\n{lower_highlights}"
    score = 0
    if len(path_parts) <= 2:
        score += 4
    elif len(path_parts) >= 4:
        score -= 4
    path_impact_tokens = (
        "answer",
        "target",
        "original problem",
        "stopping",
        "stop condition",
        "value",
        "result",
        "goal",
        "objective",
    )
    highlight_impact_tokens = path_impact_tokens
    if _contains_word(lower_path, path_impact_tokens) or _contains_word(
        lower_highlights,
        highlight_impact_tokens,
        split_on_hyphen=False,
    ):
        score += 8
    focus_weights = focus_weights or {}
    score += _focus_term_score(f"{file_path}\n{highlights_text}", focus_weights)
    if _has_focused_comparison_line(highlights, focus_weights):
        score += 10
    if _has_focused_strict_line(highlights, focus_weights):
        score += 8
    has_non_strict_comparison = any(token in lower for token in (r"\ge", ">=", r"\le", "<=", "\u2265", "\u2264"))
    has_strict_comparison = any(token in lower for token in ("strict", "strictly", ">", "<", r"\gt", r"\lt"))
    if has_non_strict_comparison and has_strict_comparison:
        score += 8
    elif has_strict_comparison:
        score += 3
    if "boxed" in lower or "conclusion" in lower:
        score += 2
    if any(token in normalized_path.lower() for token in ("technical", "auxiliary", "helper", "local")):
        score -= 3
    return score


def _select_underclaimed_default_candidate(
    candidates: list[tuple[str, list[tuple[int, str]]]],
    *,
    focus_terms: set[str] | None = None,
) -> tuple[str, list[tuple[int, str]]] | None:
    if not candidates:
        return None
    verified_candidates = [
        item for item in candidates
        if item[0].replace("\\", "/").startswith("verified_propositions/")
    ]
    search_pool = verified_candidates or candidates
    focus_weights = _focus_term_weights(search_pool, focus_terms or set())
    if focus_weights:
        focused_pool = [
            item for item in search_pool
            if _focus_term_score(_candidate_focus_text(item), focus_weights) > 0
        ]
        if focused_pool:
            search_pool = focused_pool
    selected = max(
        search_pool,
        key=lambda item: _underclaimed_default_candidate_score(item[0], item[1], focus_weights=focus_weights),
    )
    if _underclaimed_default_candidate_score(selected[0], selected[1], focus_weights=focus_weights) < 15:
        return None
    return selected


def _safe_read_markdown_rows(workspace: WorkspaceLike, path: str) -> list[tuple[int, str]]:
    try:
        result = workspace.read_text_page(path, line_offset=1, read_all=True)
    except Exception:
        return []
    return _parse_numbered_read_output(result.output)


def _extract_review_sections(
    rows: list[tuple[int, str]],
    *,
    max_sections: int,
    max_lines_per_section: int,
) -> list[list[tuple[int, str]]]:
    sections: list[list[tuple[int, str]]] = []
    seen: set[int] = set()
    for index, (_line_no, text) in enumerate(rows):
        if not _MARKDOWN_REVIEW_HEADING_RE.search(text):
            continue
        section = _extract_markdown_section(rows, start_index=index, max_lines=max_lines_per_section)
        if not section or section[0][0] in seen:
            continue
        sections.append(section)
        seen.add(section[0][0])
        if len(sections) >= max_sections:
            break
    return sections


def _extract_review_lines(rows: list[tuple[int, str]], *, max_lines: int) -> list[tuple[int, str]]:
    selected: list[tuple[int, str]] = []
    for line_no, text in rows:
        stripped = text.strip()
        if not stripped:
            continue
        if _MARKDOWN_REVIEW_LINE_RE.search(stripped):
            selected.append((line_no, stripped))
        if len(selected) >= max_lines:
            break
    return selected


def _workspace_note_files(workspace: WorkspaceLike, root: str) -> list[str]:
    prefix = "" if root in {"", "."} else root.rstrip("/") + "/"
    candidates = [
        "problem.md",
        "hint.md",
        "README.md",
        "readme.md",
        "verified_propositions/index.md",
        "knowledge/index.md",
    ]
    files: list[str] = []
    seen: set[str] = set()
    for rel_path in candidates:
        path = prefix + rel_path
        if path in seen:
            continue
        if _safe_read_markdown_rows(workspace, path):
            files.append(path)
            seen.add(path)
    return files


def _format_workspace_notes(workspace: WorkspaceLike, root: str) -> str:
    files = _workspace_note_files(workspace, root)
    if not files:
        return ""
    lines = [
        "workspace notes",
        (
            "purpose: show compact source excerpts about current state, warnings, gaps, and next actions. "
            "These are navigation evidence, not a substitute for reading the cited files."
        ),
    ]
    for file_path in files:
        rows = _safe_read_markdown_rows(workspace, file_path)
        if not rows:
            continue
        sections = _extract_review_sections(rows, max_sections=2, max_lines_per_section=16)
        review_lines = _extract_review_lines(rows, max_lines=8)
        if not sections and not review_lines:
            review_lines = rows[: min(12, len(rows))]
        lines.append(f"- {file_path}")
        if sections:
            for section in sections:
                lines.append(_format_markdown_rows(section, max_chars=1600))
        elif review_lines:
            lines.append(_format_markdown_rows(review_lines, max_chars=1000))
    return "\n".join(lines)


def _unfinished_attempt_files(workspace: WorkspaceLike, root: str, *, max_files: int) -> list[str]:
    prefix = "" if root in {"", "."} else root.rstrip("/") + "/"
    base = prefix + "unverified_propositions"
    try:
        files = [
            item for item in workspace.glob("**/*.md", path=base, max_results=max_files * 4)
            if not item.endswith("/") and item.replace("\\", "/").endswith(("/review.md", "/worker_hint.md"))
        ]
    except Exception:
        return []
    return files[:max_files]


def _review_repair_score(rows: list[tuple[int, str]]) -> int:
    text = "\n".join(row_text.lower() for _line_no, row_text in rows)
    if "verdict" not in text or "fail" not in text:
        return 0
    score = 0
    for token in (
        "repairable",
        "fixable",
        "localized",
        "localised",
        "one-line",
        "mostly correct",
        "overall architecture",
        "rest of the proof",
        "sound",
    ):
        if token in text:
            score += 2
    for token in ("critical gap", "materially incomplete", "as written"):
        if token in text:
            score += 1
    return score


def _review_repair_lines(rows: list[tuple[int, str]], *, max_lines: int = 30) -> list[tuple[int, str]]:
    terms = (
        "candidate",
        "summary",
        "positive",
        "critical gap",
        "assessment",
        "verdict",
        "repairable",
        "fixable",
        "localized",
        "localised",
        "one-line",
        "overall architecture",
        "sound",
    )
    selected: list[tuple[int, str]] = []
    seen: set[int] = set()
    for index, (line_no, text) in enumerate(rows):
        lower = text.lower()
        if not any(term in lower for term in terms):
            continue
        for nearby_line_no, nearby_text in rows[max(0, index - 1): min(len(rows), index + 7)]:
            if nearby_line_no in seen:
                continue
            if not nearby_text.strip():
                continue
            selected.append((nearby_line_no, nearby_text))
            seen.add(nearby_line_no)
            if len(selected) >= max_lines:
                return selected
    return selected


def _collect_repairable_attempt_candidates(
    workspace: WorkspaceLike,
    root: str,
    *,
    max_results: int = 80,
) -> list[tuple[int, str, list[tuple[int, str]]]]:
    prefix = "" if root in {"", "."} else root.rstrip("/") + "/"
    base = prefix + "unverified_propositions"
    try:
        review_files = [
            item for item in workspace.glob("**/review.md", path=base, max_results=max_results)
            if not item.endswith("/")
        ]
    except Exception:
        return []
    candidates: list[tuple[int, str, list[tuple[int, str]]]] = []
    for file_path in review_files:
        rows = _safe_read_markdown_rows(workspace, file_path)
        if not rows:
            continue
        score = _review_repair_score(rows)
        if score <= 0:
            continue
        candidates.append((score, file_path, rows))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates


def _repairable_related_files(workspace: WorkspaceLike, file_path: str) -> list[str]:
    parent = file_path.rsplit("/", 1)[0] if "/" in file_path else ""
    if not parent:
        return []
    related_files: list[str] = []
    for basename in ("proposition.md", "worker_hint.md"):
        related_path = f"{parent}/{basename}"
        if related_path != file_path and _safe_read_markdown_rows(workspace, related_path):
            related_files.append(related_path)
    return related_files


def _format_repairable_attempt_notes(
    workspace: WorkspaceLike,
    root: str,
    *,
    max_files: int = 6,
    candidates: list[tuple[int, str, list[tuple[int, str]]]] | None = None,
) -> str:
    candidates = candidates if candidates is not None else _collect_repairable_attempt_candidates(workspace, root)
    if not candidates:
        return ""
    lines = [
        "repairable attempt candidates",
        (
            "purpose: failed reviews with localized fixes can be the smallest useful next step; "
            "read the cited review and proposition before choosing broad new work."
        ),
        (
            "evidence budget: first read the top candidate review and proposition; if the defect is local and the result "
            "still matters, stop the broad survey and recommend that repair unless stronger already-read evidence supersedes it."
        ),
        (
            "next review move: inspect the top candidate's review and related files before proposing a new direction; "
            "if most of the attempt is sound and the defect is local, prefer an explicit repair or missing prerequisite."
        ),
        (
            "priority rule: a high-signal local repair is normally the default next proposition unless an already verified "
            "Statement supersedes it; broad new directions should explain why they outrank this shorter path."
        ),
    ]
    for score, file_path, rows in candidates[:max_files]:
        lines.append(f"- {file_path} (repair signal: {score})")
        related_files = _repairable_related_files(workspace, file_path)
        if related_files:
            lines.append("related files: " + ", ".join(related_files))
        lines.append(
            "suggested immediate target: make a repaired explicit proposition from this candidate, or state the missing "
            "local prerequisite, so later reviewers can see the usable conclusion from the Statement."
        )
        selected = _review_repair_lines(rows)
        if selected:
            lines.append(_format_markdown_rows(selected, max_chars=1800))
    if len(candidates) > max_files:
        lines.append("note: more repairable-looking reviews exist; inspect unverified_propositions with a narrower path.")
    return "\n".join(lines)


def _unreviewed_high_level_score(
    proposition_rows: list[tuple[int, str]],
    hint_rows: list[tuple[int, str]],
) -> int:
    text = "\n".join(row_text.lower() for _line_no, row_text in [*proposition_rows[:80], *hint_rows[:80]])
    score = 0
    for token in ("final", "complete", "full", "assembly", "assemble", "end-to-end", "overall", "finish"):
        if token in text:
            score += 2
    for token in ("global", "solution", "solve", "close", "closure", "hypothesis", "interface", "restriction"):
        if token in text:
            score += 1
    if "worker_hint" in text or hint_rows:
        score += 1
    return score


def _collect_unreviewed_high_level_attempts(
    workspace: WorkspaceLike,
    root: str,
    *,
    max_results: int = 80,
) -> list[tuple[int, str, list[tuple[int, str]], list[tuple[int, str]]]]:
    prefix = "" if root in {"", "."} else root.rstrip("/") + "/"
    base = prefix + "unverified_propositions"
    try:
        proposition_files = [
            item for item in workspace.glob("**/proposition.md", path=base, max_results=max_results)
            if not item.endswith("/")
        ]
    except Exception:
        return []
    candidates: list[tuple[int, str, list[tuple[int, str]], list[tuple[int, str]]]] = []
    for file_path in proposition_files:
        parent = file_path.rsplit("/", 1)[0] if "/" in file_path else ""
        if parent and _safe_read_markdown_rows(workspace, f"{parent}/review.md"):
            continue
        proposition_rows = _safe_read_markdown_rows(workspace, file_path)
        if not proposition_rows:
            continue
        hint_rows = _safe_read_markdown_rows(workspace, f"{parent}/worker_hint.md") if parent else []
        score = _unreviewed_high_level_score(proposition_rows, hint_rows)
        if score <= 0:
            continue
        candidates.append((score, file_path, proposition_rows, hint_rows))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates


def _format_unreviewed_high_level_attempts(
    candidates: list[tuple[int, str, list[tuple[int, str]], list[tuple[int, str]]]],
    *,
    max_files: int = 4,
) -> str:
    if not candidates:
        return ""
    lines = [
        "unreviewed high-level attempts",
        (
            "purpose: broad or final-looking attempts without a review can hide interface mistakes. "
            "Before recommending a final direction, inspect the stated inputs, outputs, prerequisites, and cited evidence."
        ),
        (
            "priority rule: if a high-level attempt lacks review, prefer a narrow interface proposition or review target "
            "over another broad final claim; this keeps later orchestration from inheriting unverified assumptions."
        ),
    ]
    for score, file_path, proposition_rows, hint_rows in candidates[:max_files]:
        lines.append(f"- {file_path} (high-level signal: {score})")
        statement = _extract_markdown_section(proposition_rows, start_index=0, max_lines=14)
        if statement:
            lines.append(_format_markdown_rows(statement, max_chars=1000))
        if hint_rows:
            lines.append("worker hint excerpt:")
            lines.append(_format_markdown_rows(hint_rows[:18], max_chars=1200))
        lines.append(
            "suggested immediate target: make the exact interface explicit before a final claim: "
            "which evidence is used, which prerequisites remain, which levels or restrictions change, and what must be reviewed."
        )
    if len(candidates) > max_files:
        lines.append("note: more unreviewed high-level attempts exist; inspect unverified_propositions with a narrower path.")
    return "\n".join(lines)


def _format_unfinished_attempt_notes(workspace: WorkspaceLike, root: str, *, max_files: int = 16) -> str:
    files = _unfinished_attempt_files(workspace, root, max_files=max_files)
    if not files:
        return ""
    lines = [
        "unfinished attempt notes",
        (
            "purpose: surface review and hint excerpts from unfinished work so the next step does not repeat known failures. "
            "If broad unfinished attempts cite missing prerequisites, prefer a smaller explicit prerequisite or interface step."
        ),
    ]
    for file_path in files:
        rows = _safe_read_markdown_rows(workspace, file_path)
        if not rows:
            continue
        sections = _extract_review_sections(rows, max_sections=2, max_lines_per_section=18)
        review_lines = _extract_review_lines(rows, max_lines=8)
        tail = rows[-min(14, len(rows)):]
        lines.append(f"- {file_path}")
        if sections:
            for section in sections:
                lines.append(_format_markdown_rows(section, max_chars=1800))
        elif review_lines:
            lines.append(_format_markdown_rows(review_lines, max_chars=1400))
        else:
            lines.append(_format_markdown_rows(tail, max_chars=1200))
    if len(files) >= max_files:
        lines.append("note: unfinished attempt list hit max_files; inspect a narrower path if later attempts may matter.")
    return "\n".join(lines)


def _markdown_index_progress_audit_hint(workspace: WorkspaceLike, path: str) -> str:
    normalized_path = path.replace("\\", "/")
    if not normalized_path.lower().endswith("/index.md") and normalized_path.lower() != "index.md":
        return ""
    parent = normalized_path.rsplit("/", 1)[0] if "/" in normalized_path else "."
    try:
        files = [
            item for item in workspace.glob("**/*.md", path=parent, max_results=80)
            if not item.endswith("/") and item != normalized_path
        ]
    except Exception:
        return ""
    candidates = _collect_underclaimed_proof_candidates(workspace, files, max_candidates=6)
    if not candidates:
        return ""
    audit = _format_underclaimed_proof_candidates(
        candidates,
        heading="index-adjacent progress audit: possible underclaimed proof statements",
        include_rule=True,
    )
    return (
        "<system>"
        "Because this is an index file, also inspect whether nearby verified/proof Markdown files contain stronger conclusions "
        "than their Statement sections expose. Index summaries can lag behind proof tails.\n"
        f"{audit}\n"
        "</system>"
    )


def _research_progress_scan_paths(workspace: WorkspaceLike, root: str, paths: list[str]) -> list[str]:
    if paths:
        return paths
    try:
        entries = set(workspace.list_dir(root, max_results=500))
    except Exception:
        return [root]
    preferred = [
        f"{root.rstrip('/')}/{name}".lstrip("./")
        for name in ("verified_propositions", "knowledge")
        if f"{name}/" in entries
    ]
    return preferred or [root]


def _run_research_progress_review(workspace: WorkspaceLike, args: dict[str, Any]) -> ToolResult:
    root = str(args.get("path", "."))
    raw_paths = args.get("paths", [])
    paths = [str(path) for path in raw_paths] if isinstance(raw_paths, list) else []
    max_files = int(args.get("max_files", 120))
    try:
        scan_paths = _research_progress_scan_paths(workspace, root, paths)
        files: list[str] = []
        seen: set[str] = set()
        per_path_limit = max(1, max_files)
        for scan_path in scan_paths:
            if scan_path.lower().endswith((".md", ".markdown")):
                found = [scan_path]
            else:
                found = [
                    item for item in workspace.glob("**/*.md", path=scan_path, max_results=per_path_limit)
                    if not item.endswith("/")
                ]
            for file_path in found:
                if file_path not in seen:
                    files.append(file_path)
                    seen.add(file_path)
                if len(files) >= max_files:
                    break
            if len(files) >= max_files:
                break

        lines = [
            "Research progress review",
            f"root: {root}",
            f"scan paths: {', '.join(scan_paths)}",
            f"markdown files scanned: {len(files)}",
        (
            "purpose: find progress that is hard for reviewers/orchestrators to consume, especially "
            "verified-style Markdown proofs whose tail establishes a stronger or more actionable conclusion "
            "than the Statement section explicitly records."
        ),
        (
            "review stopping guide: use this output to choose evidence, not to exhaust the workspace. "
            "After reading the problem or hint if needed, read the selected target or top repair candidate and at most "
            "one competing index or gap file; broaden only when the cited evidence conflicts or misses the user's question."
        ),
        (
            "scoped exploration guide: if several major directories or competing evidence areas matter, use Agent scoped_explorer "
            "on those areas and have the main agent compare their reports before choosing the global next step."
        ),
        (
            "priority guide: separate easy local cleanup from the issue that blocks the most ambitious current claim; "
            "recommend cleanup only when it is the actual global blocker or when an already-proved result must be surfaced first."
        ),
    ]
        if len(files) >= max_files:
            lines.append("note: file list hit max_files; run again on a narrower path if important files may be omitted.")

        workspace_notes = _format_workspace_notes(workspace, root)
        if workspace_notes:
            lines.extend(["", workspace_notes])

        repairable_candidates = _collect_repairable_attempt_candidates(workspace, root)
        top_repairable = repairable_candidates[0] if repairable_candidates and repairable_candidates[0][0] >= 8 else None
        repairable_notes = _format_repairable_attempt_notes(
            workspace,
            root,
            candidates=repairable_candidates,
        )
        if repairable_notes:
            lines.extend(["", repairable_notes])

        unreviewed_candidates = _collect_unreviewed_high_level_attempts(workspace, root)
        top_unreviewed = unreviewed_candidates[0] if unreviewed_candidates and unreviewed_candidates[0][0] >= 6 else None
        unreviewed_notes = _format_unreviewed_high_level_attempts(unreviewed_candidates)
        if unreviewed_notes:
            lines.extend(["", unreviewed_notes])

        unfinished_notes = _format_unfinished_attempt_notes(workspace, root)
        if unfinished_notes:
            lines.extend(["", unfinished_notes])

        candidates = _collect_underclaimed_proof_candidates(workspace, files, max_candidates=60)

        if not candidates:
            if top_unreviewed is not None:
                high_score, high_path, _high_rows, _hint_rows = top_unreviewed
                lines.extend([
                    "",
                    "suggested next proposition: make the high-level attempt's exact interface explicit before a final claim",
                    (
                        f"selected target: {high_path} (high-level signal: {high_score}). "
                        "Read the attempt and worker hint, then state the narrow interface that must be reviewed."
                    ),
                ])
                return ToolResult("\n".join(lines))
            if top_repairable is not None:
                repair_score, repair_path, _repair_rows = top_repairable
                related_files = _repairable_related_files(workspace, repair_path)
                related_text = f" Related files: {', '.join(related_files)}." if related_files else ""
                lines.extend([
                    "",
                    "suggested next proposition: repair the selected failed attempt or state its missing local prerequisite",
                    (
                        f"selected target: {repair_path} (repair signal: {repair_score}).{related_text} "
                        "Read the cited review and proposition before broadening the search."
                    ),
                ])
                return ToolResult("\n".join(lines))
            lines.extend([
                "",
                "possible underclaimed proof statements: none found by the structural scan",
                (
                    "next review move: inspect the proposition index and the most central proof files directly; "
                    "this tool only detects statement-vs-proof-tail mismatches, not every remaining gap."
                ),
            ])
            return ToolResult("\n".join(lines))

        focus_terms = _problem_focus_terms(workspace, root)
        default_candidate = _select_underclaimed_default_candidate(
            candidates,
            focus_terms=focus_terms,
        )
        display_limit = 10 if (top_unreviewed is not None or top_repairable is not None) else 20
        display_candidates = candidates[:display_limit]
        if default_candidate is not None and default_candidate not in display_candidates:
            display_candidates = [default_candidate, *display_candidates[: max(0, display_limit - 1)]]

        lines.extend([
            "",
            _format_underclaimed_proof_candidates(
                display_candidates,
                heading=f"possible underclaimed proof statements: {len(candidates)}",
                include_rule=True,
            ),
        ])
        if default_candidate is not None:
            default_path, _default_highlights = default_candidate
            if top_unreviewed is not None:
                high_score, high_path, _high_rows, _hint_rows = top_unreviewed
                compare_text = ""
                if top_repairable is not None:
                    repair_score, repair_path, _repair_rows = top_repairable
                    compare_text = f" Compare with repairable candidate {repair_path} (repair signal: {repair_score}) after reading the high-level interface."
                lines.extend([
                    "",
                    "suggested next proposition: make the high-level attempt's exact interface explicit before a final claim",
                    (
                        f"selected target: {high_path} (high-level signal: {high_score}).{compare_text} "
                        f"Also compare with underclaimed candidate {default_path} if the interface is already settled."
                    ),
                    (
                        "why selected: broad unreviewed attempts often depend on hidden prerequisites or level changes; "
                        "making the interface reviewable usually prevents shallow final recommendations."
                    ),
                ])
            elif top_repairable is not None:
                repair_score, repair_path, _repair_rows = top_repairable
                related_files = _repairable_related_files(workspace, repair_path)
                related_text = f" Related files: {', '.join(related_files)}." if related_files else ""
                lines.extend([
                    "",
                    "suggested next proposition: repair the selected failed attempt or state its missing local prerequisite",
                    (
                        f"selected target: {repair_path} (repair signal: {repair_score}).{related_text} "
                        f"Compare with underclaimed candidate {default_path} only if the repair no longer matters after reading it."
                    ),
                    (
                        "why selected: a high-signal failed review says the attempt is mostly sound with a localized defect; "
                        "such repairs are usually more actionable than a broad proof-tail consolidation scan."
                    ),
                ])
            elif repairable_notes:
                lines.extend([
                    "",
                    "possible underclaimed consolidation: make the selected proof-tail conclusion explicit in its Statement",
                    (
                        f"selected target: {default_path}. Compare this with the repairable attempt candidates above; "
                        "do not treat either signal as global until you have read the cited files."
                    ),
                    (
                        "next-proposition priority: if the top repairable candidate still matters after that reading, "
                        "prefer repairing or explicitly restating it before choosing a broader new direction."
                    ),
                ])
            else:
                lines.extend([
                    "",
                    "suggested next proposition: make the selected underclaimed proof-tail conclusion explicit in its Statement",
                    (
                        f"selected target: {default_path}. Read it, compare the Statement with the highlighted proof ending, "
                        "and use this suggestion if the tail changes the answer, a best-known objective value or estimate, the stopping condition, or a planning premise."
                    ),
                ])
        return ToolResult("\n".join(lines))
    except Exception as exc:
        return ToolResult(_tool_error(str(exc)), is_error=True)
