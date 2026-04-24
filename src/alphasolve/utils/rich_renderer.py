from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rich import box
from rich.cells import cell_len, set_cell_size
from rich.console import Console, Group
from rich.columns import Columns
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

if TYPE_CHECKING:
    from rich.console import RenderableType

RICH_CONSOLE = Console()

_DISPLAY_CHAR_LIMIT = 16000
_ORCHESTRATOR_LOG_LINES = 20
_TERMINAL_STATUSES = {"verified", "rejected", "failed", "solved"}

# 每个 worker 槽位对应一个固定颜色。
_TEAM_COLORS = ("cyan", "magenta", "green", "yellow", "blue", "bright_cyan", "bright_magenta", "bright_green")

# 状态名到展示符号和样式的映射。
_STATUS_ICON: dict[str, tuple[str, str]] = {
    "queued":   ("○", "grey50"),
    "running":  ("●", "cyan"),
    "thinking": ("●", "magenta"),
    "tool":     ("●", "yellow"),
    "writing":  ("●", "bright_cyan"),
    "verified": ("✓", "green"),
    "solved":   ("✓", "bold green"),
    "rejected": ("✗", "yellow"),
    "failed":   ("✗", "red"),
}

_DIRECTION_UP   = "↑"
_DIRECTION_DOWN = "↓"
_PLAY            = "▶"
_PAUSE           = "⏸"
_SEP             = "·"


def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m{s % 60:02d}s"


def _fmt_chars(n: int) -> str:
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


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
    if not text:
        return []
    lines = text.splitlines()
    return lines[-n:] if len(lines) > n else lines


@dataclass
class WorkerRenderState:
    worker_id: int
    color: str
    status: str = "queued"
    phase: str = "starting"
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    thinking_text: str = ""
    output_text: str = ""
    active_tool: str | None = None
    active_tool_args: str = ""
    last_tool: str | None = None
    last_tool_error: bool = False
    log_lines: list[str] = field(default_factory=list)
    result_summary: str = ""
    char_count: int = 0  # 已累计输出的字符数


@dataclass
class OrchestratorRenderState:
    status: str = "idle"
    phase: str = "orchestrator"
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    thinking_text: str = ""
    output_text: str = ""
    active_tool: str | None = None
    active_tool_args: str = ""
    last_tool: str | None = None
    last_tool_error: bool = False
    log_lines: list[str] = field(default_factory=list)


