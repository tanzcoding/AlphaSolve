from __future__ import annotations

import io
import threading
import time
import textwrap
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

from rich import box
from rich.cells import cell_len, set_cell_size
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from rich.console import RenderableType


RICH_CONSOLE = Console()

_DISPLAY_CHAR_LIMIT = 16000
_MAX_TEAM_LOG_LINES = 80
_ORCHESTRATOR_LOG_LINES = 30
_RESIZE_POLL_INTERVAL = 0.10
_TERMINAL_STATUSES = {"verified", "rejected", "failed", "solved", "cancelled"}
_MAX_TIMELINE_EVENTS = 40
_WORKER_RETENTION_SECONDS = 600

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

_STATUS_ICON: dict[str, tuple[str, str]] = {
    "queued": ("◌", "grey50"),
    "idle": ("○", "grey50"),
    "running": ("●", "cyan"),
    "thinking": ("●", "magenta"),
    "tool": ("▶", "yellow"),
    "writing": ("◆", "bright_cyan"),
    "complete": ("✓", "green"),
    "verified": ("✓", "green"),
    "solved": ("✓", "bold green"),
    "rejected": ("✗", "yellow"),
    "failed": ("✗", "red"),
    "cancelled": ("✗", "grey50"),
}

_CHECK = "✓"
_CROSS = "✗"
_PLAY = "▶"
_DOT = "·"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_elapsed(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _fmt_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _clean_inline(text: str) -> str:
    return " ".join(text.replace("\r", " ").replace("\t", " ").split())


def _truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    clean = text.replace("\r", " ").replace("\t", " ").replace("\n", " ").strip()
    if cell_len(clean) <= width:
        return clean
    if width <= 3:
        return set_cell_size(clean, width).rstrip()
    return set_cell_size(clean, width - 1).rstrip() + "…"


def _tail_chars(text: str, n: int = _DISPLAY_CHAR_LIMIT) -> str:
    return text[-n:] if len(text) > n else text


def _tail_lines(text: str, n: int) -> list[str]:
    if not text or n <= 0:
        return []
    lines = text.splitlines()
    return lines[-n:] if len(lines) > n else lines


def _wrap_inline(text: str, *, width: int, max_lines: int) -> list[str]:
    if max_lines <= 0:
        return []
    clean = _clean_inline(text)
    if not clean:
        return []
    wrap_width = max(8, width)
    lines = textwrap.wrap(
        clean,
        width=wrap_width,
        break_long_words=True,
        break_on_hyphens=False,
        replace_whitespace=False,
        drop_whitespace=True,
    )
    if not lines:
        return []
    return [_truncate(line, width) for line in lines[:max_lines]]


def _text_line(text: str, style: str = "", *, no_wrap: bool = True) -> Text:
    return Text(text, style=style, no_wrap=no_wrap, overflow="ellipsis")


def _worker_label(worker_id: str) -> str:
    text = str(worker_id)
    if text.isdigit():
        return text.zfill(2)
    return text


# ---------------------------------------------------------------------------
# Timeline event model (kimi-cli style)
# ---------------------------------------------------------------------------


class EventType(Enum):
    PHASE = auto()
    THOUGHT = auto()
    TOOL_DONE = auto()
    CONTENT = auto()
    WARNING = auto()
    LOG = auto()


@dataclass
class TimelineEvent:
    type: EventType
    timestamp: float
    text: str
    style: str = ""
    meta: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Render state
# ---------------------------------------------------------------------------


@dataclass
class WorkerRenderState:
    worker_id: str
    color: str
    status: str = "queued"
    phase: str = "starting"
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    char_count: int = 0
    timeline: deque[TimelineEvent] = field(
        default_factory=lambda: deque(maxlen=_MAX_TIMELINE_EVENTS)
    )
    finished_at: float | None = None
    # Runtime thinking state (for dynamic spinner rendering)
    thinking_started_at: float = 0.0
    thinking_token_count: int = 0
    thinking_text: str = ""
    # Runtime tool state
    active_tool: str | None = None
    active_tool_args: str = ""
    # Output buffer (accumulates assistant output, flushed to timeline on completion)
    output_buffer: str = ""
    result_summary: str = ""
    model: str = ""


@dataclass
class OrchestratorRenderState:
    status: str = "idle"
    phase: str = "orchestrator"
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    char_count: int = 0
    timeline: deque[TimelineEvent] = field(
        default_factory=lambda: deque(maxlen=_MAX_TIMELINE_EVENTS)
    )
    thinking_started_at: float = 0.0
    thinking_token_count: int = 0
    thinking_text: str = ""
    active_tool: str | None = None
    active_tool_args: str = ""
    output_buffer: str = ""
    model: str = ""


# ---------------------------------------------------------------------------
# Low-level terminal painter
# ---------------------------------------------------------------------------


class _LineDiffLive:
    """Small terminal painter that updates changed dashboard lines only."""

    auto_refresh = False

    def __init__(self, *, console: Console, screen: bool) -> None:
        self.console = console
        self.screen = screen
        self._started = False
        self._last_lines: list[str] = []
        self._last_size: tuple[int, int] | None = None

    def start(self, *, refresh: bool = False) -> None:
        del refresh
        if self._started:
            return
        self._started = True
        if self.screen:
            self._write("\x1b[?1049h\x1b[H\x1b[2J")
        self._write("\x1b[?25l")

    def stop(self) -> None:
        if not self._started:
            return
        if self.screen:
            self._write("\x1b[?25h\x1b[?1049l")
        else:
            self._write("\n\x1b[?25h")
        self._started = False

    def update(self, renderable: "RenderableType", *, refresh: bool = True) -> None:
        del refresh
        if not self._started:
            return
        size = (max(80, self.console.size.width), max(24, self.console.size.height))
        lines = self._render_lines(renderable, width=size[0], height=size[1])
        if self._last_size != size or len(lines) != len(self._last_lines):
            self._paint_full(lines)
        else:
            self._paint_diff(lines)
        self._last_lines = lines
        self._last_size = size

    def _render_lines(self, renderable: "RenderableType", *, width: int, height: int) -> list[str]:
        stream = io.StringIO()
        capture = Console(
            file=stream,
            width=width,
            height=height,
            color_system=self.console.color_system,
            force_terminal=self.console.is_terminal,
            no_color=self.console.no_color,
            legacy_windows=False,
            soft_wrap=False,
        )
        capture.print(renderable, end="")
        lines = stream.getvalue().replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if len(lines) < height:
            lines.extend([""] * (height - len(lines)))
        return lines[:height]

    def _paint_full(self, lines: list[str]) -> None:
        if self.screen:
            self._write("\x1b[H\x1b[2J")
        elif self._last_lines:
            self._move_to_top(len(self._last_lines))
            self._clear_lines(len(self._last_lines))
            self._move_to_top(len(self._last_lines))
        for index, line in enumerate(lines):
            self._write("\x1b[2K\r" + line)
            if index != len(lines) - 1:
                self._write("\x1b[1E")

    def _paint_diff(self, lines: list[str]) -> None:
        self._move_to_top(len(lines))
        for index, line in enumerate(lines):
            if line != self._last_lines[index]:
                self._write("\x1b[2K\r" + line)
            if index != len(lines) - 1:
                self._write("\x1b[1E")

    def _clear_lines(self, line_count: int) -> None:
        for index in range(line_count):
            self._write("\x1b[2K\r")
            if index != line_count - 1:
                self._write("\x1b[1E")

    def _move_to_top(self, line_count: int) -> None:
        if self.screen:
            self._write("\x1b[H")
        elif line_count > 1:
            self._write(f"\x1b[{line_count - 1}F")
        else:
            self._write("\r")

    def _write(self, text: str) -> None:
        self.console.file.write(text)
        self.console.file.flush()


# ---------------------------------------------------------------------------
# PropositionTeamRenderer
# ---------------------------------------------------------------------------


class PropositionTeamRenderer:
    """Native Rich dashboard for the AlphaSolve agent team (kimi-cli timeline style)."""

    def __init__(
        self,
        *,
        console: Console = RICH_CONSOLE,
        refresh_per_second: float = 2.0,
        max_log_lines: int = 6,
        screen: bool = True,
    ) -> None:
        self.console = console
        self.refresh_per_second = refresh_per_second
        self._min_refresh_interval = 1.0 / max(0.1, float(refresh_per_second))
        self.max_log_lines = max_log_lines
        self.screen = screen
        self._workers: dict[str, WorkerRenderState] = {}
        self._orchestrator = OrchestratorRenderState()
        self._digest = OrchestratorRenderState(status="idle", phase="idle")
        self._lock = threading.RLock()
        self._live: _LineDiffLive | None = None
        self._started_at = time.time()
        self._last_refresh_at = 0.0
        self._refresh_pending = False
        self._last_seen_size: tuple[int, int] | None = None
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._pool_verified_count = 0
        self._worker_started = 0
        self._worker_finished = 0
        self._accepted = 0
        self._rejected = 0
        self._failed = 0
        self._solved = False
        self._team_log: list[str] = []
        self._digest_pending = 0
        self._digest_processed = 0
        self._digest_current_label = ""
        self._digest_last_label = ""

    def start(self) -> None:
        with self._lock:
            if self._live is not None:
                return
            self._live = _LineDiffLive(console=self.console, screen=self.screen)
            self._last_seen_size = self._console_size()
            self._watch_stop.clear()
            self._live.start(refresh=False)
            self._live.update(self.render(), refresh=True)
            self._last_refresh_at = time.time()
            self._watch_thread = threading.Thread(
                target=self._watch_terminal_loop,
                name="AlphaSolveDashboardRefresh",
                daemon=True,
            )
            self._watch_thread.start()

    def stop(self) -> None:
        self._watch_stop.set()
        thread = self._watch_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.5)

        with self._lock:
            live = self._live
            if live is not None and self._refresh_pending:
                live.update(self.render(), refresh=True)
                self._refresh_pending = False
            self._live = None
            self._watch_thread = None
        if live is not None:
            live.stop()

    def update_pool(
        self,
        *,
        capacity_verified: int | None = None,
        verified_count: int | None = None,
        solved: bool | None = None,
    ) -> None:
        del capacity_verified
        with self._lock:
            if verified_count is not None:
                self._pool_verified_count = max(0, int(verified_count))
            if solved is not None:
                self._solved = bool(solved)
            self._refresh_locked(force=True)

    def register_worker(
        self,
        worker_id: str,
        *,
        verified_ctx_size: int = 0,
        remaining_capacity: int = 0,
    ) -> None:
        del verified_ctx_size, remaining_capacity
        with self._lock:
            created = worker_id not in self._workers
            state = self._ensure_worker(worker_id)
            state.status = "running"
            state.phase = "spawned"
            state.updated_at = time.time()
            if created:
                self._append_team_log_locked(f"@worker-{worker_id} spawned")
            self._refresh_locked(force=True)

    def record_commit(
        self,
        *,
        accepted: bool,
        status: str,
        solved: bool,
        duplicate_of: int | None = None,
        verified_count: int | None = None,
    ) -> None:
        del duplicate_of
        with self._lock:
            if accepted:
                self._accepted += 1
            if status == "rejected":
                self._rejected += 1
            if verified_count is not None:
                self._pool_verified_count = max(0, int(verified_count))
            self._solved = self._solved or solved
            self._refresh_locked(force=True)

    def remove_worker(self, worker_id: str) -> None:
        with self._lock:
            self._workers.pop(worker_id, None)
            self._refresh_locked(force=True)

    def clear_worker_text(self, worker_id: str) -> None:
        """Clear output buffer for *worker_id* (e.g. between agents)."""
        with self._lock:
            state = self._ensure_worker(worker_id)
            state.output_buffer = ""
            state.updated_at = time.time()
            self._refresh_locked(force=True)

    def set_worker_model(self, worker_id: str, model: str) -> None:
        with self._lock:
            state = self._ensure_worker(worker_id)
            state.model = model
            self._refresh_locked()

    def update_phase(self, worker_id: str, phase: str, *, status: str = "running") -> None:
        with self._lock:
            state = self._ensure_worker(worker_id)
            state.phase = phase
            state.status = status
            state.updated_at = time.time()
            self._refresh_locked()

    def update_thinking(
        self, worker_id: str, *, module: str, thinking_text: str, elapsed: float
    ) -> None:
        del elapsed
        with self._lock:
            state = self._ensure_worker(worker_id)
            state.phase = module
            state.status = "thinking"
            if state.thinking_started_at == 0:
                state.thinking_started_at = time.time()
            delta = max(0, len(thinking_text) - state.thinking_token_count)
            state.thinking_token_count = len(thinking_text)
            state.thinking_text = _tail_chars(thinking_text)
            state.char_count += delta
            state.updated_at = time.time()
            self._refresh_locked(force=True)

    def finish_thinking(
        self, worker_id: str, *, module: str, elapsed: float, char_count: int
    ) -> None:
        with self._lock:
            state = self._ensure_worker(worker_id)
            state.phase = module
            state.status = "running"
            state.updated_at = time.time()
            self._append_event(
                state,
                EventType.THOUGHT,
                f"Thought for {elapsed:.1f}s {_DOT} {_fmt_count(char_count)} chars",
                style="grey50 italic",
            )
            state.thinking_started_at = 0.0
            state.thinking_token_count = 0
            state.thinking_text = ""
            self._refresh_locked(force=True)

    def append_output(self, worker_id: str, text: str) -> None:
        if not text:
            return
        with self._lock:
            state = self._ensure_worker(worker_id)
            state.output_buffer = _tail_chars(state.output_buffer + text)
            state.char_count += len(text)
            state.status = "writing"
            state.updated_at = time.time()
            self._refresh_locked(force=True)

    def flush_output(self, worker_id: str) -> None:
        """Flush accumulated assistant output into the timeline."""
        with self._lock:
            state = self._ensure_worker(worker_id)
            buf = state.output_buffer.strip()
            if buf:
                preview = _clean_inline(buf)
                self._append_event(
                    state,
                    EventType.CONTENT,
                    preview,
                    style="grey70",
                )
            state.output_buffer = ""
            state.updated_at = time.time()
            self._refresh_locked(force=True)

    def reset_stream(
        self,
        worker_id: str,
        *,
        content_chars: int = 0,
        reasoning_chars: int = 0,
        phase: str | None = None,
    ) -> None:
        with self._lock:
            state = self._ensure_worker(worker_id)
            if content_chars:
                state.output_buffer = state.output_buffer[:-content_chars] if content_chars < len(state.output_buffer) else ""
            if reasoning_chars:
                state.thinking_started_at = 0.0
                state.thinking_token_count = 0
                state.thinking_text = ""
            state.char_count = max(0, state.char_count - content_chars - reasoning_chars)
            if phase:
                state.phase = phase
            state.status = "thinking"
            state.updated_at = time.time()
            self._refresh_locked(force=True)

    def update_tool_start(
        self, worker_id: str, *, module: str, name: str, arg_preview: str
    ) -> None:
        with self._lock:
            state = self._ensure_worker(worker_id)
            state.phase = module
            state.status = "tool"
            state.active_tool = name
            state.active_tool_args = arg_preview
            state.updated_at = time.time()
            self._refresh_locked(force=True)

    def update_tool_done(self, worker_id: str, *, name: str, is_error: bool = False) -> None:
        with self._lock:
            state = self._ensure_worker(worker_id)
            args = state.active_tool_args if state.active_tool == name else ""
            marker = _CROSS if is_error else _CHECK
            style = "red" if is_error else "green"
            self._append_event(
                state,
                EventType.TOOL_DONE,
                f"{marker} {name}  {args}".rstrip(),
                style=style,
                meta={"is_error": is_error},
            )
            state.active_tool = None
            state.active_tool_args = ""
            state.status = "running"
            state.updated_at = time.time()
            self._append_team_log_locked(f"@worker-{worker_id} {marker} {name}")
            self._refresh_locked(force=True)

    def finish_worker(
        self, worker_id: str, *, status: str, solved: bool = False, summary: str = ""
    ) -> None:
        with self._lock:
            state = self._ensure_worker(worker_id)
            self.flush_output(worker_id)
            state.status = "solved" if solved else status
            state.phase = "done"
            state.result_summary = _clean_inline(summary)
            state.active_tool = None
            state.active_tool_args = ""
            state.finished_at = time.time()
            state.updated_at = time.time()
            self._worker_finished += 1
            if state.status == "failed":
                self._failed += 1
            self._append_event(
                state,
                EventType.PHASE,
                f"finished: {state.status}",
                style="bold green" if solved else "grey50",
            )
            self._append_team_log_locked(f"@worker-{worker_id} finished: {state.status}")
            self._refresh_locked(force=True)

    def log(
        self,
        worker_id: str | None,
        message: str,
        *,
        module: str | None = None,
        level: str = "INFO",
        end: str = "\n",
    ) -> None:
        if not message and end == "\n":
            return
        if worker_id is None:
            if end != "\n":
                self.append_orchestrator_output(message + end)
                return
            line = self._fmt_log(message, module=module, level=level)
            with self._lock:
                self._append_event(self._orchestrator, EventType.LOG, line, style="grey60")
                self._orchestrator.updated_at = time.time()
                self._append_team_log_locked(f"orchestrator {line}")
                self._refresh_locked()
            return

        if end != "\n":
            self.append_output(worker_id, message + end)
            return
        line = self._fmt_log(message, module=module, level=level)
        with self._lock:
            state = self._ensure_worker(worker_id)
            event_type = EventType.WARNING if level == "WARNING" else EventType.LOG
            style = "yellow" if level == "WARNING" else ("red" if level == "ERROR" else "grey60")
            self._append_event(state, event_type, line, style=style)
            state.updated_at = time.time()
            self._append_team_log_locked(f"@worker-{worker_id} {line}")
            self._refresh_locked()

    def log_digest(
        self,
        message: str,
        *,
        module: str | None = None,
        level: str = "INFO",
    ) -> None:
        if not message:
            return
        line = self._fmt_log(message, module=module, level=level)
        with self._lock:
            event_type = EventType.WARNING if level == "WARNING" else EventType.LOG
            style = "yellow" if level == "WARNING" else ("red" if level == "ERROR" else "grey60")
            self._append_event(self._digest, event_type, line, style=style)
            self._digest.updated_at = time.time()
            self._append_team_log_locked(f"digest {line}")
            self._refresh_locked()

    # -- Digest --------------------------------------------------------------

    def enqueue_digest_task(self, source_label: str) -> None:
        label = _clean_inline(source_label) or "digest task"
        with self._lock:
            self._digest_pending += 1
            self._digest_last_label = label
            if not self._digest_current_label:
                self._digest.phase = "queued"
                self._digest.status = "queued"
            self._append_event(self._digest, EventType.LOG, f"queued {label}", style="grey60")
            self._digest.updated_at = time.time()
            self._append_team_log_locked(f"digest queued {label}")
            self._refresh_locked(force=True)

    def start_digest_task(self, source_label: str) -> None:
        label = _clean_inline(source_label) or "digest task"
        with self._lock:
            self._digest_pending = max(0, self._digest_pending - 1)
            self._digest_current_label = label
            self._digest_last_label = label
            self._digest.started_at = time.time()
            self._digest.updated_at = time.time()
            self._digest.phase = "knowledge_digest"
            self._digest.status = "running"
            self._digest.char_count = 0
            self._digest.thinking_started_at = 0.0
            self._digest.thinking_token_count = 0
            self._digest.thinking_text = ""
            self._digest.active_tool = None
            self._digest.active_tool_args = ""
            self._digest.output_buffer = ""
            self._append_event(self._digest, EventType.PHASE, f"digesting {label}", style="grey50")
            self._append_team_log_locked(f"digest started {label}")
            self._refresh_locked(force=True)

    def finish_digest_task(self, *, success: bool) -> None:
        with self._lock:
            label = self._digest_current_label or self._digest_last_label
            if label:
                self._digest_last_label = label
            self._digest_current_label = ""
            self._digest_processed += 1
            self._digest.active_tool = None
            self._digest.active_tool_args = ""
            self._digest.output_buffer = ""
            if self._digest_pending > 0:
                self._digest.phase = "queued"
                self._digest.status = "queued"
            elif success:
                self._digest.phase = "idle"
                self._digest.status = "idle"
            else:
                self._digest.phase = "digest error"
                self._digest.status = "failed"
            self._digest.updated_at = time.time()
            self._append_team_log_locked(
                f"digest finished {'ok' if success else 'failed'}"
                + (f" {label}" if label else "")
            )
            self._refresh_locked(force=True)

    # -- Orchestrator --------------------------------------------------------

    def update_orchestrator_phase(self, phase: str, *, status: str = "running") -> None:
        with self._lock:
            self._orchestrator.phase = phase
            self._orchestrator.status = status
            self._orchestrator.updated_at = time.time()
            self._refresh_locked()

    def set_orchestrator_model(self, model: str) -> None:
        with self._lock:
            self._orchestrator.model = model
            self._refresh_locked()

    def update_digest_phase(self, phase: str, *, status: str = "running") -> None:
        with self._lock:
            self._digest.phase = phase
            self._digest.status = status
            self._digest.updated_at = time.time()
            self._refresh_locked()

    def set_digest_model(self, model: str) -> None:
        with self._lock:
            self._digest.model = model
            self._refresh_locked()

    def update_digest_thinking(
        self, *, module: str, thinking_text: str, elapsed: float
    ) -> None:
        del elapsed
        with self._lock:
            state = self._digest
            state.phase = module
            state.status = "thinking"
            if state.thinking_started_at == 0:
                state.thinking_started_at = time.time()
            delta = max(0, len(thinking_text) - state.thinking_token_count)
            state.thinking_token_count = len(thinking_text)
            state.thinking_text = _tail_chars(thinking_text)
            state.char_count += delta
            state.updated_at = time.time()
            self._refresh_locked(force=True)

    def finish_digest_thinking(
        self, *, module: str, elapsed: float, char_count: int
    ) -> None:
        with self._lock:
            state = self._digest
            state.phase = module
            state.status = "running"
            state.updated_at = time.time()
            self._append_event(
                state,
                EventType.THOUGHT,
                f"Thought for {elapsed:.1f}s {_DOT} {_fmt_count(char_count)} chars",
                style="grey50 italic",
            )
            state.thinking_started_at = 0.0
            state.thinking_token_count = 0
            state.thinking_text = ""
            self._refresh_locked(force=True)

    def append_digest_output(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            state = self._digest
            state.output_buffer = _tail_chars(state.output_buffer + text)
            state.char_count += len(text)
            state.status = "writing"
            state.updated_at = time.time()
            self._refresh_locked(force=True)

    def flush_digest_output(self) -> None:
        with self._lock:
            state = self._digest
            buf = state.output_buffer.strip()
            if buf:
                preview = _clean_inline(buf)
                self._append_event(state, EventType.CONTENT, preview, style="grey70")
            state.output_buffer = ""
            state.updated_at = time.time()
            self._refresh_locked(force=True)

    def reset_digest_stream(self, *, content_chars: int = 0, reasoning_chars: int = 0) -> None:
        with self._lock:
            state = self._digest
            if content_chars:
                state.output_buffer = state.output_buffer[:-content_chars] if content_chars < len(state.output_buffer) else ""
            if reasoning_chars:
                state.thinking_started_at = 0.0
                state.thinking_token_count = 0
                state.thinking_text = ""
            state.char_count = max(0, state.char_count - content_chars - reasoning_chars)
            state.status = "thinking"
            state.updated_at = time.time()
            self._refresh_locked(force=True)

    def update_digest_tool_start(self, *, module: str, name: str, arg_preview: str) -> None:
        with self._lock:
            state = self._digest
            state.phase = module
            state.status = "tool"
            state.active_tool = name
            state.active_tool_args = arg_preview
            state.updated_at = time.time()
            self._refresh_locked(force=True)

    def update_digest_tool_done(self, *, name: str, is_error: bool = False) -> None:
        with self._lock:
            state = self._digest
            args = state.active_tool_args if state.active_tool == name else ""
            marker = _CROSS if is_error else _CHECK
            style = "red" if is_error else "green"
            self._append_event(
                state,
                EventType.TOOL_DONE,
                f"{marker} {name}  {args}".rstrip(),
                style=style,
                meta={"is_error": is_error},
            )
            state.active_tool = None
            state.active_tool_args = ""
            state.status = "running"
            state.updated_at = time.time()
            self._append_team_log_locked(f"digest {marker} {name}")
            self._refresh_locked(force=True)

    def update_orchestrator_thinking(
        self, *, module: str, thinking_text: str, elapsed: float
    ) -> None:
        del elapsed
        with self._lock:
            state = self._orchestrator
            state.phase = module
            state.status = "thinking"
            if state.thinking_started_at == 0:
                state.thinking_started_at = time.time()
            delta = max(0, len(thinking_text) - state.thinking_token_count)
            state.thinking_token_count = len(thinking_text)
            state.thinking_text = _tail_chars(thinking_text)
            state.char_count += delta
            state.updated_at = time.time()
            self._refresh_locked(force=True)

    def finish_orchestrator_thinking(
        self, *, module: str, elapsed: float, char_count: int
    ) -> None:
        with self._lock:
            state = self._orchestrator
            state.phase = module
            state.status = "running"
            state.updated_at = time.time()
            self._append_event(
                state,
                EventType.THOUGHT,
                f"Thought for {elapsed:.1f}s {_DOT} {_fmt_count(char_count)} chars",
                style="grey50 italic",
            )
            state.thinking_started_at = 0.0
            state.thinking_token_count = 0
            state.thinking_text = ""
            self._refresh_locked(force=True)

    def append_orchestrator_output(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            state = self._orchestrator
            state.output_buffer = _tail_chars(state.output_buffer + text)
            state.char_count += len(text)
            state.status = "writing"
            state.updated_at = time.time()
            self._refresh_locked(force=True)

    def flush_orchestrator_output(self) -> None:
        with self._lock:
            state = self._orchestrator
            buf = state.output_buffer.strip()
            if buf:
                preview = _clean_inline(buf)
                self._append_event(state, EventType.CONTENT, preview, style="grey70")
            state.output_buffer = ""
            state.updated_at = time.time()
            self._refresh_locked(force=True)

    def reset_orchestrator_stream(
        self, *, content_chars: int = 0, reasoning_chars: int = 0
    ) -> None:
        with self._lock:
            state = self._orchestrator
            if content_chars:
                state.output_buffer = state.output_buffer[:-content_chars] if content_chars < len(state.output_buffer) else ""
            if reasoning_chars:
                state.thinking_started_at = 0.0
                state.thinking_token_count = 0
                state.thinking_text = ""
            state.char_count = max(0, state.char_count - content_chars - reasoning_chars)
            state.status = "thinking"
            state.updated_at = time.time()
            self._refresh_locked(force=True)

    def update_orchestrator_tool_start(
        self, *, module: str, name: str, arg_preview: str
    ) -> None:
        with self._lock:
            state = self._orchestrator
            state.phase = module
            state.status = "tool"
            state.active_tool = name
            state.active_tool_args = arg_preview
            state.updated_at = time.time()
            self._refresh_locked(force=True)

    def update_orchestrator_tool_done(self, *, name: str, is_error: bool = False) -> None:
        with self._lock:
            state = self._orchestrator
            args = state.active_tool_args if state.active_tool == name else ""
            marker = _CROSS if is_error else _CHECK
            style = "red" if is_error else "green"
            self._append_event(
                state,
                EventType.TOOL_DONE,
                f"{marker} {name}  {args}".rstrip(),
                style=style,
                meta={"is_error": is_error},
            )
            state.active_tool = None
            state.active_tool_args = ""
            state.status = "running"
            state.updated_at = time.time()
            marker = _CROSS if is_error else _CHECK
            self._append_team_log_locked(f"orchestrator {marker} {name}")
            self._refresh_locked(force=True)

    # -- Rendering -----------------------------------------------------------

    def render(self) -> "RenderableType":
        with self._lock:
            # 清理超过 10 分钟的已完成 worker
            now = time.time()
            to_remove = [
                wid
                for wid, s in self._workers.items()
                if s.finished_at is not None and now - s.finished_at > _WORKER_RETENTION_SECONDS
            ]
            for wid in to_remove:
                self._workers.pop(wid, None)

            width = max(80, self.console.size.width)
            height = max(24, self.console.size.height)
            elapsed = time.time() - self._started_at
            active = sum(1 for s in self._workers.values() if s.status not in _TERMINAL_STATUSES)

            body_height = max(12, height - 3)
            sidebar_width = self._sidebar_width(width)
            main_width = width - sidebar_width - (1 if sidebar_width else 0)
            orchestrator_height = max(7, min(13, body_height // 3))
            workers_height = max(7, body_height - orchestrator_height - 1)

            header = self._render_header(elapsed=elapsed, active=active, width=width)
            footer = self._render_footer(width=width)

            if sidebar_width:
                body = Table.grid(expand=True)
                body.add_column(width=sidebar_width)
                body.add_column(ratio=1)
                body.add_row(
                    self._render_sidebar(width=sidebar_width, height=body_height),
                    self._render_main(width=main_width, orchestrator_height=orchestrator_height, workers_height=workers_height),
                )
            else:
                body = self._render_main(
                    width=width,
                    orchestrator_height=orchestrator_height,
                    workers_height=workers_height,
                )

            return Group(header, Rule(style="grey23"), body, footer)

    def _render_header(self, *, elapsed: float, active: int, width: int) -> Text:
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append("AlphaSolve", "bold green" if self._solved else "bold white")
        t.append("  native dashboard  ", "grey50")
        self._append_stat(t, "running", str(active), "cyan")
        self._append_stat(t, "done", str(self._worker_finished), "green" if self._worker_finished else "grey50")
        self._append_stat(t, "verified", str(self._pool_verified_count), "green" if self._pool_verified_count else "grey50")
        self._append_stat(t, "rejected", str(self._rejected), "yellow" if self._rejected else "grey50")
        self._append_stat(t, "failed", str(self._failed), "red" if self._failed else "grey50")
        self._append_stat(t, "elapsed", _fmt_elapsed(elapsed), "grey50")
        if cell_len(t.plain) <= width:
            return t
        return Text(_truncate(t.plain, width), style="")

    def _render_footer(self, *, width: int) -> Text:
        text = "grey CoT tails  |  tool status ✓/✗  |  Ctrl+C to stop"
        return Text(_truncate(text, width), style="grey50")

    def _render_sidebar(self, *, width: int, height: int) -> "RenderableType":
        team_height, digest_height = self._sidebar_heights(height)
        sidebar = Table.grid(expand=True)
        sidebar.add_column(ratio=1)
        sidebar.add_row(self._render_team_sidebar(width=width, height=team_height))
        sidebar.add_row(self._render_digest_sidebar(width=width, height=digest_height))
        return sidebar

    def _render_team_sidebar(self, *, width: int, height: int) -> Panel:
        workers = sorted(self._workers.values(), key=lambda s: s.worker_id)
        lines: list[Text] = []

        lines.append(_text_line("Team", "bold"))
        lines.append(self._metric("started", self._worker_started, "cyan"))
        lines.append(self._metric("accepted", self._accepted, "green"))
        lines.append(self._metric("verified pool", self._pool_verified_count, "green"))
        lines.append(Text(""))

        lines.append(_text_line("Agents", "bold"))
        if not workers:
            lines.append(_text_line("  waiting for workers", "grey50 italic"))
        else:
            for state in workers[-max(1, min(len(workers), 8)): ]:
                icon, style = _STATUS_ICON.get(state.status, ("○", "grey50"))
                line = Text(no_wrap=True, overflow="ellipsis")
                line.append(f"{icon} ", style)
                line.append(f"@worker-{_worker_label(state.worker_id)}", state.color)
                line.append(f" {_truncate(state.phase, max(4, width - 20))}", "grey60")
                lines.append(line)

        remaining = max(2, height - len(lines) - 4)
        lines.append(Text(""))
        lines.append(_text_line("Recent", "bold"))
        for item in self._team_log[-remaining:]:
            lines.append(_text_line("  " + _truncate(item, width - 6), "grey60"))

        return Panel(
            Group(*lines),
            title="[grey50]team[/]",
            title_align="left",
            border_style="grey35",
            box=box.ROUNDED,
            padding=(0, 1),
            height=height,
        )

    def _render_digest_sidebar(self, *, width: int, height: int) -> Panel:
        return self._render_agent_panel(
            self._digest,
            title="@knowledge-digest",
            color="bright_blue",
            width=width,
            height=height,
        )

    def _render_main(self, *, width: int, orchestrator_height: int, workers_height: int) -> "RenderableType":
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1)
        grid.add_row(self._render_agent_panel(
            self._orchestrator,
            title="@orchestrator",
            color="bright_cyan",
            width=width,
            height=orchestrator_height,
        ))
        grid.add_row(self._render_worker_grid(width=width, height=workers_height))
        return grid

    def _render_worker_grid(self, *, width: int, height: int) -> "RenderableType":
        states = sorted(self._workers.values(), key=lambda s: s.worker_id)
        if not states:
            return Panel(
                Text("Waiting for workers…", style="grey50 italic"),
                title="[grey50]workers[/]",
                title_align="left",
                border_style="grey35",
                box=box.ROUNDED,
                padding=(0, 1),
                height=height,
            )

        columns = self._worker_columns(width, len(states))
        rows = (len(states) + columns - 1) // columns
        tile_height = max(6, (height - max(0, rows - 1)) // rows)
        tile_width = max(24, width // columns - 1)

        table = Table.grid(expand=True)
        for _ in range(columns):
            table.add_column(ratio=1)

        for row_index in range(rows):
            row_renderables = []
            for col_index in range(columns):
                index = row_index * columns + col_index
                if index >= len(states):
                    row_renderables.append(Text(""))
                    continue
                state = states[index]
                row_renderables.append(
                    self._render_agent_panel(
                        state,
                        title=f"@worker-{_worker_label(state.worker_id)}",
                        color=state.color,
                        width=tile_width,
                        height=tile_height,
                    )
                )
            table.add_row(*row_renderables)
        return table

    def _render_agent_panel(
        self,
        state: WorkerRenderState | OrchestratorRenderState,
        *,
        title: str,
        color: str,
        width: int,
        height: int,
    ) -> Panel:
        content_width = max(10, width - 4)
        max_lines = max(3, height - 3)
        elapsed = time.time() - state.started_at
        icon, icon_style = _STATUS_ICON.get(state.status, ("○", "grey50"))
        lines: list[Text] = []

        # Header line
        model_label = getattr(state, "model", "") or ""
        status = Text(no_wrap=True, overflow="ellipsis")
        status.append(f"{icon} ", icon_style)
        status.append(_truncate(state.phase, max(6, content_width - 20)), "bold")
        if model_label:
            status.append(f"  {model_label}", "bright_black")
        status.append(f"  {_fmt_elapsed(elapsed)}", "grey50")
        status.append(f"  ↑{_fmt_count(state.char_count)}", "grey50")
        lines.append(status)

        # Timeline events (oldest first, newest last)
        timeline_lines = self._render_timeline(state, width=content_width, max_lines=max_lines - 1)
        lines.extend(timeline_lines)

        # Active indicators (thinking / tool) at the bottom
        active_lines = self._render_active_lines(state, width=content_width)
        # Ensure we don't overflow; if we do, drop old timeline lines
        while len(lines) + len(active_lines) > max_lines and len(lines) > 1:
            lines.pop(1)  # remove oldest timeline line, keep header
        lines.extend(active_lines)

        if len(lines) < max_lines and not state.timeline and not state.active_tool and state.status not in ("thinking", "tool"):
            lines.append(_text_line("waiting for activity", "grey50 italic"))

        subtitle = self._panel_subtitle(state)
        return Panel(
            Group(*lines[:max_lines]),
            title=f"[{color}]{title}[/]",
            title_align="right",
            subtitle=subtitle,
            subtitle_align="left",
            border_style=color,
            box=box.ROUNDED,
            padding=(0, 1),
            height=height,
        )

    def _render_timeline(
        self,
        state: WorkerRenderState | OrchestratorRenderState,
        *,
        width: int,
        max_lines: int,
    ) -> list[Text]:
        if max_lines <= 0:
            return []
        rendered: list[Text] = []
        for event in list(state.timeline)[-max_lines:]:
            text = _truncate(event.text, width)
            rendered.append(_text_line(text, event.style or "grey70"))
        return rendered

    def _render_active_lines(
        self,
        state: WorkerRenderState | OrchestratorRenderState,
        *,
        width: int,
    ) -> list[Text]:
        lines: list[Text] = []
        if state.status == "thinking" and state.thinking_started_at > 0:
            thinking_elapsed = time.time() - state.thinking_started_at
            count_str = _fmt_count(state.thinking_token_count)
            line = Text(no_wrap=True, overflow="ellipsis")
            line.append("Thinking", "italic")
            frame = _bullet_frame_for(thinking_elapsed)
            line.append(f" {frame}", "cyan")
            line.append(f"  {_fmt_elapsed(thinking_elapsed)}", "grey50")
            line.append(f" · {count_str} chars", "grey50")
            if thinking_elapsed > 0.5 and state.thinking_token_count > 0:
                rate = int(state.thinking_token_count / thinking_elapsed)
                if rate > 0:
                    line.append(f" · {rate} char/s", "grey50")
            lines.append(line)

            # kimi-cli style: show a small preview of the latest reasoning text
            if state.thinking_text:
                for raw in _tail_lines(state.thinking_text, 3):
                    if raw.strip():
                        lines.append(_text_line(_truncate(raw, width), "grey50 italic"))

        if state.active_tool:
            line = Text(no_wrap=True, overflow="ellipsis")
            line.append(f"{_PLAY} Using ", "yellow")
            line.append(state.active_tool, "bold blue")
            if state.active_tool_args:
                line.append("  " + _truncate(state.active_tool_args, max(4, width - cell_len(line.plain) - 2)), "grey50")
            lines.append(line)
        if state is self._digest:
            if self._digest_current_label:
                lines.append(_text_line(_truncate(f"Source {self._digest_current_label}", width), "grey50"))
            elif self._digest_last_label:
                lines.append(_text_line(_truncate(f"Last {self._digest_last_label}", width), "grey50"))
            queue_line = Text(no_wrap=True, overflow="ellipsis")
            queue_line.append("Queue ", "grey50")
            queue_line.append(str(self._digest_pending), "cyan" if self._digest_pending else "grey50")
            queue_line.append(" pending", "grey50")
            queue_line.append("  done ", "grey50")
            queue_line.append(str(self._digest_processed), "green" if self._digest_processed else "grey50")
            lines.append(queue_line)
        return lines

    def _panel_subtitle(self, state: WorkerRenderState | OrchestratorRenderState) -> str:
        if state is self._digest:
            status = "[red]last run failed[/]" if state.status == "failed" else "[grey50]live[/]"
            if state.active_tool:
                status = "[yellow]tool running[/]"
            elif state.status == "queued":
                status = "[grey50]queued[/]"
            elif state.status == "idle":
                status = "[grey50]idle[/]"
            return (
                f"{status} [grey35]|[/] [grey50]queue {self._digest_pending}[/] "
                f"[grey35]|[/] [grey50]done {self._digest_processed}[/]"
            )
        if state.active_tool:
            return "[yellow]tool running[/]"
        if state.status in _TERMINAL_STATUSES:
            return f"[grey50]{state.status}[/]"
        # Show the most recent event type as a hint
        if state.timeline:
            last = state.timeline[-1]
            if last.type == EventType.TOOL_DONE:
                return "[red]last tool failed[/]" if last.meta.get("is_error") else "[green]last tool ok[/]"
        return "[grey50]live[/]"

    def _ensure_worker(self, worker_id: str) -> WorkerRenderState:
        if worker_id not in self._workers:
            self._workers[worker_id] = WorkerRenderState(
                worker_id=worker_id,
                color=_TEAM_COLORS[hash(worker_id) % len(_TEAM_COLORS)],
            )
            self._worker_started += 1
        return self._workers[worker_id]

    def _append_event(
        self,
        state: WorkerRenderState | OrchestratorRenderState,
        event_type: EventType,
        text: str,
        style: str = "",
        meta: dict | None = None,
    ) -> None:
        state.timeline.append(
            TimelineEvent(
                type=event_type,
                timestamp=time.time(),
                text=text,
                style=style,
                meta=meta or {},
            )
        )

    def _fmt_log(self, message: str, *, module: str | None, level: str) -> str:
        prefix = f"[{level.lower()}]" if level != "INFO" else ""
        if module:
            prefix = f"{prefix} {module}".strip()
        clean = _clean_inline(message)
        return f"{prefix}  {clean}".strip() if prefix else clean

    def _metric(self, label: str, value: int, style: str) -> Text:
        line = Text(no_wrap=True, overflow="ellipsis")
        line.append(f"{label:<14}", "grey50")
        line.append(str(value), style if value else "grey50")
        return line

    def _append_stat(self, text: Text, label: str, value: str, value_style: str) -> None:
        text.append(f"{label} ", "grey50")
        text.append(value, value_style)
        text.append("  ", "")

    def _append_team_log_locked(self, line: str) -> None:
        self._team_log.append(_clean_inline(line))
        self._team_log = self._team_log[-_MAX_TEAM_LOG_LINES:]

    def _sidebar_width(self, width: int) -> int:
        if width < 100:
            return 0
        return max(28, min(42, width // 4))

    def _sidebar_heights(self, height: int) -> tuple[int, int]:
        digest_height = max(8, min(12, height // 3))
        team_height = max(8, height - digest_height)
        return team_height, max(8, height - team_height)

    def _worker_columns(self, width: int, count: int) -> int:
        if count <= 1 or width < 86:
            return 1
        if width >= 156 and count >= 3:
            return 3
        return 2

    def _console_size(self) -> tuple[int, int]:
        size = self.console.size
        return (max(80, size.width), max(24, size.height))

    def _watch_terminal_loop(self) -> None:
        while not self._watch_stop.wait(_RESIZE_POLL_INTERVAL):
            with self._lock:
                self._refresh_for_resize_or_pending_locked()

    def _refresh_for_resize_or_pending_locked(self) -> None:
        if self._live is None:
            return

        now = time.time()
        current_size = self._console_size()
        if current_size != self._last_seen_size:
            self._last_seen_size = current_size
            self._paint_now_locked(now=now)
            return

        if self._refresh_pending and now - self._last_refresh_at >= self._min_refresh_interval:
            self._paint_now_locked(now=now)

    def _paint_now_locked(self, *, now: float | None = None) -> None:
        if self._live is None:
            return
        self._last_seen_size = self._console_size()
        self._last_refresh_at = time.time() if now is None else now
        self._refresh_pending = False
        self._live.update(self.render(), refresh=True)

    def _refresh_locked(self, *, force: bool = False) -> None:
        if self._live is None:
            return
        now = time.time()
        if force:
            self._paint_now_locked(now=now)
            return
        if now - self._last_refresh_at < self._min_refresh_interval:
            self._refresh_pending = True
            return
        self._paint_now_locked(now=now)


# ---------------------------------------------------------------------------
# Animated bullet frame for thinking spinner (kimi-cli style)
# ---------------------------------------------------------------------------

_BULLET_FRAMES = (".  ", ".. ", "...", " ..", "  .", "   ")
_BULLET_FRAME_INTERVAL = 0.13


def _bullet_frame_for(elapsed: float) -> str:
    idx = int(elapsed / _BULLET_FRAME_INTERVAL) % len(_BULLET_FRAMES)
    return _BULLET_FRAMES[idx]


def __getattr__(name: str):
    if name == "LemmaTeamRenderer":
        import warnings
        warnings.warn("LemmaTeamRenderer is deprecated, use PropositionTeamRenderer instead.", DeprecationWarning, stacklevel=2)
        return PropositionTeamRenderer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
