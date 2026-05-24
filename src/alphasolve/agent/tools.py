from __future__ import annotations

import json
import re
import subprocess
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .config import AgentConfig
from .workspace import READ_PAGE_DEFAULT_LINES, READ_PAGE_MAX_LINES, WorkspaceLike
from .shell import find_bash_path, has_bash, run_powershell_command


ToolHandler = Callable[[dict[str, Any]], "ToolResult"]


class SubagentDispatcher(Protocol):
    """第二层 Agent 工具的最小调度器协议。

    Why: 第二层不知道也不应该知道 reasoning/compute/numerical 等业务
    subagent 类型；这套方法让第三层 SubagentService 把"业务上的
    subagent 角色"翻译成"第二层认识的调度协议"。
    """
    def available_types(self) -> list[str]: ...
    def describe_type(self, agent_type: str) -> str: ...
    def call(self, agent_type: str, description: str, prompt: str, *, depth: int = ...) -> str: ...


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False
    stop_agent: bool = False
    stop_answer: str | None = None


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler


_MARKDOWN_SECTION_HEADING_RE = re.compile(
    r"^\s{0,3}#{1,6}\s+.*("
    r"statement|proposition|lemma|theorem|claim|corollary|conjecture|"
    r"result|conclusion|summary|progress|insight|finding|next|open"
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
    r"^\s{0,3}#{1,6}\s+.*(statement|proposition|theorem|claim|lemma|corollary).*$",
    re.IGNORECASE,
)
_MARKDOWN_REVIEW_HEADING_RE = re.compile(
    r"^\s{0,3}#{1,6}\s+.*("
    r"current|progress|insight|status|summary|overview|todo|next|recommend|"
    r"remaining|open|gap|block|blocked|warning|fail|failed|review|decision|"
    r"risk|issue|missing|known|unknown"
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


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        *,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: ToolHandler,
    ) -> None:
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = RegisteredTool(
            name=name,
            description=description,
            parameters=parameters,
            handler=handler,
        )

    def tool_defs(
        self,
        enabled: tuple[str, ...] | list[str] | None = None,
        tool_parameters: Mapping[str, Mapping[str, Any]] | None = None,
        tool_descriptions: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> list["ToolDef"]:
        from alphasolve.llm.types import ToolDef
        names = list(enabled) if enabled is not None else list(self._tools)
        missing = [name for name in names if name not in self._tools]
        if missing:
            raise KeyError(f"unknown tools: {missing}")
        constraints = tool_parameters or {}
        descriptions = tool_descriptions or {}
        out: list[ToolDef] = []
        for name in names:
            tool = self._tools[name]
            params = deepcopy(tool.parameters)
            constraint = constraints.get(name)
            if constraint:
                _apply_parameter_constraints(params, constraint)
            description = tool.description
            desc_override = descriptions.get(name)
            if desc_override:
                if "override" in desc_override:
                    description = desc_override["override"]
                elif "suffix" in desc_override:
                    description = description + "\n" + desc_override["suffix"]
                if "parameters" in desc_override:
                    _apply_parameter_description_overrides(params, desc_override["parameters"])
            out.append(ToolDef(name=tool.name, description=description, parameters=params))
        return out

    def registered_tools(self) -> list[RegisteredTool]:
        return list(self._tools.values())

    def execute(
        self,
        name: str,
        args: Mapping[str, Any],
        *,
        enabled: list[str] | None = None,
        tool_parameters: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> ToolResult:
        if enabled is not None and name not in enabled:
            return ToolResult(json.dumps({"error": f"tool is not enabled for this agent: {name}"}, ensure_ascii=False), is_error=True)
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False), is_error=True)
        constraints = (tool_parameters or {}).get(name) or {}
        effective_args = _apply_argument_defaults(args, tool.parameters, constraints)
        error = _validate_tool_arguments(name, effective_args, constraints)
        if error:
            return ToolResult(json.dumps({"error": error}, ensure_ascii=False), is_error=True)
        try:
            return tool.handler(effective_args)
        except Exception as exc:
            return ToolResult(json.dumps({"error": str(exc)}, ensure_ascii=False), is_error=True)


def _apply_parameter_constraints(parameters: dict[str, Any], constraints: Mapping[str, Any]) -> None:
    properties = parameters.setdefault("properties", {})
    if not isinstance(properties, dict):
        return
    for param_name, constraint in constraints.items():
        if not isinstance(constraint, Mapping):
            continue
        prop = properties.setdefault(str(param_name), {})
        if isinstance(prop, dict):
            prop.update(dict(constraint))


