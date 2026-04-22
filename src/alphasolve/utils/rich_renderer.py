"""Rich-based terminal rendering helpers for AlphaSolve CLI.

The module keeps the legacy single-call helpers and adds a shared lemma-team
dashboard.  A pool can opt into one dashboard and let every visible
LemmaWorker update its own lane without competing for separate Rich Live
instances.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from math import ceil
from typing import TYPE_CHECKING, Optional

from rich import box
from rich.cells import cell_len, set_cell_size
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from rich.console import RenderableType

RICH_CONSOLE = Console()

# Animated bullet frames after the "Thinking" label.
_BULLET_FRAMES = (".  ", ".. ", "...", " ..", "  .", "   ")
_BULLET_FRAME_INTERVAL = 0.13  # seconds per frame
_THINKING_PREVIEW_LINES = 4
_DISPLAY_CHAR_LIMIT = 20000
_WORKER_CONTENT_LINES = 7
_WORKER_PANEL_HEIGHT = _WORKER_CONTENT_LINES + 2
_ORCHESTRATOR_LOG_LINES = 18
_ORCHESTRATOR_MIN_WIDTH = 32
_WORKER_MIN_WIDTH = 28
_MAX_COMPLETED_WORKERS = 6
_TERMINAL_STATUSES = {"verified", "rejected", "failed", "solved"}

_TEAM_COLORS = (
    "cyan",
    "magenta",
    "green",
    "yellow",
    "blue",
    "bright_cyan",
    "bright_magenta",
    "bright_green",
)

_STATUS_STYLES = {
    "queued": "grey50",
    "running": "cyan",
    "thinking": "magenta",
    "tool": "yellow",
    "writing": "bright_cyan",
    "verified": "green",
    "solved": "bold green",
    "rejected": "yellow",
    "failed": "red",
}


def _bullet_frame_for(elapsed: float) -> str:
    """Select the current bullet frame from wall-clock elapsed time."""
    idx = int(elapsed / _BULLET_FRAME_INTERVAL) % len(_BULLET_FRAMES)
    return _BULLET_FRAMES[idx]


def _tail_lines(text: str, n: int) -> str:
    """Extract the last *n* lines from *text* via reverse scanning."""
    if n <= 0:
        return ""
    pos = len(text)
    for _ in range(n):
        pos = text.rfind("\n", 0, pos)
        if pos == -1:
            return text
    return text[pos + 1 :]


def _tail_chars(text: str, n: int = _DISPLAY_CHAR_LIMIT) -> str:
    if len(text) <= n:
        return text
    return text[-n:]


def _truncate_cells(text: str, max_width: int) -> str:
    """Trim a display string to a terminal cell width without wrapping."""
    if max_width <= 0:
        return ""
    clean = text.replace("\r", " ").replace("\t", " ").strip()
    if cell_len(clean) <= max_width:
        return clean
    if max_width <= 3:
        return set_cell_size(clean, max_width).rstrip()
    return set_cell_size(clean, max_width - 3).rstrip() + "..."


def _format_preview(text: str, *, max_lines: int, max_width: int) -> str:
    if max_lines <= 0 or max_width <= 0:
        return ""
    lines = text.splitlines() or [text]
    tail = lines[-max_lines:]
    return "\n".join(_truncate_cells(line, max_width) for line in tail)


def compose_thinking_live(thinking_text: str, elapsed: float) -> "RenderableType":
    """Compose the transient Live display for reasoning content."""
    frame = _bullet_frame_for(elapsed)
    header = Text.assemble(
        ("Thinking", "italic"),
        (f" {frame}", "cyan"),
        (f"  {elapsed:.1f}s", "grey50"),
        (f" | {len(thinking_text)} chars", "grey50"),
    )
    spinner = Spinner("dots", text=header)
    if not thinking_text:
        return spinner
    preview = _tail_lines(thinking_text, _THINKING_PREVIEW_LINES)
    return Group(spinner, Text(preview, style="grey50 italic"))


def compose_thinking_final(elapsed: float, char_count: int) -> Text:
    """Compose the one-line summary committed after thinking ends."""
    return Text(
        f"Thought for {elapsed:.1f}s | {char_count} chars",
        style="grey50 italic",
    )


def build_tool_using_text(name: str, arg_preview: str) -> Spinner:
    """Render an in-progress tool call: ``Using <name> (<args>)`` with spinner."""
    text = Text()
    text.append("Using ", style="")
    text.append(name, style="blue")
    if arg_preview:
        text.append(" (", style="grey50")
        text.append(arg_preview, style="grey50")
        text.append(")", style="grey50")
    return Spinner("dots", text=text)


def build_tool_used_text(name: str, arg_preview: str, is_error: bool = False) -> Text:
    """Render a finished tool call: ``Used <name> (<args>)``."""
    bullet_style = "dark_red" if is_error else "green"
    text = Text()
    text.append("* ", style=bullet_style)
    text.append("Used ", style="")
    text.append(name, style="blue")
    if arg_preview:
        text.append(" (", style="grey50")
        text.append(arg_preview, style="grey50")
        text.append(")", style="grey50")
    if is_error:
        text.append(" error", style="dark_red")
    return text


@dataclass
class WorkerRenderState:
    worker_id: int
    color: str
    status: str = "queued"
    phase: str = "starting"
    verified_ctx_size: int = 0
    remaining_capacity: int = 0
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    thinking_text: str = ""
    output_text: str = ""
    active_tool: Optional[str] = None
    active_tool_args: str = ""
    last_tool: Optional[str] = None
    last_tool_error: bool = False
    log_lines: list[str] = field(default_factory=list)
    result_summary: str = ""
    completed_at: Optional[float] = None


@dataclass
class OrchestratorRenderState:
    status: str = "idle"
    phase: str = "orchestrator"
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    thinking_text: str = ""
    output_text: str = ""
    active_tool: Optional[str] = None
    active_tool_args: str = ""
    last_tool: Optional[str] = None
    last_tool_error: bool = False
    log_lines: list[str] = field(default_factory=list)


class LemmaTeamRenderer:
    """One Rich Live dashboard shared by all terminal-visible lemma workers."""

    def __init__(
        self,
        *,
        console: Console = RICH_CONSOLE,
        refresh_per_second: int = 4,
        max_log_lines: int = 5,
        screen: bool = True,
    ) -> None:
        self.console = console
        self.refresh_per_second = refresh_per_second
        self._min_refresh_interval = 1.0 / max(1, refresh_per_second)
        self.max_log_lines = max_log_lines
        self.screen = screen
        self._workers: dict[int, WorkerRenderState] = {}
        self._orchestrator = OrchestratorRenderState()
        self._lock = threading.RLock()
        self._live: Optional[Live] = None
        self._started_at = time.time()
        self._last_refresh_at = 0.0
        self._pool_capacity: Optional[int] = None
        self._pool_verified_count = 0
        self._worker_started = 0
        self._worker_finished = 0
        self._accepted = 0
        self._rejected = 0
        self._failed = 0
        self._duplicates = 0
        self._solved = False

    def start(self) -> None:
        with self._lock:
            if self._live is not None:
                return
            self._live = Live(
                console=self.console,
                screen=self.screen,
                refresh_per_second=self.refresh_per_second,
                auto_refresh=True,
                transient=False,
                get_renderable=self.render,
                vertical_overflow="crop",
            )
            self._live.start()
            self._last_refresh_at = time.time()

    def stop(self) -> None:
        with self._lock:
            live = self._live
            self._live = None
        if live is not None:
            live.stop()

    def update_pool(
        self,
        *,
        capacity_verified: Optional[int] = None,
        verified_count: Optional[int] = None,
        solved: Optional[bool] = None,
    ) -> None:
        with self._lock:
            if capacity_verified is not None:
                self._pool_capacity = max(0, int(capacity_verified))
            if verified_count is not None:
                self._pool_verified_count = max(0, int(verified_count))
            if solved is not None:
                self._solved = bool(solved)
            self._refresh_locked(force=True)

    def register_worker(
        self,
        worker_id: int,
        *,
        verified_ctx_size: int = 0,
        remaining_capacity: int = 0,
    ) -> None:
        with self._lock:
            if worker_id not in self._workers:
                self._workers[worker_id] = WorkerRenderState(
                    worker_id=worker_id,
                    color=_TEAM_COLORS[worker_id % len(_TEAM_COLORS)],
                    verified_ctx_size=verified_ctx_size,
                    remaining_capacity=remaining_capacity,
                )
                self._worker_started += 1
            state = self._workers[worker_id]
            state.verified_ctx_size = verified_ctx_size
            state.remaining_capacity = remaining_capacity
            state.status = "running"
            state.phase = "spawned"
            state.completed_at = None
            state.updated_at = time.time()
            self._refresh_locked(force=True)

    def update_phase(self, worker_id: int, phase: str, *, status: str = "running") -> None:
        with self._lock:
            state = self._ensure_worker_locked(worker_id)
            state.phase = phase
            state.status = status
            if status not in _TERMINAL_STATUSES:
                state.completed_at = None
            state.updated_at = time.time()
            self._refresh_locked()

    def update_orchestrator_phase(self, phase: str, *, status: str = "running") -> None:
        with self._lock:
            state = self._orchestrator
            state.phase = phase
            state.status = status
            state.updated_at = time.time()
            self._refresh_locked()

    def update_thinking(
        self,
        worker_id: int,
        *,
        module: str,
        thinking_text: str,
        elapsed: float,
    ) -> None:
        del elapsed
        with self._lock:
            state = self._ensure_worker_locked(worker_id)
            state.phase = module
            state.status = "thinking"
            state.thinking_text = _tail_chars(thinking_text)
            state.updated_at = time.time()
            self._refresh_locked()

    def update_orchestrator_thinking(
        self,
        *,
        module: str,
        thinking_text: str,
        elapsed: float,
    ) -> None:
        del elapsed
        with self._lock:
            state = self._orchestrator
            state.phase = module
            state.status = "thinking"
            state.thinking_text = _tail_chars(thinking_text)
            state.updated_at = time.time()
            self._refresh_locked()

    def finish_thinking(self, worker_id: int, *, module: str, elapsed: float, char_count: int) -> None:
        with self._lock:
            state = self._ensure_worker_locked(worker_id)
            state.phase = module
            state.status = "running"
            state.log_lines.append(f"Thought for {elapsed:.1f}s | {char_count} chars")
            state.log_lines = state.log_lines[-self.max_log_lines :]
            state.thinking_text = ""
            state.updated_at = time.time()
            self._refresh_locked(force=True)

    def finish_orchestrator_thinking(self, *, module: str, elapsed: float, char_count: int) -> None:
        with self._lock:
            state = self._orchestrator
            state.phase = module
            state.status = "running"
            state.log_lines.append(f"Thought for {elapsed:.1f}s | {char_count} chars")
            state.log_lines = state.log_lines[-_ORCHESTRATOR_LOG_LINES:]
            state.thinking_text = ""
            state.updated_at = time.time()
            self._refresh_locked(force=True)

    def append_output(self, worker_id: int, text: str) -> None:
        if not text:
            return
        with self._lock:
            state = self._ensure_worker_locked(worker_id)
            state.output_text = _tail_chars(state.output_text + text)
            state.status = "writing"
            state.updated_at = time.time()
            self._refresh_locked()

    def append_orchestrator_output(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            state = self._orchestrator
            state.output_text = _tail_chars(state.output_text + text)
            state.status = "writing"
            state.updated_at = time.time()
            self._refresh_locked()

    def update_tool_start(self, worker_id: int, *, module: str, name: str, arg_preview: str) -> None:
        with self._lock:
            state = self._ensure_worker_locked(worker_id)
            state.phase = module
            state.status = "tool"
            state.active_tool = name
            state.active_tool_args = arg_preview
            state.updated_at = time.time()
            self._refresh_locked()

    def update_orchestrator_tool_start(self, *, module: str, name: str, arg_preview: str) -> None:
        with self._lock:
            state = self._orchestrator
            state.phase = module
            state.status = "tool"
            state.active_tool = name
            state.active_tool_args = arg_preview
            state.updated_at = time.time()
            self._refresh_locked()

    def update_tool_done(self, worker_id: int, *, name: str, is_error: bool = False) -> None:
        with self._lock:
            state = self._ensure_worker_locked(worker_id)
            state.last_tool = name
            state.last_tool_error = is_error
            state.active_tool = None
            state.active_tool_args = ""
            state.status = "running"
            state.updated_at = time.time()
            self._refresh_locked(force=True)

    def update_orchestrator_tool_done(self, *, name: str, is_error: bool = False) -> None:
        with self._lock:
            state = self._orchestrator
            state.last_tool = name
            state.last_tool_error = is_error
            state.active_tool = None
            state.active_tool_args = ""
            state.status = "running"
            state.updated_at = time.time()
            self._refresh_locked(force=True)

    def log(
        self,
        worker_id: Optional[int],
        message: str,
        *,
        module: Optional[str] = None,
        level: str = "INFO",
        end: str = "\n",
    ) -> None:
        if not message and end == "\n":
            return
        if worker_id is None:
            if end != "\n":
                self.append_orchestrator_output(message + end)
                return
            line = self._format_log_line(message, module=module, level=level)
            with self._lock:
                state = self._orchestrator
                state.log_lines.append(line)
                state.log_lines = state.log_lines[-_ORCHESTRATOR_LOG_LINES:]
                state.updated_at = time.time()
                self._refresh_locked()
            return
        if end != "\n":
            self.append_output(worker_id, message + end)
            return
        line = self._format_log_line(message, module=module, level=level)
        with self._lock:
            state = self._ensure_worker_locked(worker_id)
            state.log_lines.append(line)
            state.log_lines = state.log_lines[-self.max_log_lines :]
            state.updated_at = time.time()
            self._refresh_locked()

    def finish_worker(
        self,
        worker_id: int,
        *,
        status: str,
        solved: bool = False,
        summary: str = "",
    ) -> None:
        with self._lock:
            state = self._ensure_worker_locked(worker_id)
            was_completed = state.completed_at is not None
            state.status = "solved" if solved else status
            state.phase = "done"
            state.result_summary = summary
            state.active_tool = None
            state.thinking_text = ""
            state.completed_at = time.time()
            state.updated_at = time.time()
            if not was_completed:
                self._worker_finished += 1
            if not was_completed and state.status == "failed":
                self._failed += 1
            self._prune_completed_locked()
            self._refresh_locked(force=True)

    def record_commit(
        self,
        *,
        accepted: bool,
        status: str,
        solved: bool,
        duplicate_of: Optional[int] = None,
        verified_count: Optional[int] = None,
    ) -> None:
        with self._lock:
            if accepted:
                self._accepted += 1
            if status == "rejected":
                self._rejected += 1
            if duplicate_of is not None:
                self._duplicates += 1
            if verified_count is not None:
                self._pool_verified_count = max(0, int(verified_count))
            self._solved = self._solved or solved
            self._refresh_locked(force=True)

    def remove_worker(self, worker_id: int) -> None:
        with self._lock:
            self._workers.pop(worker_id, None)
            self._refresh_locked(force=True)

    def render(self) -> "RenderableType":
        with self._lock:
            worker_count = len(self._workers)
            active_count = sum(
                1 for state in self._workers.values() if state.status not in _TERMINAL_STATUSES
            )
            elapsed = time.time() - self._started_at

            header = self._render_header(active_count=active_count, worker_count=worker_count, elapsed=elapsed)
            worker_states = sorted(self._workers.values(), key=lambda s: s.worker_id)
            right = self._render_worker_grid(worker_states)
            layout = Table.grid(expand=True)
            layout.add_column(ratio=4, min_width=_ORCHESTRATOR_MIN_WIDTH)
            layout.add_column(ratio=9, min_width=50)
            layout.add_row(self._render_orchestrator(), right)
            return Group(header, layout)

    def _render_header(self, *, active_count: int, worker_count: int, elapsed: float) -> Text:
        cap = "?"
        if self._pool_capacity is not None:
            cap = str(self._pool_capacity)
        title_style = "bold green" if self._solved else "bold white"
        if self.console.size.width < 100:
            return Text.assemble(
                ("AlphaSolve", title_style),
                ("  run ", "grey50"),
                (f"{active_count}/{worker_count}", "cyan"),
                ("  done ", "grey50"),
                (str(self._worker_finished), "green" if self._worker_finished else "grey50"),
                ("  verified ", "grey50"),
                (f"{self._pool_verified_count}/{cap}", "green"),
                ("  rejected ", "grey50"),
                (str(self._rejected), "yellow" if self._rejected else "grey50"),
                ("  failed ", "grey50"),
                (str(self._failed), "red" if self._failed else "grey50"),
                ("  ", "grey50"),
                (f"{elapsed:.0f}s", "grey50"),
            )
        return Text.assemble(
            ("AlphaSolve", title_style),
            ("  running ", "grey50"),
            (str(active_count), "cyan"),
            (f"/{worker_count}", "grey50"),
            ("  started ", "grey50"),
            (str(self._worker_started), "cyan"),
            ("  done ", "grey50"),
            (str(self._worker_finished), "green" if self._worker_finished else "grey50"),
            ("  accepted ", "grey50"),
            (f"{self._pool_verified_count}/{cap}", "green"),
            ("  new ", "grey50"),
            (str(self._accepted), "green" if self._accepted else "grey50"),
            ("  rejected ", "grey50"),
            (str(self._rejected), "yellow" if self._rejected else "grey50"),
            ("  duplicates ", "grey50"),
            (str(self._duplicates), "yellow" if self._duplicates else "grey50"),
            ("  failed ", "grey50"),
            (str(self._failed), "red" if self._failed else "grey50"),
            ("  elapsed ", "grey50"),
            (f"{elapsed:.1f}s", "grey50"),
        )

    def _render_worker_grid(self, states: list[WorkerRenderState]) -> "RenderableType":
        available_height = max(1, self.console.size.height - 2)
        rows_per_column = max(1, available_height // _WORKER_PANEL_HEIGHT)
        column_count = max(1, ceil(max(1, len(states)) / rows_per_column))
        available_width = max(_WORKER_MIN_WIDTH, int(self.console.size.width * 0.66))
        panel_width = max(_WORKER_MIN_WIDTH, (available_width // column_count) - 1)

        if not states:
            panels = [
                Panel(
                    Text("Waiting for lemma workers...", style="grey50 italic", no_wrap=True),
                    border_style="grey35",
                    box=box.SQUARE,
                    height=_WORKER_PANEL_HEIGHT,
                    padding=(0, 1),
                    width=panel_width,
                )
            ]
        else:
            panels = [self._render_worker(state, width=panel_width) for state in states]

        if column_count == 1:
            return Group(*panels)

        columns = [
            Group(*panels[i * rows_per_column : (i + 1) * rows_per_column])
            for i in range(column_count)
        ]
        grid = Table.grid(expand=True)
        for _ in columns:
            grid.add_column(ratio=1, min_width=24)
        grid.add_row(*columns)
        return grid

    def _render_orchestrator(self) -> Panel:
        state = self._orchestrator
        elapsed = time.time() - state.started_at
        title = f"orchestrator | {state.phase}"
        subtitle = f"{state.status} | {elapsed:.1f}s"
        content_width = max(20, int(self.console.size.width * 0.31) - 4)

        lines: list[Text] = []
        if state.active_tool:
            tool = Text.assemble(("> ", "cyan"), ("Using ", "grey50"), (state.active_tool, "bold blue"))
            if state.active_tool_args:
                tool.append(f" ({_truncate_cells(state.active_tool_args, max(8, content_width - len(state.active_tool) - 10))})", style="grey50")
            lines.append(tool)
        elif state.last_tool:
            tool_style = "dark_red" if state.last_tool_error else "green"
            lines.append(Text.assemble(("> ", "cyan"), ("Used ", tool_style), (state.last_tool, "blue")))

        preview = self._best_orchestrator_preview(
            max(4, self.console.size.height - 8 - len(lines)),
            width=content_width,
        )
        if preview:
            lines.append(Text(preview, style=self._orchestrator_preview_style(), no_wrap=True, overflow="ellipsis"))
        if not lines:
            lines.append(Text("Waiting for orchestration events...", style="grey50 italic", no_wrap=True))

        return Panel(
            Group(*lines),
            title=title,
            title_align="left",
            subtitle=subtitle,
            subtitle_align="right",
            border_style="grey50",
            box=box.SQUARE,
            padding=(0, 1),
        )

    def _render_worker(self, state: WorkerRenderState, *, width: int) -> Panel:
        elapsed = time.time() - state.started_at
        title = f"worker-{state.worker_id:02d}"
        status_style = _STATUS_STYLES.get(state.status, state.color)
        subtitle = f"{state.status} | {elapsed:.1f}s"
        content_width = max(10, width - 4)

        meta = Text.assemble(
            (_truncate_cells(state.phase, max(8, content_width - 18)), "bold"),
            ("  ctx ", "grey50"),
            (str(state.verified_ctx_size), state.color),
            ("  quota ", "grey50"),
            (str(state.remaining_capacity), state.color),
        )

        lines: list[Text] = [meta]
        if state.active_tool:
            tool = Text.assemble(
                ("> ", state.color),
                ("Using ", "grey50"),
                (state.active_tool, "bold blue"),
            )
            if state.active_tool_args:
                arg_width = max(8, content_width - len(state.active_tool) - 10)
                tool.append(f" ({_truncate_cells(state.active_tool_args, arg_width)})", style="grey50")
            lines.append(tool)
        elif state.last_tool:
            tool_style = "dark_red" if state.last_tool_error else "green"
            lines.append(Text.assemble(("> ", state.color), ("Used ", tool_style), (state.last_tool, "blue")))

        if state.result_summary:
            lines.append(Text(_truncate_cells(state.result_summary, content_width), style="bold", no_wrap=True))
        preview = self._best_preview(
            state,
            body_lines=max(1, _WORKER_CONTENT_LINES - len(lines)),
            width=content_width,
        )
        if preview:
            lines.append(Text(preview, style=self._preview_style(state), no_wrap=True, overflow="ellipsis"))

        body = Group(*lines[:_WORKER_CONTENT_LINES])
        return Panel(
            body,
            title=title,
            title_align="left",
            subtitle=subtitle,
            subtitle_align="right",
            border_style=status_style,
            box=box.SQUARE,
            height=_WORKER_PANEL_HEIGHT,
            padding=(0, 1),
            width=width,
        )

    def _best_orchestrator_preview(self, body_lines: int, *, width: int) -> str:
        state = self._orchestrator
        if state.thinking_text:
            return _format_preview(state.thinking_text, max_lines=body_lines, max_width=width)
        if state.output_text:
            return _format_preview(state.output_text, max_lines=body_lines, max_width=width)
        if state.log_lines:
            return _format_preview("\n".join(state.log_lines), max_lines=body_lines, max_width=width)
        return ""

    def _orchestrator_preview_style(self) -> str:
        state = self._orchestrator
        if state.thinking_text:
            return "grey50 italic"
        if state.output_text:
            return "white"
        return "grey50"

    def _best_preview(self, state: WorkerRenderState, *, body_lines: int, width: int) -> str:
        if state.thinking_text:
            return _format_preview(state.thinking_text, max_lines=body_lines, max_width=width)
        if state.output_text:
            return _format_preview(state.output_text, max_lines=body_lines, max_width=width)
        if state.log_lines:
            return _format_preview("\n".join(state.log_lines), max_lines=body_lines, max_width=width)
        return ""

    def _preview_style(self, state: WorkerRenderState) -> str:
        if state.thinking_text:
            return "grey50 italic"
        if state.output_text:
            return "white"
        return "grey50"

    def _ensure_worker_locked(self, worker_id: int) -> WorkerRenderState:
        if worker_id not in self._workers:
            self._workers[worker_id] = WorkerRenderState(
                worker_id=worker_id,
                color=_TEAM_COLORS[worker_id % len(_TEAM_COLORS)],
            )
            self._worker_started += 1
        return self._workers[worker_id]

    def _format_log_line(self, message: str, *, module: Optional[str], level: str) -> str:
        prefix = f"[{level.lower()}]"
        if module:
            prefix += f" {module}"
        clean = message.strip()
        return f"{prefix} {clean}" if clean else prefix

    def _prune_completed_locked(self) -> None:
        completed = [
            state
            for state in self._workers.values()
            if state.completed_at is not None and state.status != "solved"
        ]
        overflow = len(completed) - _MAX_COMPLETED_WORKERS
        if overflow <= 0:
            return
        for state in sorted(completed, key=lambda s: s.completed_at or 0)[:overflow]:
            self._workers.pop(state.worker_id, None)

    def _refresh_locked(self, *, force: bool = False) -> None:
        if self._live is not None:
            now = time.time()
            if not force and now - self._last_refresh_at < self._min_refresh_interval:
                return
            self._last_refresh_at = now
            if not getattr(self._live, "auto_refresh", False):
                self._live.update(self.render(), refresh=True)