class LemmaTeamRenderer:
    """Rich Live dashboard for AlphaSolve agent team."""

    def __init__(
        self,
        *,
        console: Console = RICH_CONSOLE,
        refresh_per_second: int = 4,
        max_log_lines: int = 6,
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
        self._live: Live | None = None
        self._started_at = time.time()
        self._last_refresh_at = 0.0
        self._pool_verified_count = 0
        self._worker_started = 0
        self._worker_finished = 0
        self._accepted = 0
        self._rejected = 0
        self._failed = 0
        self._solved = False

    # ── lifecycle ──────────────────────────────────────────────────────────

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

    # ── pool-level updates ─────────────────────────────────────────────────

    def update_pool(
        self,
        *,
        capacity_verified: int | None = None,
        verified_count: int | None = None,
        solved: bool | None = None,
    ) -> None:
        with self._lock:
            if verified_count is not None:
                self._pool_verified_count = max(0, int(verified_count))
            if solved is not None:
                self._solved = bool(solved)
            self._refresh_locked(force=True)

    def register_worker(self, worker_id: int, *, verified_ctx_size: int = 0, remaining_capacity: int = 0) -> None:
        with self._lock:
            if worker_id not in self._workers:
                self._workers[worker_id] = WorkerRenderState(
                    worker_id=worker_id,
                    color=_TEAM_COLORS[worker_id % len(_TEAM_COLORS)],
                )
                self._worker_started += 1
            state = self._workers[worker_id]
            state.status = "running"
            state.phase = "spawned"
            state.updated_at = time.time()
            self._refresh_locked(force=True)

    def record_commit(self, *, accepted: bool, status: str, solved: bool, duplicate_of: int | None = None, verified_count: int | None = None) -> None:
        with self._lock:
            if accepted:
                self._accepted += 1
            if status == "rejected":
                self._rejected += 1
            if verified_count is not None:
                self._pool_verified_count = max(0, int(verified_count))
            self._solved = self._solved or solved
            self._refresh_locked(force=True)

    def remove_worker(self, worker_id: int) -> None:
        with self._lock:
            self._workers.pop(worker_id, None)
            self._refresh_locked(force=True)

    # ── per-worker updates ─────────────────────────────────────────────────

    def update_phase(self, worker_id: int, phase: str, *, status: str = "running") -> None:
        with self._lock:
            state = self._ensure_worker(worker_id)
            state.phase = phase
            state.status = status
            state.updated_at = time.time()
            self._refresh_locked()

    def update_thinking(self, worker_id: int, *, module: str, thinking_text: str, elapsed: float) -> None:
        del elapsed
        with self._lock:
            state = self._ensure_worker(worker_id)
            state.phase = module
            state.status = "thinking"
            state.thinking_text = _tail_chars(thinking_text)
            state.char_count = max(state.char_count, len(thinking_text))
            state.updated_at = time.time()
            self._refresh_locked()

    def finish_thinking(self, worker_id: int, *, module: str, elapsed: float, char_count: int) -> None:
        with self._lock:
            state = self._ensure_worker(worker_id)
            state.phase = module
            state.status = "running"
            state.thinking_text = ""
            state.char_count += char_count
            state.log_lines.append(f"thought {elapsed:.1f}s · {_fmt_chars(char_count)} chars")
            state.log_lines = state.log_lines[-self.max_log_lines:]
            state.updated_at = time.time()
            self._refresh_locked(force=True)

    def append_output(self, worker_id: int, text: str) -> None:
        if not text:
            return
        with self._lock:
            state = self._ensure_worker(worker_id)
            state.output_text = _tail_chars(state.output_text + text)
            state.char_count += len(text)
            state.status = "writing"
            state.updated_at = time.time()
            self._refresh_locked()

    def update_tool_start(self, worker_id: int, *, module: str, name: str, arg_preview: str) -> None:
        with self._lock:
            state = self._ensure_worker(worker_id)
            state.phase = module
            state.status = "tool"
            state.active_tool = name
            state.active_tool_args = arg_preview
            state.updated_at = time.time()
            self._refresh_locked()

    def update_tool_done(self, worker_id: int, *, name: str, is_error: bool = False) -> None:
        with self._lock:
            state = self._ensure_worker(worker_id)
            state.last_tool = name
            state.last_tool_error = is_error
            state.active_tool = None
            state.active_tool_args = ""
            state.status = "running"
            state.updated_at = time.time()
            self._refresh_locked(force=True)

    def finish_worker(self, worker_id: int, *, status: str, solved: bool = False, summary: str = "") -> None:
        with self._lock:
            state = self._ensure_worker(worker_id)
            state.status = "solved" if solved else status
            state.phase = "done"
            state.result_summary = summary
            state.active_tool = None
            state.thinking_text = ""
            state.updated_at = time.time()
            self._worker_finished += 1
            if state.status == "failed":
                self._failed += 1
            self._refresh_locked(force=True)

    def log(self, worker_id: int | None, message: str, *, module: str | None = None, level: str = "INFO", end: str = "\n") -> None:
        if not message and end == "\n":
            return
        if worker_id is None:
            if end != "\n":
                self.append_orchestrator_output(message + end)
                return
            line = self._fmt_log(message, module=module, level=level)
            with self._lock:
                self._orchestrator.log_lines.append(line)
                self._orchestrator.log_lines = self._orchestrator.log_lines[-_ORCHESTRATOR_LOG_LINES:]
                self._orchestrator.updated_at = time.time()
                self._refresh_locked()
            return
        if end != "\n":
            self.append_output(worker_id, message + end)
            return
        line = self._fmt_log(message, module=module, level=level)
        with self._lock:
            state = self._ensure_worker(worker_id)
            state.log_lines.append(line)
            state.log_lines = state.log_lines[-self.max_log_lines:]
            state.updated_at = time.time()
            self._refresh_locked()

    # ── orchestrator updates ───────────────────────────────────────────────

    def update_orchestrator_phase(self, phase: str, *, status: str = "running") -> None:
        with self._lock:
            self._orchestrator.phase = phase
            self._orchestrator.status = status
            self._orchestrator.updated_at = time.time()
            self._refresh_locked()

    def update_orchestrator_thinking(self, *, module: str, thinking_text: str, elapsed: float) -> None:
        del elapsed
        with self._lock:
            self._orchestrator.phase = module
            self._orchestrator.status = "thinking"
            self._orchestrator.thinking_text = _tail_chars(thinking_text)
            self._orchestrator.updated_at = time.time()
            self._refresh_locked()

    def finish_orchestrator_thinking(self, *, module: str, elapsed: float, char_count: int) -> None:
        with self._lock:
            self._orchestrator.phase = module
            self._orchestrator.status = "running"
            self._orchestrator.thinking_text = ""
            self._orchestrator.log_lines.append(f"thought {elapsed:.1f}s · {_fmt_chars(char_count)} chars")
            self._orchestrator.log_lines = self._orchestrator.log_lines[-_ORCHESTRATOR_LOG_LINES:]
            self._orchestrator.updated_at = time.time()
            self._refresh_locked(force=True)

    def append_orchestrator_output(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._orchestrator.output_text = _tail_chars(self._orchestrator.output_text + text)
            self._orchestrator.status = "writing"
            self._orchestrator.updated_at = time.time()
            self._refresh_locked()

    def update_orchestrator_tool_start(self, *, module: str, name: str, arg_preview: str) -> None:
        with self._lock:
            self._orchestrator.phase = module
            self._orchestrator.status = "tool"
            self._orchestrator.active_tool = name
            self._orchestrator.active_tool_args = arg_preview
            self._orchestrator.updated_at = time.time()
            self._refresh_locked()

    def update_orchestrator_tool_done(self, *, name: str, is_error: bool = False) -> None:
        with self._lock:
            self._orchestrator.last_tool = name
            self._orchestrator.last_tool_error = is_error
            self._orchestrator.active_tool = None
            self._orchestrator.active_tool_args = ""
            self._orchestrator.status = "running"
            self._orchestrator.updated_at = time.time()
            self._refresh_locked(force=True)

    # ── rendering ──────────────────────────────────────────────────────────

    def render(self) -> "RenderableType":
        with self._lock:
            elapsed = time.time() - self._started_at
            active = sum(1 for s in self._workers.values() if s.status not in _TERMINAL_STATUSES)
            header = self._render_header(elapsed=elapsed, active=active)
            rule = Rule(style="grey23")
            orch = self._render_orchestrator_panel()
            workers = self._render_worker_list()
            cols = Columns([orch, workers], equal=False, expand=True)
            return Group(header, rule, cols)

    def _render_header(self, *, elapsed: float, active: int) -> Text:
        title_style = "bold green" if self._solved else "bold white"
        t = Text()
        t.append("AlphaSolve", title_style)
        t.append("  ", "")
        _stat(t, "running", str(active), "cyan")
        _stat(t, "done", str(self._worker_finished), "green" if self._worker_finished else "grey50")
        _stat(t, "verified", str(self._pool_verified_count), "green" if self._pool_verified_count else "grey50")
        _stat(t, "rejected", str(self._rejected), "yellow" if self._rejected else "grey50")
        _stat(t, "failed", str(self._failed), "red" if self._failed else "grey50")
        _stat(t, "elapsed", _fmt_elapsed(elapsed), "grey50")
        return t

    def _render_orchestrator_panel(self) -> Panel:
        state = self._orchestrator
        elapsed = time.time() - state.started_at
        icon, icon_style = _STATUS_ICON.get(state.status, ("●", "grey50"))
        width = max(30, self.console.size.width // 3)
        content_width = width - 4

        lines: list[Text] = []

        # 状态行
        status_line = Text()
        status_line.append(f"{icon} ", icon_style)
        status_line.append(_truncate(state.phase, content_width - 12), "bold")
        status_line.append(f"  {_fmt_elapsed(elapsed)}", "grey50")
        lines.append(status_line)

        # 当前工具或最近一次工具调用
        if state.active_tool:
            tl = Text()
            tl.append(f"{_PLAY} ", "yellow")
            tl.append(state.active_tool, "bold blue")
            arg = _truncate(state.active_tool_args, max(8, content_width - len(state.active_tool) - 6))
            if arg:
                tl.append(f"  {arg}", "grey50")
            lines.append(tl)
        elif state.last_tool:
            tl = Text()
            err_style = "red" if state.last_tool_error else "green"
            tl.append("✓ " if not state.last_tool_error else "✗ ", err_style)
            tl.append(state.last_tool, "blue")
            lines.append(tl)

        # 预览优先级：thinking > output > log
        available = max(4, self.console.size.height - 6 - len(lines))
        preview_lines = self._orch_preview_lines(available, content_width)
        for pl in preview_lines:
            lines.append(pl)

        return Panel(
            Group(*lines),
            title="[grey50]orchestrator[/]",
            title_align="left",
            border_style="grey35",
            box=box.ROUNDED,
            padding=(0, 1),
            width=width,
        )

    def _render_worker_list(self) -> "RenderableType":
        states = sorted(self._workers.values(), key=lambda s: s.worker_id)
        if not states:
            return Text("  Waiting for lemma workers…", style="grey50 italic")

        width = max(40, self.console.size.width - self.console.size.width // 3 - 4)
        rows: list[Text] = []
        for state in states:
            rows.append(self._render_worker_row(state, width=width))
            # 第二行展示预览。
            preview = self._worker_preview(state, width=width - 4)
            if preview:
                rows.append(preview)
            rows.append(Text(""))  # 空行分隔不同 worker

        return Group(*rows)

    def _render_worker_row(self, state: WorkerRenderState, *, width: int) -> Text:
        elapsed = time.time() - state.started_at
        icon, icon_style = _STATUS_ICON.get(state.status, ("●", "grey50"))
        color = state.color

        t = Text()
        t.append(f"{icon} ", icon_style)
        t.append(f"worker-{state.worker_id:02d}", f"bold {color}")
        t.append(f"  {_truncate(state.phase, 14)}", "bold")

        # 活动状态指示
        if state.active_tool:
            t.append(f"  {_PLAY} ", "yellow")
            t.append(state.active_tool, "blue")
            arg = _truncate(state.active_tool_args, max(8, width - 50))
            if arg:
                t.append(f" ({arg})", "grey50")
        elif state.last_tool:
            err_style = "red" if state.last_tool_error else "green"
            marker = "✗" if state.last_tool_error else "✓"
            t.append(f"  {marker} ", err_style)
            t.append(state.last_tool, "grey70")
        elif state.result_summary:
            t.append(f"  {_truncate(state.result_summary, max(10, width - 40))}", "grey70")

        # 右侧展示耗时和字符数。
        suffix = f"  {_DIRECTION_UP}{_fmt_chars(state.char_count)}  {_fmt_elapsed(elapsed)}"
        t.append(suffix, "grey50")

        return t

    def _worker_preview(self, state: WorkerRenderState, *, width: int) -> Text | None:
        if state.status in _TERMINAL_STATUSES:
            return None
        if state.thinking_text:
            lines = _tail_lines(state.thinking_text, 6)
            non_empty = [_truncate(l, width) for l in lines if l.strip()]
            if not non_empty:
                return None
            return Group(*[Text(f"  {l}", style="grey50 italic", no_wrap=True, overflow="ellipsis") for l in non_empty])
        if state.output_text:
            lines = _tail_lines(state.output_text, 2)
            preview = "  " + "  ".join(_truncate(l, width) for l in lines if l.strip())
            return Text(preview, style="grey70", no_wrap=True, overflow="ellipsis")
        if state.log_lines:
            last = state.log_lines[-1]
            return Text(f"  {_truncate(last, width)}", style="grey50", no_wrap=True, overflow="ellipsis")
        return None

    def _orch_preview_lines(self, n: int, width: int) -> list[Text]:
        state = self._orchestrator
        if state.thinking_text:
            lines = _tail_lines(state.thinking_text, n)
            return [Text(_truncate(l, width), style="grey50 italic", no_wrap=True, overflow="ellipsis") for l in lines if l.strip()]
        if state.output_text:
            lines = _tail_lines(state.output_text, n)
            return [Text(_truncate(l, width), style="grey70", no_wrap=True, overflow="ellipsis") for l in lines if l.strip()]
        if state.log_lines:
            lines = state.log_lines[-n:]
            return [Text(_truncate(l, width), style="grey50", no_wrap=True, overflow="ellipsis") for l in lines]
        return []

    # ── helpers ────────────────────────────────────────────────────────────

    def _ensure_worker(self, worker_id: int) -> WorkerRenderState:
        if worker_id not in self._workers:
            self._workers[worker_id] = WorkerRenderState(
                worker_id=worker_id,
                color=_TEAM_COLORS[worker_id % len(_TEAM_COLORS)],
            )
            self._worker_started += 1
        return self._workers[worker_id]

    def _fmt_log(self, message: str, *, module: str | None, level: str) -> str:
        prefix = f"[{level.lower()}]" if level != "INFO" else ""
        if module:
            prefix = f"{prefix} {module}".strip()
        clean = message.strip()
        return f"{prefix}  {clean}".strip() if prefix else clean

    def _refresh_locked(self, *, force: bool = False) -> None:
        if self._live is None:
            return
        now = time.time()
        if not force and now - self._last_refresh_at < self._min_refresh_interval:
            return
        self._last_refresh_at = now
        if not getattr(self._live, "auto_refresh", False):
            self._live.update(self.render(), refresh=True)


def _stat(t: Text, label: str, value: str, value_style: str) -> None:
    t.append(f"{label} ", "grey50")
    t.append(value, value_style)
    t.append("  ", "")
