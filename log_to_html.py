#!/usr/bin/env python3
"""Convert AlphaSolve log files into collapsible, well-structured HTML."""

from __future__ import annotations

import argparse
import html
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


ENTRY_PATTERN = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\s*│\s*([A-Z]+)\s*│\s*(.*)$"
)

MAJOR_AGENTS = {"solver", "verifier", "refiner", "summarizer"}

SUBCALL_START_PATTERN = re.compile(r"^\[Tool Call\]\s*(.+)$", re.IGNORECASE)
RESULT_MARKER = "[result]"

CONTEXT_TAG_KEYWORDS = [
    "思维链",
    "tool",
    "工具",
    "回答",
    "result",
    "dependency",
    "analysis",
    "总结",
    "chain",
    "call",
    "code",
    "本轮",
]

CATEGORY_LABELS = {
    "cot": "Chain-of-Thought",
    "tool": "Tool Call",
    "answer": "Answer / Summary",
    "metric": "Metric",
    "subagent": "Subagent",
    "log": "Log Entry",
}

AGENT_DISPLAY_NAMES = {
    "solver": "Solver Agent",
    "verifier": "Verifier Agent",
    "refiner": "Refiner Agent",
    "summarizer": "Summarizer Agent",
    "alphasolve": "AlphaSolve",
    "start": "Startup",
}


@dataclass
class SubCall:
    """Represents a nested tool/sub-agent invocation."""

    name: str
    task_lines: List[str] = field(default_factory=list)
    result_lines: List[str] = field(default_factory=list)

    def section_text(self, lines: List[str]) -> str:
        text = "\n".join(lines).strip()
        return text


@dataclass
class LogEntry:
    """Structured representation of a single log entry."""

    timestamp: Optional[str]
    level: Optional[str]
    message: str
    details: List[str]
    tag: Optional[str] = None
    agent: str = "global"
    category: str = "log"
    has_subagent: bool = False
    sub_calls: List[SubCall] = field(default_factory=list)

    def full_text(self) -> str:
        lines = [self.message.strip()]
        if self.details:
            lines.extend([line.rstrip("\n") for line in self.details])
        for sub in self.sub_calls:
            lines.append(f"[Tool Call] {sub.name}")
            lines.extend(line.rstrip("\n") for line in sub.task_lines)
            lines.extend(line.rstrip("\n") for line in sub.result_lines)
        return "\n".join(line for line in lines if line is not None)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert AlphaSolve .log files into collapsible HTML reports."
    )
    parser.add_argument("--log", required=True, help="Path to the source log file.")
    parser.add_argument(
        "--output",
        help="Optional output HTML path. Defaults to log_as_html/<logname>.html",
    )
    parser.add_argument(
        "--title",
        help="Optional custom title for the HTML page (default: derived from filename).",
    )
    return parser.parse_args()


def parse_log_file(log_path: Path) -> Tuple[List[str], List[LogEntry]]:
    header_lines: List[str] = []
    entries: List[LogEntry] = []
    current_entry: Optional[LogEntry] = None

    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n")
            match = ENTRY_PATTERN.match(line)
            if match:
                if current_entry:
                    entries.append(current_entry)
                timestamp, level, message = match.groups()
                current_entry = LogEntry(
                    timestamp=timestamp.strip(),
                    level=level.strip(),
                    message=message.strip(),
                    details=[],
                )
            else:
                if current_entry is not None:
                    current_entry.details.append(line)
                else:
                    header_lines.append(line)

    if current_entry:
        entries.append(current_entry)

    return header_lines, entries


def extract_tag(message: str) -> Optional[str]:
    match = re.search(r"\[([^\]]+)\]", message)
    if match:
        return match.group(1).strip()
    return None


def is_contextual_tag(tag: Optional[str]) -> bool:
    if not tag:
        return False
    lowered = tag.lower()
    return any(keyword in lowered for keyword in CONTEXT_TAG_KEYWORDS)


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "section"


def detect_subagent(text: str) -> bool:
    lowered = text.lower()
    if "math_research_subagent" in lowered or "subagent" in lowered:
        return True
    return "tool call" in lowered