def _apply_parameter_description_overrides(parameters: dict[str, Any], overrides: Mapping[str, Mapping[str, str]]) -> None:
    """覆盖某个参数自己的 description 文本。未知参数名直接跳过，避免 YAML 笔误悄悄造出
    没有 type 的空参数 schema。"""
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        return
    for param_name, spec in overrides.items():
        prop = properties.get(str(param_name))
        if isinstance(prop, dict) and "description" in spec:
            prop["description"] = str(spec["description"])


def _validate_tool_arguments(tool_name: str, args: Mapping[str, Any], constraints: Mapping[str, Any]) -> str | None:
    for param_name, constraint in constraints.items():
        if param_name not in args:
            continue
        if not isinstance(constraint, Mapping):
            continue
        error = _validate_value(args[param_name], constraint, path=f"{tool_name}.{param_name}")
        if error:
            return error
    return None


def _apply_argument_defaults(
    args: Mapping[str, Any],
    parameters: Mapping[str, Any],
    constraints: Mapping[str, Any],
) -> dict[str, Any]:
    out = dict(args)
    properties = parameters.get("properties", {})
    if not isinstance(properties, Mapping):
        properties = {}
    names = set(properties) | set(constraints)
    for name in names:
        key = str(name)
        if key in out:
            continue
        merged: dict[str, Any] = {}
        base = properties.get(key)
        if isinstance(base, Mapping):
            merged.update(dict(base))
        constraint = constraints.get(key)
        if isinstance(constraint, Mapping):
            merged.update(dict(constraint))
        if "default" in merged:
            out[key] = deepcopy(merged["default"])
    return out


def _validate_value(value: Any, schema: Mapping[str, Any], *, path: str) -> str | None:
    if "const" in schema and value != schema["const"]:
        return f"{path} must be {schema['const']!r}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} must be one of {list(schema['enum'])!r}"

    schema_type = schema.get("type")
    if schema_type:
        allowed = schema_type if isinstance(schema_type, list) else [schema_type]
        if not any(_matches_json_type(value, str(item)) for item in allowed):
            return f"{path} must have type {allowed!r}"

    for key, check in (
        ("minimum", lambda current, bound: current >= bound),
        ("maximum", lambda current, bound: current <= bound),
        ("exclusiveMinimum", lambda current, bound: current > bound),
        ("exclusiveMaximum", lambda current, bound: current < bound),
    ):
        if key in schema:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return f"{path} must be numeric to satisfy {key}"
            if not check(value, schema[key]):
                return f"{path} violates {key}={schema[key]!r}"

    if "minLength" in schema:
        if not isinstance(value, str) or len(value) < int(schema["minLength"]):
            return f"{path} violates minLength={schema['minLength']!r}"
    if "maxLength" in schema:
        if not isinstance(value, str) or len(value) > int(schema["maxLength"]):
            return f"{path} violates maxLength={schema['maxLength']!r}"
    if "pattern" in schema:
        if not isinstance(value, str) or re.search(str(schema["pattern"]), value) is None:
            return f"{path} must match pattern {schema['pattern']!r}"
    return None


def _matches_json_type(value: Any, schema_type: str) -> bool:
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "object":
        return isinstance(value, Mapping)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "null":
        return value is None
    return True


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


