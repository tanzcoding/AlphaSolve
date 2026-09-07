from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from alphasolve.solver.ui.team_renderer import PropositionTeamRenderer


AgentEventHandler = Callable[[dict[str, Any]], None]


class _AssistantOutput:
    """按原生项结束正文；修正快照时只重建尚未刷入时间线的正文。"""

    def __init__(self, append: Callable[[str], None], flush: Callable[[], None],
                 reset: Callable[[int], None]) -> None:
        self.append = append
        self.flush = flush
        self.reset = reset
        self.chunks: list[tuple[str | None, str]] = []

    def __call__(self, event: dict[str, Any]) -> bool:
        kind = event.get("type")
        if kind in {"run_start", "run_finish", "model_retry"}:
            self.chunks.clear()
        if kind == "assistant_delta":
            delta = str(event.get("delta") or "")
            if delta:
                self.chunks.append((event.get("item_id"), delta))
                self.append(delta)
            return True
        if kind != "assistant_message":
            return False
        content = str(event.get("content") or "")
        if "item_id" not in event:
            # 旧事件源没有项标识，保持原来的追加与完成约定。
            if content and not event.get("streamed_content"):
                self.append(content)
            if event.get("streamed_content"):
                self.flush()
                self.chunks.clear()
            return True
        item_id = event.get("stream_item_id", event.get("item_id"))
        remaining = [(key, text) for key, text in self.chunks if key != item_id]
        if event.get("replace_content") is not None or remaining:
            # 仅撤回本 sink 记录的未完成正文；其他项随后原样恢复，不清推理或父调用。
            self.reset(sum(len(text) for _, text in self.chunks))
            self.append(content)
            self.flush()
            for _, text in remaining:
                self.append(text)
        else:
            if content and not event.get("streamed_content"):
                self.append(content)
            self.flush()
        self.chunks = remaining
        return True


def make_orchestrator_event_sink(renderer: PropositionTeamRenderer | None) -> AgentEventHandler | None:
    if renderer is None:
        return None
    output = _AssistantOutput(renderer.append_orchestrator_output, renderer.flush_orchestrator_output,
                              lambda chars: renderer.reset_orchestrator_stream(content_chars=chars))

    def sink(event: dict[str, Any]) -> None:
        if output(event):
            return
        event_type = event.get("type")
        if event_type == "subagent_event":
            _show_subagent_event(renderer, event, parent="orchestrator")
        elif event_type == "run_start":
            renderer.update_orchestrator_phase("orchestrator", status="running")
            renderer.log(None, "orchestrator started", module="orchestrator")
        elif event_type == "model_request":
            renderer.update_orchestrator_phase("thinking", status="thinking")
        elif event_type == "model_retry":
            renderer.reset_orchestrator_stream(
                content_chars=int(event.get("content_chars") or 0),
                reasoning_chars=int(event.get("reasoning_chars") or 0),
            )
            renderer.log(None, _event_retry(event), module="orchestrator", level="WARNING")
        elif event_type == "thinking_delta":
            content = str(event.get("content") or "")
            if content:
                renderer.update_orchestrator_thinking(
                    module="orchestrator",
                    thinking_text=content,
                    elapsed=float(event.get("elapsed") or 0),
                )
        elif event_type == "thinking":
            content = str(event.get("content") or "")
            if content:
                if not event.get("streamed"):
                    renderer.update_orchestrator_thinking(module="orchestrator", thinking_text=content, elapsed=0)
                renderer.finish_orchestrator_thinking(module="orchestrator", elapsed=float(event.get("elapsed") or 0), char_count=len(content))
        elif event_type == "tool_call":
            renderer.update_orchestrator_tool_start(
                module="orchestrator",
                name=str(event.get("name") or ""),
                arg_preview=_event_args_preview(event),
            )
        elif event_type == "tool_result":
            is_error = bool(event.get("is_error"))
            name = str(event.get("name") or "")
            renderer.update_orchestrator_tool_done(name=name, is_error=is_error)
            if is_error:
                renderer.log(None, _content_preview(event), module=name, level="ERROR")
        elif event_type == "run_finish":
            renderer.flush_orchestrator_output()
            renderer.update_orchestrator_phase("complete", status="verified")
            final_answer = str(event.get("final_answer") or "")
            if final_answer:
                renderer.log(None, final_answer, module="orchestrator", level="DONE")
        elif event_type == "run_error":
            renderer.update_orchestrator_phase("error", status="failed")
            renderer.log(None, _event_error(event), module="orchestrator", level="ERROR")

    return sink