def classify_category(tag: Optional[str], text: str, has_subagent: bool) -> str:
    lowered = text.lower()
    tag_lower = tag.lower() if tag else ""
    if has_subagent or "tool call" in lowered:
        return "subagent"
    if "tool call" in lowered or "[tool call]" in lowered or "工具" in lowered:
        return "tool"
    if tag and ("思维链" in tag or "chain" in tag_lower):
        return "cot"
    if tag and any(keyword in tag for keyword in ["回答", "answer", "result", "dependency"]):
        return "answer"
    if "cot length" in lowered or "answer length" in lowered or "metric" in lowered:
        return "metric"
    return "log"


def determine_agent(
    tag: Optional[str], message: str, fallback_major: Optional[str]
) -> Optional[str]:
    normalized_tag = tag.lower() if tag else None
    if normalized_tag in MAJOR_AGENTS:
        return normalized_tag
    if normalized_tag and not is_contextual_tag(normalized_tag):
        return normalized_tag

    message_lower = message.lower()
    for agent in MAJOR_AGENTS:
        bracketed = f"[{agent}]"
        if bracketed in message_lower:
            return agent
        if re.search(rf"\b{re.escape(agent)}\b", message_lower):
            return agent

    if tag and is_contextual_tag(tag):
        return fallback_major

    if fallback_major in MAJOR_AGENTS:
        return fallback_major
    return None


def enrich_entries(entries: List[LogEntry]) -> None:
    current_major_agent: Optional[str] = None
    for entry in entries:
        entry.tag = extract_tag(entry.message)
        agent = determine_agent(entry.tag, entry.message, current_major_agent)
        if agent in MAJOR_AGENTS:
            current_major_agent = agent

        entry.agent = agent or "global"

        sub_calls = parse_subcalls(entry.details)
        entry.sub_calls = sub_calls
        entry.has_subagent = bool(sub_calls) or detect_subagent(entry.message)

        entry.category = classify_category(
            entry.tag,
            entry.full_text(),
            entry.has_subagent,
        )


def parse_subcalls(detail_lines: List[str]) -> List[SubCall]:
    subcalls: List[SubCall] = []
    current: Optional[SubCall] = None
    recording_result = False

    for line in detail_lines:
        stripped = line.strip()
        start_match = SUBCALL_START_PATTERN.match(stripped)
        if start_match:
            if current:
                subcalls.append(current)
            current = SubCall(name=start_match.group(1).strip())
            recording_result = False
            continue

        if stripped.startswith(RESULT_MARKER):
            recording_result = True
            continue

        if current:
            target = current.result_lines if recording_result else current.task_lines
            target.append(line)

    if current:
        subcalls.append(current)
    return subcalls


def build_blocks(entries: List[LogEntry]) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    counters: Dict[str, int] = {}
    current_block: Optional[Dict[str, Any]] = None

    for entry in entries:
        agent = entry.agent or "global"
        if not current_block or current_block["agent"] != agent:
            counters[agent] = counters.get(agent, 0) + 1
            current_block = {
                "agent": agent,
                "index": counters[agent],
                "entries": [],
            }
            blocks.append(current_block)
        current_block["entries"].append(entry)

    return blocks


def truncate(text: str, length: int = 180) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    return clean if len(clean) <= length else clean[: length - 1].rstrip() + "…"


def format_header_block(header_lines: List[str]) -> str:
    if not header_lines:
        return ""
    safe = html.escape("\n".join(header_lines).strip())
    if not safe:
        return ""
    return f"""
    <section class=\"file-header\">
        <h2>File Header</h2>
        <pre>{safe}</pre>
    </section>
    """


def agent_display_name(agent: str) -> str:
    key = agent.lower()
    if key in AGENT_DISPLAY_NAMES:
        return AGENT_DISPLAY_NAMES[key]
    if agent == "global":
        return "Global / Misc"
    return agent.title()