def _strip_markdown_math_noise(text: str) -> str:
    return re.sub(r"\s+", "", text.lower())


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
    normalized_statement = _strip_markdown_math_noise(statement_text)

    tail_window = rows[-min(45, len(rows)):]
    conclusion_rows = [
        (line_no, text)
        for line_no, text in tail_window
        if _MARKDOWN_IMPORTANT_LINE_RE.search(text)
    ]
    novel_rows = [
        (line_no, text)
        for line_no, text in conclusion_rows
        if _strip_markdown_math_noise(text) not in normalized_statement
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
        "If a highlighted tail conclusion changes an answer, stopping condition, or planning premise, "
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
        lines.append("selected theorem/progress sections:")
        for section in selected_sections:
            lines.append(_format_markdown_rows(section, max_chars=5000))

    tail = rows[-tail_lines:]
    important_tail = [(line_no, text) for line_no, text in tail if _MARKDOWN_IMPORTANT_LINE_RE.search(text)]
    if important_tail:
        lines.append("important-looking lines near file end:")
        lines.append(_format_markdown_rows(important_tail, max_chars=4000))
    lines.append(f"file tail (last {min(tail_lines, len(rows))} lines):")
    lines.append(_format_markdown_rows(tail, max_chars=6000))
    return "\n".join(lines)


def _run_inspect_markdown(workspace: WorkspaceLike, args: dict[str, Any]) -> ToolResult:
    path = str(args.get("path", "."))
    max_files = int(args.get("max_files", 20))
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
                "purpose: surface statements, theorem-like/progress sections, and proof endings; "
                "use Read for the full context before relying on a claim."
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
            "stopping criterion, answer, or future planning, the next proposition should normally be an explicit statement of that "
            "already-established conclusion before pursuing harder downstream work. When choosing the next proposition, prefer this "
            "consolidation step unless another already-read Statement explicitly records the same conclusion."
        )
        lines.append("why shown: each listed file has conclusion-like or comparison-heavy proof-tail lines not verbatim in the Statement section.")
    for file_path, highlights in candidates:
        lines.append(f"- {file_path}")
        for line_no, text in highlights:
            lines.append(f"  tail {line_no}: {text}")
    return "\n".join(lines)


def _contains_word(text: str, tokens: tuple[str, ...], *, hyphen_is_boundary: bool = True) -> bool:
    word_chars = "A-Za-z0-9_" if hyphen_is_boundary else "A-Za-z0-9_-"
    for token in tokens:
        pattern = rf"(?<![{word_chars}])" + re.escape(token) + rf"(?![{word_chars}])"
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def _underclaimed_default_candidate_score(file_path: str, highlights: list[tuple[int, str]]) -> int:
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
        hyphen_is_boundary=False,
    ):
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
) -> tuple[str, list[tuple[int, str]]] | None:
    if not candidates:
        return None
    verified_candidates = [
        item for item in candidates
        if item[0].replace("\\", "/").startswith("verified_propositions/")
    ]
    search_pool = verified_candidates or candidates
    selected = max(search_pool, key=lambda item: _underclaimed_default_candidate_score(item[0], item[1]))
    if _underclaimed_default_candidate_score(selected[0], selected[1]) < 15:
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
        sections = _extract_review_sections(rows, max_sections=3, max_lines_per_section=24)
        review_lines = _extract_review_lines(rows, max_lines=10)
        if not sections and not review_lines:
            review_lines = rows[: min(12, len(rows))]
        lines.append(f"- {file_path}")
        if sections:
            for section in sections:
                lines.append(_format_markdown_rows(section, max_chars=2400))
        elif review_lines:
            lines.append(_format_markdown_rows(review_lines, max_chars=1600))
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
        ]
        if len(files) >= max_files:
            lines.append("note: file list hit max_files; run again on a narrower path if important files may be omitted.")

        workspace_notes = _format_workspace_notes(workspace, root)
        if workspace_notes:
            lines.extend(["", workspace_notes])

        unfinished_notes = _format_unfinished_attempt_notes(workspace, root)
        if unfinished_notes:
            lines.extend(["", unfinished_notes])

        candidates = _collect_underclaimed_proof_candidates(workspace, files, max_candidates=20)

        if not candidates:
            lines.extend([
                "",
                "possible underclaimed proof statements: none found by the structural scan",
                (
                    "next review move: inspect the proposition index and the most central proof files directly; "
                    "this tool only detects statement-vs-proof-tail mismatches, not all mathematical gaps."
                ),
            ])
            return ToolResult("\n".join(lines))

        lines.extend([
            "",
            _format_underclaimed_proof_candidates(
                candidates,
                heading=f"possible underclaimed proof statements: {len(candidates)}",
                include_rule=True,
            ),
        ])
        default_candidate = _select_underclaimed_default_candidate(candidates)
        if default_candidate is not None:
            default_path, _default_highlights = default_candidate
            lines.extend([
                "",
                "suggested next proposition: make the selected underclaimed proof-tail conclusion explicit in its Statement",
                (
                    f"selected target: {default_path}. Read it, compare the Statement with the highlighted proof ending, "
                    "and only use this suggestion if the tail changes the answer, stopping condition, or planning premise."
                ),
            ])
        return ToolResult("\n".join(lines))
    except Exception as exc:
        return ToolResult(_tool_error(str(exc)), is_error=True)


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
            "For progress review, consider ResearchProgressReview before deciding what is already known or what to do next; "
            "it looks for verified-style proof files whose conclusions are buried in proof tails rather than explicit statements. "
            "If such a buried conclusion affects the answer, stopping criterion, or planning, surface it as an explicit proposition before harder new work.</system>"
        )
    return text