def make_curator_event_sink(renderer: PropositionTeamRenderer | None) -> AgentEventHandler | None:
    if renderer is None:
        return None
    output = _AssistantOutput(renderer.append_curator_output, renderer.flush_curator_output,
                              lambda chars: renderer.reset_curator_stream(content_chars=chars))

    def sink(event: dict[str, Any]) -> None:
        if output(event):
            return
        event_type = event.get("type")
        if event_type == "subagent_event":
            _show_subagent_event(renderer, event, parent="curator")
        elif event_type == "run_start":
            renderer.update_curator_phase("curator", status="running")
            renderer.log_curator("curator started", module="curator")
        elif event_type == "model_request":
            renderer.update_curator_phase("thinking", status="thinking")
        elif event_type == "model_retry":
            renderer.reset_curator_stream(
                content_chars=int(event.get("content_chars") or 0),
                reasoning_chars=int(event.get("reasoning_chars") or 0),
            )
            renderer.log_curator(_event_retry(event), module="curator", level="WARNING")
        elif event_type == "thinking_delta":
            content = str(event.get("content") or "")
            if content:
                renderer.update_curator_thinking(
                    module="curator",
                    thinking_text=content,
                    elapsed=float(event.get("elapsed") or 0),
                )
        elif event_type == "thinking":
            content = str(event.get("content") or "")
            if content:
                if not event.get("streamed"):
                    renderer.update_curator_thinking(module="curator", thinking_text=content, elapsed=0)
                renderer.finish_curator_thinking(
                    module="curator",
                    elapsed=float(event.get("elapsed") or 0),
                    char_count=len(content),
                )
        elif event_type == "tool_call":
            renderer.update_curator_tool_start(
                module="curator",
                name=str(event.get("name") or ""),
                arg_preview=_event_args_preview(event),
            )
        elif event_type == "tool_result":
            is_error = bool(event.get("is_error"))
            name = str(event.get("name") or "")
            renderer.update_curator_tool_done(name=name, is_error=is_error)
            if is_error:
                renderer.log_curator(_content_preview(event), module=name, level="ERROR")
        elif event_type == "run_finish":
            renderer.flush_curator_output()
            renderer.update_curator_phase("complete", status="complete")
        elif event_type == "run_error":
            renderer.update_curator_phase("error", status="failed")
            renderer.log_curator(_event_error(event), module="curator", level="ERROR")

    return sink


def make_worker_event_sink(
    renderer: PropositionTeamRenderer | None,
    *,
    worker_id: str,
    role: str,
) -> AgentEventHandler | None:
    if renderer is None:
        return None
    output = _AssistantOutput(lambda text: renderer.append_output(worker_id, text),
                              lambda: renderer.flush_output(worker_id),
                              lambda chars: renderer.reset_stream(worker_id, content_chars=chars))

    def sink(event: dict[str, Any]) -> None:
        if output(event):
            return
        event_type = event.get("type")
        if event_type == "subagent_event":
            _show_subagent_event(renderer, event, parent="worker", worker_id=worker_id)
        elif event_type == "run_start":
            renderer.update_phase(worker_id, role, status="running")
            renderer.log(worker_id, f"{role} started", module=role)
        elif event_type == "model_request":
            renderer.update_phase(worker_id, role, status="thinking")
        elif event_type == "model_retry":
            renderer.reset_stream(
                worker_id,
                content_chars=int(event.get("content_chars") or 0),
                reasoning_chars=int(event.get("reasoning_chars") or 0),
                phase=role,
            )
            renderer.log(worker_id, _event_retry(event), module=role, level="WARNING")
        elif event_type == "thinking_delta":
            content = str(event.get("content") or "")
            if content:
                renderer.update_thinking(
                    worker_id,
                    module=role,
                    thinking_text=content,
                    elapsed=float(event.get("elapsed") or 0),
                )
        elif event_type == "thinking":
            content = str(event.get("content") or "")
            if content:
                if not event.get("streamed"):
                    renderer.update_thinking(worker_id, module=role, thinking_text=content, elapsed=0)
                renderer.finish_thinking(worker_id, module=role, elapsed=float(event.get("elapsed") or 0), char_count=len(content))
        elif event_type == "tool_call":
            renderer.update_tool_start(
                worker_id,
                module=role,
                name=str(event.get("name") or ""),
                arg_preview=_event_args_preview(event),
            )
        elif event_type == "tool_result":
            is_error = bool(event.get("is_error"))
            name = str(event.get("name") or "")
            renderer.update_tool_done(worker_id, name=name, is_error=is_error)
            if is_error:
                renderer.log(worker_id, _content_preview(event), module=name, level="ERROR")
        elif event_type == "run_finish":
            renderer.flush_output(worker_id)
            renderer.update_phase(worker_id, f"{role} done", status="running")
        elif event_type == "run_error":
            renderer.update_phase(worker_id, f"{role} error", status="failed")
            renderer.log(worker_id, _event_error(event), module=role, level="ERROR")

    return sink