def build_summary_grid(
    meta: Dict[str, Any], agent_totals: Dict[str, int], category_totals: Dict[str, int]
) -> str:
    agent_chips = "".join(
        f"<span class=\"chip agent-{slugify(agent)}\"><strong>{html.escape(agent_display_name(agent))}</strong><span>{count}</span></span>"
        for agent, count in sorted(agent_totals.items(), key=lambda item: (-item[1], item[0]))
    )

    category_chips = "".join(
        f"<span class=\"chip category-{slugify(category)}\"><strong>{html.escape(CATEGORY_LABELS.get(category, category.title()))}</strong><span>{count}</span></span>"
        for category, count in sorted(category_totals.items(), key=lambda item: (-item[1], item[0]))
    )

    return f"""
    <section class=\"summary-grid\">
        <div class=\"card\">
            <p class=\"label\">Log File</p>
            <p class=\"value\">{html.escape(meta['log_name'])}</p>
            <p class=\"sub\">{html.escape(meta['log_path'])}</p>
        </div>
        <div class=\"card\">
            <p class=\"label\">Total Entries</p>
            <p class=\"value\">{meta['entries']}</p>
            <p class=\"sub\">Across {meta['blocks']} blocks</p>
        </div>
        <div class=\"card\">
            <p class=\"label\">Generated</p>
            <p class=\"value\">{html.escape(meta['generated'])}</p>
            <p class=\"sub\">{html.escape(meta['timezone'])}</p>
        </div>
    </section>
    <section class=\"chips-row\">
        <div>
            <h3>Agent Activity</h3>
            <div class=\"chip-list\">{agent_chips or '<span class="muted">No agent data</span>'}</div>
        </div>
        <div>
            <h3>Category Breakdown</h3>
            <div class=\"chip-list\">{category_chips or '<span class="muted">No category data</span>'}</div>
        </div>
    </section>
    """


def render_entry(entry: LogEntry, entry_index: int) -> str:
    summary_text = truncate(entry.message)
    tag_badge = (
        f"<span class=\"badge tag-badge\">{html.escape(entry.tag or 'untagged')}</span>"
    )
    level_badge = (
        f"<span class=\"badge level-{entry.level.lower()}\">{entry.level}</span>"
        if entry.level
        else ""
    )
    category_badge = (
        f"<span class=\"badge category-badge category-{entry.category}\">{html.escape(CATEGORY_LABELS.get(entry.category, entry.category.title()))}</span>"
    )
    timestamp_html = html.escape(entry.timestamp) if entry.timestamp else "—"
    content_html = html.escape(entry.full_text() or "(no content)")
    subcall_html = "".join(render_subcall(sub, idx + 1) for idx, sub in enumerate(entry.sub_calls))

    return f"""
    <details class=\"entry category-{entry.category}\" open>
        <summary>
            <div class=\"summary-line\">
                {level_badge}
                {category_badge}
                {tag_badge}
                <span class=\"summary-text\">{html.escape(summary_text)}</span>
            </div>
            <div class=\"summary-meta\">
                <span>#{entry_index}</span>
                <span>{timestamp_html}</span>
                <span>{html.escape(entry.agent.title()) if entry.agent else ''}</span>
            </div>
        </summary>
        <div class=\"entry-body\">
            <pre>{content_html}</pre>
            {subcall_html}
        </div>
    </details>
    """


def render_subcall(subcall: SubCall, index: int) -> str:
    task = html.escape(subcall.section_text(subcall.task_lines) or "(no task provided)")
    result = html.escape(subcall.section_text(subcall.result_lines) or "(no result provided)")
    summary = truncate(subcall.name, 160)
    return f"""
    <details class=\"subcall\" open>
        <summary>
            <div class=\"summary-line\">
                <span class=\"badge category-badge category-tool\">Subagent Tool Call</span>
                <span class=\"summary-text\">{html.escape(summary)}</span>
            </div>
            <div class=\"summary-meta\">
                <span>subcall #{index}</span>
                <span>{html.escape(subcall.name)}</span>
            </div>
        </summary>
        <div class=\"entry-body\">
            <div class=\"subcall-section\">
                <h4>Task</h4>
                <pre>{task}</pre>
            </div>
            <div class=\"subcall-section\">
                <h4>Result</h4>
                <pre>{result}</pre>
            </div>
        </div>
    </details>
    """


