"""Convert AlphaSolve .log files into a human-friendly, collapsible HTML report.

Usage (from repo root):
  python log_to_html.py logs/20251231_160002_350.log

Output:
  log_as_html/<same_name>.html

This parser is tailored to the project's logging conventions:
- File logger format: "YYYY-mm-dd HH:MM:SS.mmm │ LEVEL │ <message>"
- Multi-line messages are stored as a single log record whose continuation lines
  have NO timestamp prefix.
- LLM streaming buffers are logged as blocks:
    [本轮思维链]\n...
    [本轮回答]\n...
    [思维链中工具调用]\n[Tool Call] ...\n...

The HTML groups by big agents (solver/verifier/refiner/summarizer) and nests
subagent runs when detected.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import html
import os
import re
from typing import Dict, List, Optional, Tuple, Union


BIG_AGENTS = ("solver", "verifier", "refiner", "summarizer")


_TS_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+│\s+"
    r"(?P<level>[A-Z]+)\s+│\s+(?P<msg>.*)$"
)

# Matches leading "📝 [solver] ..." etc.
_MODULE_TAG_RE = re.compile(r"\[(?P<tag>[A-Za-z_]+)\]")

_THOUGHT_MARKERS = ("[本轮思维链]", "[思维链内容]")
_ANSWER_MARKERS = ("[本轮回答]", "[最终回答]")
_TOOL_MARKER = "[思维链中工具调用]"


@dataclasses.dataclass
class LogRecord:
    ts: Optional[str]  # keep original string; may be None for file header lines
    level: Optional[str]
    msg: str

    def first_line(self) -> str:
        return self.msg.splitlines()[0] if self.msg else ""


@dataclasses.dataclass
class ToolCallBlock:
    name: str
    body: str


@dataclasses.dataclass
class RenderBlock:
    """A typed block for rendering."""

    kind: str  # thought | answer | toolcalls | logline
    ts: Optional[str]
    level: Optional[str]
    title: str
    content: str
    tool_calls: Optional[List[ToolCallBlock]] = None


@dataclasses.dataclass
class Section:
    """Hierarchical section for HTML output."""

    title: str
    kind: str  # agent | subagent | system | toolcall
    # IMPORTANT: must preserve the original chronological order.
    # We store a single ordered stream of items (blocks and nested sections).
    items: List[Union[RenderBlock, "Section"]] = dataclasses.field(default_factory=list)
    meta: Dict[str, str] = dataclasses.field(default_factory=dict)

    def add_child(self, sec: "Section") -> "Section":
        self.items.append(sec)
        return sec

    def add_block(self, blk: RenderBlock) -> None:
        self.items.append(blk)


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def parse_log_records(text: str) -> Tuple[List[str], List[LogRecord]]:
    """Parse file into (header_lines, records)."""
    header_lines: List[str] = []
    records: List[LogRecord] = []

    current: Optional[LogRecord] = None

    for raw_line in text.splitlines():
        m = _TS_LINE_RE.match(raw_line)
        if m:
            # flush previous
            if current is not None:
                records.append(current)
            current = LogRecord(ts=m.group("ts"), level=m.group("level"), msg=m.group("msg"))
        else:
            # header / separator lines or continuation lines
            if current is None:
                header_lines.append(raw_line)
            else:
                current.msg += "\n" + raw_line

    if current is not None:
        records.append(current)

    # Trim trailing blank lines in header for nicer rendering
    while header_lines and not header_lines[-1].strip():
        header_lines.pop()
    return header_lines, records


def _detect_module_tag(msg: str) -> Optional[str]:
    m = _MODULE_TAG_RE.search(msg)
    if not m:
        return None
    return (m.group("tag") or "").strip()


def _strip_emoji_prefix(msg: str) -> str:
    # Common: "📝 [solver] ..." or "✅ [verifier] ..." etc.
    # We only strip the first token if it *looks* like emoji + space.
    if len(msg) >= 2 and msg[1] == " ":
        return msg[2:]
    return msg


def _is_block_start(record: LogRecord, markers: Tuple[str, ...]) -> Optional[str]:
    first = record.first_line().strip()
    for mk in markers:
        if first.startswith(mk):
            return mk
    return None


def parse_tool_calls(toolcalls_message: str) -> List[ToolCallBlock]:
    """Parse the content after "[思维链中工具调用]" into tool call blocks."""
    # Keep original newlines.
    lines = toolcalls_message.splitlines()

    # Drop leading marker line if present.
    if lines and lines[0].strip().startswith(_TOOL_MARKER):
        lines = lines[1:]

    blocks: List[ToolCallBlock] = []
    current_name: Optional[str] = None
    current_lines: List[str] = []

    def flush():
        nonlocal current_name, current_lines
        if current_name is None:
            return
        body = "\n".join(current_lines).strip("\n")
        blocks.append(ToolCallBlock(name=current_name, body=body))
        current_name = None
        current_lines = []

    for ln in lines:
        m = re.match(r"^\[Tool Call\]\s+(?P<name>.+?)\s*$", ln.strip())
        if m:
            flush()
            current_name = (m.group("name") or "").strip()
            current_lines = []
            continue
        # Otherwise part of current block (or prelude)
        if current_name is None:
            # prelude noise
            continue
        current_lines.append(ln)

    flush()
    return blocks


def classify_record(record: LogRecord) -> RenderBlock:
    first = record.first_line().strip()

    mk = _is_block_start(record, _THOUGHT_MARKERS)
    if mk is not None:
        body = record.msg
        # Strip the marker itself from content if present.
        if body.startswith(mk):
            body = body[len(mk) :]
            if body.startswith("\n"):
                body = body[1:]
        return RenderBlock(
            kind="thought",
            ts=record.ts,
            level=record.level,
            title=f"Thought {mk}",
            content=body,
        )

    mk = _is_block_start(record, _ANSWER_MARKERS)
    if mk is not None:
        body = record.msg
        if body.startswith(mk):
            body = body[len(mk) :]
            if body.startswith("\n"):
                body = body[1:]
        return RenderBlock(
            kind="answer",
            ts=record.ts,
            level=record.level,
            title=f"Answer {mk}",
            content=body,
        )

    if first.startswith(_TOOL_MARKER):
        tcs = parse_tool_calls(record.msg)
        # Improve UX: if there is exactly one tool call in this record, use the tool
        # name as the title and render only that tool body.
        if len(tcs) == 1:
            tc0 = tcs[0]
            return RenderBlock(
                kind="toolcalls",
                ts=record.ts,
                level=record.level,
                title=tc0.name,
                content=f"[Tool Call] {tc0.name}\n{tc0.body}".strip("\n"),
                tool_calls=tcs,
            )

        return RenderBlock(
            kind="toolcalls",
            ts=record.ts,
            level=record.level,
            title=_TOOL_MARKER,
            content=record.msg,
            tool_calls=tcs,
        )

    return RenderBlock(
        kind="logline",
        ts=record.ts,
        level=record.level,
        title="log",
        content=record.msg,
    )


def build_sections(header_lines: List[str], records: List[LogRecord]) -> Section:
    root = Section(title="AlphaSolve Log", kind="root")

    if header_lines:
        root.add_block(
            RenderBlock(
                kind="logline",
                ts=None,
                level=None,
                title="header",
                content="\n".join(header_lines).strip("\n"),
            )
        )

    # We render a strict time-ordered *timeline* of sections.
    # Each contiguous run of a big agent becomes its own section (Solver #1, Verifier #1, Solver #2, ...).
    system = root.add_child(Section(title="System #1", kind="system"))

    agent_counts: Dict[str, int] = {k: 0 for k in BIG_AGENTS}
    system_count = 1

    current_agent: Section = system
    current_agent_key: str = "system"
    stack: List[Section] = [current_agent]
    pending_subagent_tool: Optional[Section] = None

    def infer_agent_from_message(tag_l: Optional[str], msg: str) -> Optional[str]:
        """Best-effort agent inference.

        Notes:
        - Many LLMClient blocks (e.g. "[本轮思维链]") have no module tag.
          We must attribute them to the currently executing big agent.
        - Some solver logs (e.g. "final solver prompt is:") also have no [solver] tag.
        """
        if tag_l in BIG_AGENTS:
            return tag_l
        low = (msg or "").lower()
        if "final solver prompt is" in low:
            return "solver"
        return None

    def current_ctx() -> Section:
        return stack[-1]

    def in_subagent() -> bool:
        return any(s.kind == "subagent" for s in stack)

    def start_agent_segment(agent_key: str) -> Section:
        nonlocal system_count
        if agent_key == "system":
            system_count += 1
            return root.add_child(Section(title=f"System #{system_count}", kind="system"))
        agent_counts[agent_key] = agent_counts.get(agent_key, 0) + 1
        return root.add_child(Section(title=f"{agent_key.capitalize()} #{agent_counts[agent_key]}", kind="agent"))

    for rec in records:
        msg0 = rec.first_line()
        tag = _detect_module_tag(_strip_emoji_prefix(msg0) if msg0 else "")
        tag_l = tag.lower() if tag else None

        # Switch big-agent context unless we're currently inside a subagent.
        agent_hint = infer_agent_from_message(tag_l, rec.msg)
        if agent_hint in BIG_AGENTS and not in_subagent() and current_agent_key != agent_hint:
            current_agent = start_agent_segment(agent_hint)
            current_agent_key = agent_hint
            stack = [current_agent]
            pending_subagent_tool = None

        # Start subagent session when we see the entering line.
        # (This happens BEFORE the parent tool summary record is written.)
        blk = classify_record(rec)

        # Start subagent session when we see the entering line.
        # We MUST insert the tool-call node into the agent's stream *here* to preserve ordering:
        # thought -> toolcall -> subagent(thought/toolcalls/answer) -> thought ...
        if (tag_l == "subagent") and ("entering subagent" in rec.msg) and (not in_subagent()):
            tool = Section(title="Tool call: math_research_subagent", kind="toolcall")
            # Add the "entering" line as part of the tool call (outside the subagent content).
            tool.add_block(blk)
            current_ctx().add_child(tool)

            sub = tool.add_child(Section(title="Subagent", kind="subagent"))
            pending_subagent_tool = tool
            stack.append(sub)
            continue

        # If we are inside a subagent and the parent tool summary arrives (it contains Tool Call math_research_subagent),
        # attach it to the placeholder tool section and pop back to parent.
        if in_subagent() and blk.kind == "toolcalls" and blk.tool_calls:
            if any(tc.name.strip() == "math_research_subagent" for tc in blk.tool_calls):
                # Attach summary to the tool section.
                if pending_subagent_tool is not None:
                    pending_subagent_tool.add_block(blk)
                else:
                    # Fallback: attach where we are.
                    current_ctx().add_block(blk)
                # Pop subagent context
                stack = [current_agent]
                pending_subagent_tool = None
                continue

        # Default attach: toolcall summaries are blocks under current context, unless they are math_research_subagent outside subagent.
        if blk.kind == "toolcalls" and blk.tool_calls:
            # Create child toolcall sections for each tool call, so they can be folded individually.
            for tc in blk.tool_calls:
                if tc.name.strip() == "math_research_subagent" and not in_subagent():
                    tool = Section(title="Tool call: math_research_subagent", kind="toolcall")
                    tool.add_block(
                        RenderBlock(
                            kind="toolcalls",
                            ts=blk.ts,
                            level=blk.level,
                            title="math_research_subagent",
                            content=f"[Tool Call] {tc.name}\n{tc.body}".strip("\n"),
                            tool_calls=[tc],
                        )
                    )
                    current_ctx().add_child(tool)
                else:
                    # Normal tool calls (run_python/run_wolfram/solver_format_guard/...) as nested blocks.
                    tool = Section(title=f"Tool call: {tc.name}", kind="toolcall")
                    tool.add_block(
                        RenderBlock(
                            kind="toolcalls",
                            ts=blk.ts,
                            level=blk.level,
                            title=tc.name,
                            content=f"[Tool Call] {tc.name}\n{tc.body}".strip("\n"),
                            tool_calls=[tc],
                        )
                    )
                    current_ctx().add_child(tool)
            continue

        # For regular blocks, attach to current context.
        current_ctx().add_block(blk)

    return root


def _esc(s: str) -> str:
    return html.escape(s, quote=False)


def _fmt_ts(ts: Optional[str]) -> str:
    if not ts:
        return ""
    return ts


def _render_block(blk: RenderBlock) -> str:
    ts = _fmt_ts(blk.ts)
    level = (blk.level or "").strip()

    if blk.kind in ("thought", "answer"):
        # collapsed by default
        title = f"{blk.title}{' — ' + ts if ts else ''}"
        return (
            f"<details class=\"blk {blk.kind}\">"
            f"<summary><span class=\"sumTitle\">{_esc(title)}</span></summary>"
            f"<pre class=\"content\">{_esc(blk.content)}</pre>"
            f"</details>"
        )

    if blk.kind == "toolcalls":
        # This blk may represent one tool call; render the raw body.
        title = f"Tool call{': ' + blk.title if blk.title else ''}{' — ' + ts if ts else ''}"
        body = blk.content
        return (
            f"<details class=\"blk toolcalls\">"
            f"<summary><span class=\"sumTitle\">{_esc(title)}</span></summary>"
            f"<pre class=\"content\">{_esc(body)}</pre>"
            f"</details>"
        )

    # logline: if it's long or multi-line, make it foldable.
    msg = blk.content or ""
    is_multiline = "\n" in msg
    is_long = len(msg) > 240
    if is_multiline or is_long:
        first = msg.splitlines()[0] if msg else ""
        # Keep summary short for very long first lines.
        if len(first) > 160:
            first = first[:160] + " …"
        summary = " ".join(x for x in [ts, level, first] if x)
        return (
            f"<details class=\"blk loglineFold level-{_esc(level)}\">"
            f"<summary><span class=\"sumTitle\">{_esc(summary)}</span></summary>"
            f"<pre class=\"content\">{_esc(msg)}</pre>"
            f"</details>"
        )

    return (
        f"<div class=\"logline level-{_esc(level)}\">"
        f"<span class=\"ts\">{_esc(ts)}</span>"
        f"<span class=\"lvl\">{_esc(level)}</span>"
        f"<span class=\"msg\">{_esc(msg)}</span>"
        f"</div>"
    )


def _render_section(sec: Section, *, open_default: bool = True) -> str:
    # Root is rendered outside.
    classes = f"sec {sec.kind}"
    open_attr = " open" if open_default and sec.kind in ("agent", "system") else ""
    parts: List[str] = []

    parts.append(f"<details class=\"{classes}\"{open_attr}>")
    parts.append(f"<summary><span class=\"secTitle\">{_esc(sec.title)}</span></summary>")
    parts.append("<div class=\"secBody\">")

    for it in sec.items:
        if isinstance(it, RenderBlock):
            parts.append(_render_block(it))
        else:
            child = it
            # Toolcall sections are collapsed by default, everything else open.
            child_open = child.kind in ("agent", "system")
            parts.append(_render_section(child, open_default=child_open))

    parts.append("</div>")
    parts.append("</details>")
    return "\n".join(parts)


def render_html(root: Section, source_log_path: str) -> str:
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = f"AlphaSolve Log — {os.path.basename(source_log_path)}"

    # Render: root.blocks (header) + root.children sections.
    body_parts: List[str] = []
    body_parts.append(
        "<div class=\"topbar\">"
        f"<div class=\"title\">{_esc(title)}</div>"
        f"<div class=\"meta\">Generated: {_esc(now)} | Source: {_esc(source_log_path)}</div>"
        "<div class=\"actions\">"
        "<button onclick=\"toggleAll(true)\">Expand all</button>"
        "<button onclick=\"toggleAll(false)\">Collapse all</button>"
        "</div>"
        "</div>"
    )

    # Root header (if any) as a folded block
    # Root header blocks
    for it in root.items:
        if not isinstance(it, RenderBlock):
            continue
        blk = it
        body_parts.append(
            "<details class=\"sec header\">"
            "<summary><span class=\"secTitle\">Log header</span></summary>"
            f"<pre class=\"content\">{_esc(blk.content)}</pre>"
            "</details>"
        )

    # Root sections
    for it in root.items:
        if isinstance(it, Section):
            body_parts.append(_render_section(it, open_default=True))

    css = _CSS
    js = _JS
    return (
        "<!doctype html>\n"
        "<html lang=\"zh\">\n"
        "<head>\n"
        "  <meta charset=\"utf-8\"/>\n"
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>\n"
        f"  <title>{_esc(title)}</title>\n"
        f"  <style>\n{css}\n  </style>\n"
        "</head>\n"
        "<body>\n"
        f"{''.join(body_parts)}\n"
        f"<script>\n{js}\n</script>\n"
        "</body>\n"
        "</html>\n"
    )


_CSS = r"""
:root{
  --bg:#0b1020;
  --panel:#111a33;
  --panel2:#0f1730;
  --text:#e7ecff;
  --muted:#aab3d6;
  --border:#24305a;
  --accent:#7aa2ff;
  --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
  --sans: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "Apple Color Emoji","Segoe UI Emoji";
}
body{ margin:0; background:var(--bg); color:var(--text); font-family:var(--sans); }
.topbar{
  position:sticky; top:0; z-index:10;
  background:linear-gradient(180deg, rgba(17,26,51,.98), rgba(17,26,51,.85));
  border-bottom:1px solid var(--border);
  padding:14px 16px;
}
.topbar .title{ font-size:16px; font-weight:700; }
.topbar .meta{ font-size:12px; color:var(--muted); margin-top:4px; }
.actions{ margin-top:10px; display:flex; gap:8px; flex-wrap:wrap; }
button{
  background:var(--panel2);
  color:var(--text);
  border:1px solid var(--border);
  border-radius:8px;
  padding:6px 10px;
  cursor:pointer;
}
button:hover{ border-color:var(--accent); }

