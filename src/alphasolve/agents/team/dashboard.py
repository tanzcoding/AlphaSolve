from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from alphasolve.utils.rich_renderer import PropositionTeamRenderer


AgentEventHandler = Callable[[dict[str, Any]], None]


def make_orchestrator_event_sink(renderer: PropositionTeamRenderer | None) -> AgentEventHandler | None:
    if renderer is None:
        return None

    def sink(event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "run_start":
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
        elif event_type == "assistant_delta":
            delta = str(event.get("delta") or "")
            if delta:
                renderer.append_orchestrator_output(delta)
        elif event_type == "assistant_message":
            content = str(event.get("content") or "")
            if content and not event.get("streamed_content"):
                renderer.append_orchestrator_output(content)
            if event.get("streamed_content"):
                renderer.flush_orchestrator_output()
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


def make_worker_event_sink(
    renderer: PropositionTeamRenderer | None,
    *,
    worker_id: str,
    role: str,
) -> AgentEventHandler | None:
    if renderer is None:
        return None

    def sink(event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "run_start":
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
        elif event_type == "assistant_delta":
            delta = str(event.get("delta") or "")
            if delta:
                renderer.append_output(worker_id, delta)
        elif event_type == "assistant_message":
            content = str(event.get("content") or "")
            if content and not event.get("streamed_content"):
                renderer.append_output(worker_id, content)
            if event.get("streamed_content"):
                renderer.flush_output(worker_id)
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