def render_block(block: Dict[str, Any], block_index: int) -> str:
    agent = block["agent"]
    block_class = slugify(agent)
    title = f"{agent_display_name(agent)} · #{block['index']} ({len(block['entries'])} entries)"
    entries_html = "".join(
        render_entry(entry, idx + 1)
        for idx, entry in enumerate(block["entries"])
    )
    return f"""
    <details class=\"agent-block agent-{block_class}\" open>
        <summary>
            <div>
                <span class=\"badge agent-label\">{html.escape(agent_display_name(agent))}</span>
                <span class=\"summary-text\">{html.escape(title)}</span>
            </div>
        </summary>
        <div class=\"agent-body\">
            {entries_html or '<p class="muted">No entries</p>'}
        </div>
    </details>
    """


def build_html(
    meta: Dict[str, Any],
    header_lines: List[str],
    blocks: List[Dict[str, Any]],
    agent_totals: Dict[str, int],
    category_totals: Dict[str, int],
    title: str,
) -> str:
    header_html = format_header_block(header_lines)
    summary_html = build_summary_grid(meta, agent_totals, category_totals)
    blocks_html = "".join(render_block(block, idx) for idx, block in enumerate(blocks, start=1))
    style = CSS_STYLES
    script = JS_SCRIPT
    return f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>{html.escape(title)}</title>
    <style>{style}</style>
    <script defer>{script}</script>
</head>
<body>
    <header>
        <div>
            <h1>{html.escape(title)}</h1>
            <p class=\"muted\">Generated from {html.escape(meta['log_name'])}</p>
        </div>
        <div class=\"actions\">
            <button data-toggle=\"expand\">Expand All</button>
            <button data-toggle=\"collapse\">Collapse All</button>
        </div>
    </header>
    <main>
        {summary_html}
        {header_html}
        <section class=\"blocks\">
            {blocks_html}
        </section>
    </main>