def _show_subagent_event(
    renderer: PropositionTeamRenderer,
    envelope: dict[str, Any],
    *,
    parent: str,
    worker_id: str | None = None,
) -> None:
    """仅转发可见活动；子调用的完成不能结束父角色的等待。"""
    event = envelope.get("event") or {}
    kind = event.get("type")
    status = "running"
    append = finished = record = False
    if kind == "run_start":
        text = "started " + _shorten(str(event.get("description") or ""), 160)
        record = True
    elif kind == "model_request":
        status, text = "thinking", ""
    elif kind == "thinking_delta":
        status = "thinking"
        text = str(event.get("content") or event.get("delta") or "")
        append = "content" not in event
    elif kind == "assistant_delta":
        status, text, append = "writing", str(event.get("delta") or ""), True
    elif kind in {"thinking", "assistant_message"}:
        status = "thinking" if kind == "thinking" else "writing"
        text = str(event.get("content") or "")
    elif kind == "tool_call":
        status = "tool"
        text = f"Using {event.get('name') or ''}  {_event_args_preview(event)}"
    elif kind == "tool_result":
        status = "failed" if event.get("is_error") else "running"
        marker = "✗" if event.get("is_error") else "✓"
        text = f"{marker} {event.get('name') or ''}"
        if event.get("is_error"):
            text += " " + _content_preview(event)
        record = True
    elif kind in {"run_finish", "run_error", "run_stopped"}:
        status = {"run_finish": "complete", "run_error": "failed", "run_stopped": "cancelled"}[kind]
        text = _event_error(event) if kind == "run_error" else status
        finished = record = True
    else:
        return
    renderer.update_subagent_activity(
        parent=parent,
        worker_id=worker_id,
        session_id=str(envelope.get("session_id") or ""),
        name=str(envelope.get("agent_type") or "subagent"),
        status=status,
        text=text,
        append=append,
        finished=finished,
        record=record,
    )


def _event_args_preview(event: dict[str, Any]) -> str:
    raw = event.get("raw_arguments")
    if raw:
        return _shorten(str(raw), 160)
    args = event.get("arguments")
    if args is None:
        return ""
    try:
        return _shorten(json.dumps(args, ensure_ascii=False), 160)
    except TypeError:
        return _shorten(str(args), 160)


def _content_preview(event: dict[str, Any]) -> str:
    return _shorten(str(event.get("content") or ""), 500)


def _event_error(event: dict[str, Any]) -> str:
    error_type = str(event.get("error_type") or "Error")
    detail = str(event.get("error_detail") or "")
    error = detail or str(event.get("error") or "")
    if error.startswith(error_type + ":"):
        return _shorten(error, 2000)
    return _shorten(f"{error_type}: {error}", 2000)


def _event_retry(event: dict[str, Any]) -> str:
    attempt = int(event.get("attempt") or 0)
    error = _event_error(event)
    prefix = f"retrying model stream after attempt {attempt}"
    return _shorten(f"{prefix}: {error}", 1000)


def _shorten(text: str, limit: int) -> str:
    clean = " ".join(text.replace("\r", " ").split())
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 3)] + "..."