def build_default_tool_registry(
    workspace: WorkspaceLike,
    *,
    bash_timeout_seconds: int = 120,
) -> ToolRegistry:
    """构造第二层默认 ToolRegistry，注册通用 coding agent 基础工具。

    第三层用法：传入 RoleWorkspaceAccess 实例（满足 WorkspaceLike），即可自动
    获得带角色权限检查的基础工具。Bash 在第二层不做命令白名单——具体安全策略
    由调用方通过 YAML 工具白名单或自定义 handler 控制。
    """
    import datetime

    registry = ToolRegistry()

    # Read
    def _run_read(args: dict[str, Any]) -> ToolResult:
        try:
            result = workspace.read_text_page(
                args["path"],
                line_offset=int(args.get("line_offset", 1)),
                n_lines=int(args.get("n_lines", READ_PAGE_DEFAULT_LINES)),
                read_all=bool(args.get("read_all", False)),
            )
        except Exception as exc:
            return ToolResult(f"<system>ERROR reading {args['path']}: {exc}</system>", is_error=True)
        system = f"path: {args['path']}\n{result.message}"
        hint = _markdown_read_review_hint(str(args["path"]), result.output, result.message)
        index_audit_hint = _markdown_index_progress_audit_hint(workspace, str(args["path"]))
        if not result.output:
            return ToolResult(f"<system>{system}</system>")
        parts = [f"<system>{system}</system>", result.output]
        if hint:
            parts.append(hint)
        if index_audit_hint:
            parts.append(index_audit_hint)
        return ToolResult("\n".join(parts))

    registry.register(
        name="Read",
        description=(
            "Read text content from a file.\n\n"
            "Tips:\n"
            "- A `<system>` tag will be given before the read file content.\n"
            "- The system will notify you when there is anything wrong when reading the file.\n"
            "- This tool is typically worth using in parallel when you need to inspect multiple files.\n"
            "- If you want to search for a certain content or pattern, prefer Grep over Read.\n"
            "- Content will be returned with a line number before each line like `cat -n` format.\n"
            f"- By default, Read returns {READ_PAGE_DEFAULT_LINES} lines.\n"
            "- `line_offset` is the first line to return.\n"
            f"- `n_lines` is how many lines to return in this call; default is {READ_PAGE_DEFAULT_LINES}.\n"
            "- Set `read_all=true` to ignore `n_lines` and read from `line_offset` to the end of the file.\n"
            f"- Without `read_all`, the maximum `n_lines` value is {READ_PAGE_MAX_LINES}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The path to the file to read."},
                "line_offset": {
                    "type": "integer",
                    "default": 1,
                    "minimum": 1,
                    "description": "The line number to start reading from.",
                },
                "n_lines": {
                    "type": "integer",
                    "default": READ_PAGE_DEFAULT_LINES,
                    "minimum": 1,
                    "maximum": READ_PAGE_MAX_LINES,
                    "description": f"How many lines to return. Defaults to {READ_PAGE_DEFAULT_LINES}, max {READ_PAGE_MAX_LINES}.",
                },
                "read_all": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, ignore n_lines and read to end of file.",
                },
            },
            "required": ["path"],
        },
        handler=_run_read,
    )

    # Write
    registry.register(
        name="Write",
        description=(
            "Writes a file to the workspace.\n\n"
            "Usage:\n"
            "- Use `mode=\"overwrite\"` to replace the whole file.\n"
            "- Use `mode=\"append\"` to append content to the end of the file."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The path to the file to write."},
                "content": {"type": "string", "description": "The content to write to the file."},
                "mode": {
                    "type": "string",
                    "enum": ["overwrite", "append"],
                    "default": "overwrite",
                    "description": "Whether to overwrite or append.",
                },
            },
            "required": ["path", "content"],
        },
        handler=lambda args: ToolResult(
            json.dumps(
                {"path": workspace.write_text(args["path"], args["content"], mode=str(args.get("mode", "overwrite")))},
                ensure_ascii=False,
            )
        ),
    )

    # Edit
    registry.register(
        name="Edit",
        description=(
            "Performs exact string replacements in files.\n\n"
            "Usage:\n"
            "- The edit will FAIL if old_str is not unique in the file.\n"
            "- Provide a larger string with more surrounding context to make it unique."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The path to the file to modify."},
                "old_str": {"type": "string", "description": "The text to replace."},
                "new_str": {"type": "string", "description": "The text to replace it with (must be different from old_str)."},
            },
            "required": ["path", "old_str", "new_str"],
        },
        handler=lambda args: ToolResult(
            json.dumps({"path": workspace.edit(args["path"], args["old_str"], args["new_str"])}, ensure_ascii=False)
        ),
    )

    # MakeDir
    registry.register(
        name="MakeDir",
        description="Creates a directory in the workspace, including parent directories when needed.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path to create."},
            },
            "required": ["path"],
        },
        handler=lambda args: ToolResult(
            json.dumps({"path": workspace.make_dir(args["path"])}, ensure_ascii=False)
        ),
    )

    # Rename
    registry.register(
        name="Rename",
        description=(
            "Renames a file or directory in place.\n\n"
            "Usage:\n"
            "- Use this only when the item stays in the same directory and only its name changes.\n"
            "- `directory` is the parent directory that currently contains the item.\n"
            "- `old_name` and `new_name` must be plain names, not paths.\n"
            "- To move a file into another directory, use Move instead."
        ),
        parameters={
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Parent directory containing the item to rename."},
                "old_name": {"type": "string", "description": "Current name only; no path separators."},
                "new_name": {"type": "string", "description": "New name only; no path separators."},
            },
            "required": ["directory", "old_name", "new_name"],
        },
        handler=lambda args: ToolResult(
            json.dumps(workspace.rename_item(args["directory"], args["old_name"], args["new_name"]), ensure_ascii=False)
        ),
    )

    # Move
    registry.register(
        name="Move",
        description=(
            "Moves a file into another directory while keeping the same file name.\n\n"
            "Usage:\n"
            "- `path` must be an existing file.\n"
            "- `destination_dir` must be an existing directory."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Existing file path to move."},
                "destination_dir": {"type": "string", "description": "Existing destination directory."},
            },
            "required": ["path", "destination_dir"],
        },
        handler=lambda args: ToolResult(
            json.dumps(workspace.move_file(args["path"], args["destination_dir"]), ensure_ascii=False)
        ),
    )

    # Delete
    registry.register(
        name="Delete",
        description=(
            "Deletes a file or empty directory.\n\n"
            "Usage:\n"
            "- Directories must already be empty; this tool will not recursively delete."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file or empty directory path to delete."},
            },
            "required": ["path"],
        },
        handler=lambda args: ToolResult(
            json.dumps({"path": workspace.delete_path(args["path"])}, ensure_ascii=False)
        ),
    )

    # Glob
    registry.register(
        name="Glob",
        description=(
            "Fast file pattern matching tool. Supports glob patterns like ``**/*.md`` or ``src/**/*.py``, or ``*`` to list directory contents.\n\n"
            "Usage:\n"
            "- Returns plain text with one path per line."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "The glob pattern to match files against."},
                "path": {"type": "string", "description": "The directory to search in.", "default": "."},
                "max_results": {"type": "integer", "description": "Maximum results to return.", "default": 100},
            },
            "required": ["pattern"],
        },
        handler=lambda args: ToolResult(_format_list_result(
            "Glob results",
            workspace.glob(args["pattern"], path=args.get("path", "."), max_results=int(args.get("max_results", 100))),
            max_results=int(args.get("max_results", 100)),
        )),
    )

    # ListDir
    registry.register(
        name="ListDir",
        description=(
            "Lists files and directories under a workspace directory.\n\n"
            "Usage:\n"
            "- Returns plain text with one entry per line; directories end with `/`.\n"
            "- If the directory contains `index.md`, read `index.md` first before exploring other files."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory to list.", "default": "."},
                "max_results": {"type": "integer", "description": "Maximum entries to return.", "default": 200},
            },
            "required": [],
        },
        handler=lambda args: ToolResult(_format_list_dir_result(
            args.get("path", "."),
            workspace.list_dir(args.get("path", "."), max_results=int(args.get("max_results", 200))),
            max_results=int(args.get("max_results", 200)),
        )),
    )

    # ResearchProgressReview
    registry.register(
        name="ResearchProgressReview",
        description=(
            "Audit a Markdown research/proof workspace before deciding current progress or next propositions.\n\n"
            "Usage:\n"
            "- Use this early when a workspace has problem.md, knowledge notes, and verified propositions.\n"
            "- It scans Markdown proof files for a general failure mode: the proof tail establishes a stronger or more actionable conclusion than the Statement section records.\n"
            "- If it reports an underclaimed proof that affects an answer, stopping condition, or planning premise, the next proposition should normally make that stronger conclusion explicit as a Statement before pursuing harder work.\n"
            "- This is a progress-navigation tool, not a verifier; it does not prove new claims."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace root or directory to audit.", "default": "."},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional specific files/directories to scan instead of auto-detecting research folders.",
                    "default": [],
                },
                "max_files": {"type": "integer", "description": "Maximum Markdown files to scan.", "default": 120, "minimum": 1, "maximum": 500},
            },
            "required": [],
        },
        handler=lambda args: _run_research_progress_review(workspace, args),
    )

    # InspectMarkdown
    registry.register(
        name="InspectMarkdown",
        description=(
            "Survey Markdown files and surface the parts most likely to summarize research progress.\n\n"
            "Usage:\n"
            "- Use this when reviewing a notes/proofs workspace before deciding what is already known or what to do next.\n"
            "- It lists headings, extracts statement/progress sections, and always shows the file tail because conclusions are often buried near proof endings.\n"
            "- Its progress audit highlights underclaimed proof tails; if one affects an answer, stopping condition, or planning premise, make that conclusion explicit as a proposition statement before harder new work.\n"
            "- This is a navigation and review aid, not a verifier; use Read on the cited file/lines before relying on a claim."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Markdown file or directory to inspect.", "default": "."},
                "max_files": {"type": "integer", "description": "Maximum Markdown files to inspect when path is a directory.", "default": 20, "minimum": 1, "maximum": 100},
                "tail_lines": {"type": "integer", "description": "How many ending lines to show for each file.", "default": 40, "minimum": 1, "maximum": 200},
                "statement_lines": {"type": "integer", "description": "Maximum lines to show from each selected theorem/progress section.", "default": 80, "minimum": 1, "maximum": 300},
            },
            "required": [],
        },
        handler=lambda args: _run_inspect_markdown(workspace, args),
    )

    # Grep
    registry.register(
        name="Grep",
        description=(
            "Searches readable text files under a specific file or directory.\n\n"
            "Usage:\n"
            "- Prefer Grep for exact symbol/string searches.\n"
            "- Returns plain text in `path:line: text` format, optionally with context.\n"
            "- `regex` defaults to true; set `regex=false` to force plain substring matching."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "The text or regex pattern to search for."},
                "path": {"type": "string", "description": "File or directory to search in.", "default": "."},
                "regex": {"type": "boolean", "description": "Treat pattern as regex (default true).", "default": True},
                "max_results": {"type": "integer", "description": "Maximum results to return.", "default": 50},
                "context_lines": {"type": "integer", "description": "Lines of context around each match. Set 0 to disable.", "default": 0},
            },
            "required": ["pattern"],
        },
        handler=lambda args: ToolResult(_format_grep_result(
            workspace.grep(
                args["pattern"],
                path=args.get("path", "."),
                regex=bool(args.get("regex", True)),
                max_results=int(args.get("max_results", 50)),
                context_lines=int(args.get("context_lines", 0)),
            ),
            pattern=args["pattern"],
            path=args.get("path", "."),
            max_results=int(args.get("max_results", 50)),
        )),
    )

    # GetCurrentTime
    registry.register(
        name="GetCurrentTime",
        description="Returns the current date and time in ISO 8601 format.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=lambda _args: ToolResult(
            json.dumps({"datetime": datetime.datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False)
        ),
    )

    # Bash / Shell (按平台二选一)
    if has_bash():
        def _run_bash(args: dict[str, Any]) -> ToolResult:
            command = str(args.get("command") or "")
            if not command.strip():
                return ToolResult("[error] empty command", is_error=True)
            try:
                bash_path = find_bash_path()
                if bash_path is None:
                    return ToolResult("[error] bash not found", is_error=True)
                result = subprocess.run(
                    [str(bash_path), "-lc", command],
                    cwd=str(workspace.root),
                    text=True,
                    capture_output=True,
                    timeout=bash_timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                return ToolResult(f"[error] command timed out after {bash_timeout_seconds} seconds", is_error=True)
            parts = [f"[exit_code]\n{result.returncode}"]
            if result.stdout:
                parts.append(f"[stdout]\n{result.stdout}")
            if result.stderr:
                parts.append(f"[stderr]\n{result.stderr}")
            return ToolResult("\n".join(parts), is_error=result.returncode != 0)

        registry.register(
            name="Bash",
            description="Executes a bash command at the workspace root.",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bash command to execute."},
                },
                "required": ["command"],
            },
            handler=_run_bash,
        )
    else:
        def _run_shell(args: dict[str, Any]) -> ToolResult:
            command = str(args.get("command") or "")
            if not command.strip():
                return ToolResult("[error] empty command", is_error=True)
            try:
                result = run_powershell_command(
                    command,
                    cwd=str(workspace.root),
                    timeout=bash_timeout_seconds,
                )
            except FileNotFoundError as exc:
                return ToolResult(f"[error] {exc}", is_error=True)
            except subprocess.TimeoutExpired:
                return ToolResult(f"[error] command timed out after {bash_timeout_seconds} seconds", is_error=True)
            parts = [f"[exit_code]\n{result.returncode}"]
            if result.stdout:
                parts.append(f"[stdout]\n{result.stdout}")
            if result.stderr:
                parts.append(f"[stderr]\n{result.stderr}")
            return ToolResult("\n".join(parts), is_error=result.returncode != 0)

        registry.register(
            name="Shell",
            description=(
                "Executes a PowerShell command at the workspace root.\n\n"
                "IMPORTANT: Prefer dedicated tools for file operations:\n"
                "- Directory listing: Use ListDir\n"
                "- File search: Use Glob\n"
                "- Content search: Use Grep\n"
                "- Read files: Use Read\n"
                "- Edit files: Use Edit\n"
                "- Write files: Use Write"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The PowerShell command to execute."},
                },
                "required": ["command"],
            },
            handler=_run_shell,
        )

    return registry