</body>
</html>
"""


CSS_STYLES = """
:root {
    color-scheme: dark;
    --bg: #0b1120;
    --panel: #111c34;
    --border: rgba(148, 163, 184, 0.35);
    --text: #e2e8f0;
    --muted: #94a3b8;
    --accent: #38bdf8;
    font-family: 'Inter', 'Segoe UI', system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
}
body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
}
header {
    padding: 1.5rem 2rem;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 1rem;
}
header h1 {
    margin: 0;
    font-size: 1.5rem;
}
.actions button {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--text);
    padding: 0.5rem 0.9rem;
    border-radius: 999px;
    cursor: pointer;
    transition: background 0.2s ease;
}
.actions button:hover {
    background: rgba(148, 163, 184, 0.15);
}
main {
    max-width: 1200px;
    margin: 0 auto;
    padding: 2rem;
}
.summary-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 1rem;
}
.card {
    border: 1px solid var(--border);
    border-radius: 1rem;
    padding: 1rem;
    background: var(--panel);
    box-shadow: 0 10px 30px rgba(15, 23, 42, 0.45);
}
.card .label {
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-size: 0.75rem;
    color: var(--muted);
    margin-bottom: 0.4rem;
}
.card .value {
    margin: 0;
    font-size: 1.35rem;
}
.card .sub {
    margin: 0.3rem 0 0;
    color: var(--muted);
    font-size: 0.9rem;
}
.chips-row {
    margin: 2rem 0 1rem;
    display: grid;
    gap: 1.5rem;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
}
.chip-list {
    display: flex;
    flex-wrap: wrap;
    gap: 0.6rem;
}
.chip {
    background: rgba(56, 189, 248, 0.15);
    border: 1px solid rgba(56, 189, 248, 0.4);
    padding: 0.4rem 0.75rem;
    border-radius: 999px;
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.85rem;
}
.chip span {
    font-variant-numeric: tabular-nums;
    color: var(--muted);
}
.file-header pre {
    background: rgba(15, 23, 42, 0.7);
    border: 1px solid var(--border);
    padding: 1rem;
    border-radius: 0.8rem;
    overflow-x: auto;
}
.blocks {
    display: flex;
    flex-direction: column;
    gap: 1rem;
    margin-top: 2rem;
}
details {
    border-radius: 1rem;
    border: 1px solid var(--border);
    background: rgba(15, 23, 42, 0.65);
    box-shadow: 0 15px 35px rgba(0, 0, 0, 0.45);
}
details summary {
    cursor: pointer;
    list-style: none;
    padding: 1rem 1.25rem;
    font-weight: 600;
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
}
details[open] > summary {
    border-bottom: 1px solid var(--border);
}
summary::-webkit-details-marker {
    display: none;
}
.agent-body {
    padding: 1rem 1.25rem 1.25rem;
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
}
.entry summary {
    font-size: 0.95rem;
}
.entry-body {
    padding: 0.75rem 1rem 1rem;
}
.entry pre {
    margin: 0;
    background: rgba(15, 23, 42, 0.9);
    border: 1px solid rgba(56, 189, 248, 0.15);
    border-radius: 0.75rem;
    padding: 1rem;
    overflow-x: auto;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 0.85rem;
    line-height: 1.45;
}
.badge {
    display: inline-flex;
    align-items: center;
    padding: 0.1rem 0.6rem;
    border-radius: 999px;
    font-size: 0.75rem;
    border: 1px solid transparent;
    text-transform: capitalize;
}
.tag-badge {
    border-color: rgba(148, 163, 184, 0.35);
    color: var(--muted);
}
.level-info { background: rgba(59, 130, 246, 0.2); border-color: rgba(59, 130, 246, 0.4); }
.level-warning { background: rgba(251, 191, 36, 0.2); border-color: rgba(251, 191, 36, 0.4); }
.level-error { background: rgba(239, 68, 68, 0.2); border-color: rgba(239, 68, 68, 0.4); }
.level-metric { background: rgba(16, 185, 129, 0.2); border-color: rgba(16, 185, 129, 0.4); }
.category-badge { border-color: rgba(56, 189, 248, 0.35); color: var(--text); }
.summary-line {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: center;
}
.summary-meta {
    display: flex;
    gap: 0.75rem;
    font-size: 0.85rem;
    color: var(--muted);
}
.muted { color: var(--muted); }
.agent-label {
    background: rgba(248, 250, 252, 0.08);
    border-color: rgba(248, 250, 252, 0.3);
}
@media (max-width: 640px) {
    header, main { padding: 1.25rem; }
    .summary-line { flex-direction: column; align-items: flex-start; }
}
"""


JS_SCRIPT = """
document.addEventListener('DOMContentLoaded', () => {
    const buttons = document.querySelectorAll('[data-toggle]');
    buttons.forEach(btn => {
        btn.addEventListener('click', () => {
            const action = btn.getAttribute('data-toggle');
            const details = document.querySelectorAll('details');
            details.forEach(node => {
                if (action === 'expand') {
                    node.setAttribute('open', '');
                } else if (action === 'collapse') {
                    node.removeAttribute('open');
                }
            });
        });
    });
});
"""


def main() -> None:
    args = parse_args()
    log_path = Path(args.log).expanduser()
    if not log_path.is_file():
        raise SystemExit(f"Log file not found: {log_path}")

    output_path = (
        Path(args.output).expanduser()
        if args.output
        else Path("log_as_html") / f"{log_path.stem}.html"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    header_lines, entries = parse_log_file(log_path)
    if not entries:
        raise SystemExit("No log entries detected in the provided file.")

    enrich_entries(entries)
    blocks = build_blocks(entries)

    agent_totals: Dict[str, int] = {}
    category_totals: Dict[str, int] = {}
    for entry in entries:
        agent_totals[entry.agent] = agent_totals.get(entry.agent, 0) + 1
        category_totals[entry.category] = category_totals.get(entry.category, 0) + 1

    tz = datetime.now().astimezone().tzinfo
    meta = {
        "log_name": log_path.name,
        "log_path": str(log_path.resolve()),
        "entries": len(entries),
        "blocks": len(blocks),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": str(tz) if tz else "local",
    }

    title = args.title or f"AlphaSolve Log · {log_path.stem}"
    html_doc = build_html(
        meta,
        header_lines,
        blocks,
        agent_totals,
        category_totals,
        title,
    )

    output_path.write_text(html_doc, encoding="utf-8")
    rel_path = os.path.relpath(output_path, Path.cwd())
    print(f"HTML log written to {rel_path}")


if __name__ == "__main__":
    main()