details{ border:1px solid var(--border); border-radius:10px; background:rgba(17,26,51,.6); margin:10px 16px; }
details > summary{ cursor:pointer; padding:10px 12px; list-style:none; }
details > summary::-webkit-details-marker{ display:none; }
details > summary .secTitle{ font-weight:700; }

.secBody{ padding:0 12px 12px 12px; }

.sec.agent{ background:rgba(17,26,51,.8); }
.sec.system{ background:rgba(17,26,51,.7); }
.sec.toolcall{ background:rgba(15,23,48,.7); margin-left:28px; }
.sec.subagent{ background:rgba(15,23,48,.5); margin-left:44px; }

.blk{ margin:10px 0; }
.blk > summary{ padding:8px 10px; border-radius:8px; }
.blk.thought > summary{ color:#d5dbff; }
.blk.answer > summary{ color:#d9ffd5; }
.blk.toolcalls > summary{ color:#ffe9b5; }
.sumTitle{ font-family:var(--sans); }

.blk.loglineFold{ background:rgba(0,0,0,.12); }
.blk.loglineFold > summary{ color:var(--muted); }

pre.content{
  margin:0; padding:10px;
  background:rgba(0,0,0,.25);
  border:1px solid rgba(255,255,255,.08);
  border-radius:8px;
  overflow:auto;
  font-family:var(--mono);
  font-size:12px;
  line-height:1.45;
  white-space:pre-wrap;
}

.logline{
  display:grid;
  grid-template-columns: 170px 70px 1fr;
  gap:10px;
  padding:8px 10px;
  border:1px solid rgba(255,255,255,.06);
  border-radius:8px;
  background:rgba(0,0,0,.18);
  margin:8px 0;
}
.level-ERROR .lvl{ color:#ff7a7a; }
.level-WARNING .lvl{ color:#ffd27a; }
.level-INFO .lvl{ color:var(--accent); }
.level-DEBUG .lvl{ color:#9aa6ff; }
.logline .ts{ font-family:var(--mono); color:var(--muted); font-size:12px; }
.logline .lvl{ font-family:var(--mono); color:var(--accent); font-size:12px; }
.logline .msg{ font-family:var(--mono); font-size:12px; white-space:pre-wrap; }

@media (max-width: 900px){
  .logline{ grid-template-columns: 1fr; }
}
"""


_JS = r"""
function toggleAll(open) {
  const els = document.querySelectorAll('details');
  els.forEach(d => { d.open = open; });
}
"""


def convert_file(input_path: str, output_dir: str) -> str:
    text = _read_text(input_path)
    header, records = parse_log_records(text)
    root = build_sections(header, records)
    html_text = render_html(root, input_path)

    os.makedirs(output_dir, exist_ok=True)
    base = os.path.basename(input_path)
    if base.lower().endswith(".log"):
        base = base[: -len(".log")]
    out_path = os.path.join(output_dir, base + ".html")
    with open(out_path, "w", encoding="utf-8", errors="strict") as f:
        f.write(html_text)
    return out_path


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Convert AlphaSolve .log into collapsible HTML")
    p.add_argument("log_file", help="Path to the .log file (e.g. logs/xxxx.log)")
    p.add_argument(
        "--out-dir",
        default="log_as_html",
        help="Output directory for generated HTML (default: log_as_html)",
    )
    args = p.parse_args(argv)

    out = convert_file(args.log_file, args.out_dir)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

