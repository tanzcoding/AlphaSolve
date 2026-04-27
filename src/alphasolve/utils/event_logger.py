from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

AgentEventSink = Callable[[dict[str, Any]], None]

_TRUNCATE_RESULT_BYTES = 8_000
_TRUNCATE_THINKING_CHARS = 12_000
_TRUNCATE_CONTENT_CHARS = 8_000


def compose_event_sinks(*sinks: AgentEventSink | None) -> AgentEventSink | None:
    """Combine multiple event sinks into one callable.

    None entries are silently dropped.  Returns ``None`` when every entry is
    ``None`` so callers can skip event emission entirely.
    """
    live = [s for s in sinks if s is not None]
    if not live:
        return None
    if len(live) == 1:
        return live[0]

    def _broadcast(event: dict[str, Any]) -> None:
        for sink in live:
            try:
                sink(event)
            except Exception:
                pass

    return _broadcast


class EventLogWriter:
    """Writes agent lifecycle events to a human-readable, grep-friendly log file.

    The writer is a callable that can be used directly as an
    ``AgentEventSink``.  Call :meth:`close` when the owning agent scope
    ends so that the file handle is flushed and released.
    """

    def __init__(
        self,
        log_path: Path,
        *,
        scope: str = "",
    ) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._scope = scope
        self._turn = 0
        self._phase = ""
        self._log = open(log_path, "w", encoding="utf-8")
        self._agent_start: float | None = None
        self._turn_start: float | None = None
        self._tool_times: dict[str, float] = {}
        self._opened = True

    # -- callable interface (AgentEventSink) ---------------------------------

    def __call__(self, event: dict[str, Any]) -> None:
        if not self._opened:
            return
        handler = _EVENT_HANDLERS.get(event.get("type") or "")
        if handler is not None:
            handler(self, event)

    def close(self) -> None:
        if not self._opened:
            return
        self._opened = False
        self._log.close()

    # -- human-readable event handlers ---------------------------------------

    def _handle_run_start(self, event: dict[str, Any]) -> None:
        import time as _time
        self._agent_start = _time.time()
        self._phase = str(event.get("agent") or "")
        self._emit_header(self._phase)
        tools = event.get("enabled_tools") or event.get("tools") or []
        if tools:
            tool_names = ", ".join(tools)
            self._log.write(f"  tools: {tool_names}\n\n")
        else:
            self._log.write("\n")

    def _handle_model_request(self, event: dict[str, Any]) -> None:
        import time as _time
        self._turn_start = _time.time()
        self._turn = int(event.get("turn") or self._turn + 1)

    def _handle_model_retry(self, event: dict[str, Any]) -> None:
        attempt = event.get("attempt", "?")
        error = str(event.get("error") or event.get("error_detail") or "")
        self._write_indented(f"[retry] attempt {attempt}: {error}\n")

    def _handle_thinking(self, event: dict[str, Any]) -> None:
        content = str(event.get("content") or "")
        if not content.strip():
            return
        self._write_section("thinking", content, char_limit=_TRUNCATE_THINKING_CHARS)

    def _handle_assistant_message(self, event: dict[str, Any]) -> None:
        content = str(event.get("content") or "")
        tool_count = int(event.get("tool_call_count") or 0)
        # Only log free-text assistant content when there are no tool calls
        # (pure text response) — otherwise the thinking already covers intent.
        if not tool_count and content.strip():
            self._write_section("message", content, char_limit=_TRUNCATE_CONTENT_CHARS)

    def _handle_tool_call(self, event: dict[str, Any]) -> None:
        import time as _time
        name = str(event.get("name") or "?")
        args = event.get("arguments")
        raw = event.get("raw_arguments")
        tool_id = event.get("tool_call_id") or name
        self._tool_times[tool_id] = _time.time()
        self._log.write(f"  [tool] {name}\n")
        arg_str = _format_args(args, raw)
        if arg_str:
            self._log.write(f"    args: {arg_str}\n")

    def _handle_tool_result(self, event: dict[str, Any]) -> None:
        import time as _time
        name = str(event.get("name") or "?")
        tool_id = event.get("tool_call_id") or name
        elapsed = ""
        started = self._tool_times.pop(tool_id, None)
        if started is not None:
            elapsed = f"{_time.time() - started:.1f}s, "
        is_error = bool(event.get("is_error"))
        content = str(event.get("content") or "")
        tag = "error" if is_error else "result"
        short = _truncate_result(content)
        self._log.write(f"    {tag} ({elapsed}{len(content)} bytes): {short}\n\n")

    def _handle_run_finish(self, event: dict[str, Any]) -> None:
        import time as _time
        elapsed_str = ""
        if self._agent_start is not None:
            elapsed_str = f" · {_time.time() - self._agent_start:.1f}s"
        answer = str(event.get("final_answer") or "")
        reason = event.get("reason")
        self._log.write(f"  [done]{elapsed_str}\n")
        if reason:
            self._log.write(f"    reason: {reason}\n")
        if answer.strip():
            self._write_indented_block("final", answer, char_limit=_TRUNCATE_CONTENT_CHARS)
        self._log.write("\n")
        self._agent_start = None
        self._phase = ""

    def _handle_run_error(self, event: dict[str, Any]) -> None:
        error_type = str(event.get("error_type") or "Error")
        detail = str(event.get("error_detail") or event.get("error") or "")
        self._log.write(f"  [error] {error_type}\n")
        if detail:
            self._write_indented_block("", detail, char_limit=_TRUNCATE_CONTENT_CHARS)
        self._log.write("\n")

    # -- helpers --------------------------------------------------------------

    def _emit_header(self, phase: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        scope = self._scope
        if scope.startswith("worker:"):
            wid = scope.removeprefix("worker:")
            self._log.write(f"─── WORKER {wid}")
        else:
            self._log.write(f"═══ {scope.upper()}")
        if phase:
            self._log.write(f" · {phase}")
        self._log.write(f" · {ts}\n\n")

    def _write_section(self, label: str, content: str, *, char_limit: int) -> None:
        if not content.strip():
            return
        self._log.write(f"  [{label}]\n")
        self._write_indented_block("", content, char_limit=char_limit)
        self._log.write("\n")

    def _write_indented(self, text: str) -> None:
        for line in text.splitlines():
            self._log.write(f"    {line}\n")

    def _write_indented_block(self, label: str, content: str, *, char_limit: int) -> None:
        text = content.strip()
        if len(text) > char_limit:
            text = text[:char_limit] + f"\n... [{len(content) - char_limit} more chars]"
        if label:
            self._log.write(f"    {label}: ")
            first = True
            for line in text.splitlines():
                if first:
                    self._log.write(f"{line}\n")
                    first = False
                else:
                    self._log.write(f"    {line}\n")
        else:
            self._write_indented(text)


_EVENT_HANDLERS = {
    "run_start": EventLogWriter._handle_run_start,
    "model_request": EventLogWriter._handle_model_request,
    "model_retry": EventLogWriter._handle_model_retry,
    "thinking": EventLogWriter._handle_thinking,
    "assistant_message": EventLogWriter._handle_assistant_message,
    "tool_call": EventLogWriter._handle_tool_call,
    "tool_result": EventLogWriter._handle_tool_result,
    "run_finish": EventLogWriter._handle_run_finish,
    "run_error": EventLogWriter._handle_run_error,
}


# -- formatting helpers -------------------------------------------------------

def _format_args(args: Any, raw: Any) -> str:
    """Produce a compact one-line argument preview."""
    source = args if isinstance(args, dict) else None
    if source is None and isinstance(raw, str):
        try:
            source = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return _compact(raw)
    if not source:
        return ""
    return _compact(json.dumps(source, ensure_ascii=False))


def _truncate_result(content: str) -> str:
    """Return a short preview of a tool result."""
    text = " ".join(content.replace("\r", " ").split())
    if len(text) <= _TRUNCATE_RESULT_BYTES:
        return text
    return text[:_TRUNCATE_RESULT_BYTES] + f" ... [{len(content)} total bytes]"


def _compact(text: str) -> str:
    """Collapse whitespace and shorten."""
    if not text:
        return ""
    flat = " ".join(text.replace("\r", " ").split())
    if len(flat) <= 300:
        return flat
    return flat[:297] + "..."