def register_agent_tool(
    registry: ToolRegistry,
    *,
    agent_config: AgentConfig,
    dispatcher: SubagentDispatcher,
    depth: int = 0,
) -> None:
    """Register the Agent tool with a description and enum scoped to this agent's permissions.

    第三层（或其他实现）通过提供 SubagentDispatcher 实例注入业务 subagent 类型清单。
    """
    if "Agent" not in agent_config.tools:
        return
    agent_tool_params = agent_config.tool_parameters.get("Agent", {})
    type_constraint = agent_tool_params.get("type", {})
    allowed_types = type_constraint.get("enum", dispatcher.available_types())
    known = set(dispatcher.available_types())
    allowed_types = [t for t in allowed_types if t in known]
    if not allowed_types:
        return
    lines = [
        "Launch a new specialized agent and return its final report.",
        "",
        "The launched agent runs in a separate session. It does not see this conversation unless you include the needed context in `prompt`.",
        "",
        "Available agent types:",
    ]
    for stype in allowed_types:
        when = dispatcher.describe_type(stype)
        lines.append(f"- {stype}: {when}")
    lines.extend([
        "",
        "## Writing the prompt",
        "",
        "- State the exact mathematical claim, definitions, assumptions, and what needs to be proved or checked.",
        "- Include relevant context: what's already known, what approaches have been tried, which files to reference, and why this task matters.",
        "- Give enough context that the agent can make judgment calls rather than just following a narrow instruction.",
        "- Keep the task self-contained and bounded to the agent's scope.",
    ])

    def _handler(args: dict[str, Any]) -> ToolResult:
        agent_type = str(args.get("type") or "")
        description = str(args.get("description") or "")
        prompt = str(args.get("prompt") or "")
        if not prompt.strip():
            return ToolResult("ERROR: prompt must not be empty", is_error=True)
        try:
            text = dispatcher.call(agent_type, description, prompt, depth=depth)
            return ToolResult(text)
        except Exception as exc:
            return ToolResult(f"ERROR: {exc}", is_error=True)

    registry.register(
        name="Agent",
        description="\n".join(lines),
        parameters={
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": allowed_types, "description": "The type of specialized agent to use for this task."},
                "description": {"type": "string", "description": "A short (3-5 word) description of the task."},
                "prompt": {"type": "string", "description": "The task for the agent to perform."},
            },
            "required": ["type", "description", "prompt"],
        },
        handler=_handler,
    )
