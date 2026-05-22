from __future__ import annotations

import inspect
import json
import random
import socket
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

import httpx
import openai
from openai import OpenAI

from .config import GeneralAgentConfig
from .tool_registry import ToolRegistry


ChatDeltaSink = Callable[[dict[str, Any]], None]
_REASONING_KEYS = ("reasoning_content", "reasoning", "reasoning_text", "thinking")
_MISSING = object()


class ChatClient(Protocol):
    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        delta_sink: ChatDeltaSink | None = None,
    ) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class AgentRunResult:
    final_answer: str
    messages: list[dict[str, Any]]
    trace: list[dict[str, Any]]
    turns: int


class AgentRunError(RuntimeError):
    def __init__(self, message: str, *, trace: list[dict[str, Any]]):
        super().__init__(message)
        self.trace = trace


AgentEventSink = Callable[[dict[str, Any]], None]

class GeneralPurposeAgent:
    def __init__(
        self,
        *,
        config: GeneralAgentConfig,
        client: ChatClient,
        tool_registry: ToolRegistry,
        event_sink: AgentEventSink | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.tool_registry = tool_registry
        self.last_trace: list[dict[str, Any]] = []
        self.event_sink = event_sink
        self.stop_event = stop_event

    def run(self, task: str, *, description: str = "", extra_messages: list[dict[str, Any]] | None = None) -> AgentRunResult:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.config.system_prompt},
        ]
        if extra_messages:
            messages.extend(extra_messages)
        messages.append({"role": "user", "content": task})

        tools = self.tool_registry.openai_tools(self.config.tools, self.config.tool_parameters)
        final_answer = ""
        trace: list[dict[str, Any]] = [
            {
                "type": "run_start",
                "agent": self.config.name,
                "task": task,
                "description": description,
                "enabled_tools": list(self.config.tools),
                "tool_parameters": self.config.tool_parameters,
            }
        ]
        self.last_trace = trace
        self._emit(trace[-1])

        for turn in range(1, self.config.max_turns + 1):
            if self.stop_event is not None and self.stop_event.is_set():
                trace.append(
                    {"type": "run_stopped", "turn": turn, "reason": "stop_event set"}
                )
                self.last_trace = trace
                self._emit(trace[-1])
                return AgentRunResult(
                    final_answer="",
                    messages=messages,
                    trace=trace,
                    turns=turn - 1,
                )
            turn_start = time.time()
            trace.append({"type": "model_request", "agent": self.config.name, "turn": turn})
            self._emit(trace[-1])
            stream_state = {"reasoning": "", "content": ""}
            delta_sink = self._make_delta_sink(turn=turn, state=stream_state) if self.event_sink is not None else None
            try:
                assistant_message = self._complete(messages=messages, tools=tools, delta_sink=delta_sink)
                assistant_message = _normalize_message_reasoning(assistant_message)
            except KeyboardInterrupt:
                trace.append(
                    {"type": "run_stopped", "turn": turn, "reason": "keyboard_interrupt"}
                )
                self.last_trace = trace
                self._emit(trace[-1])
                return AgentRunResult(
                    final_answer="",
                    messages=messages,
                    trace=trace,
                    turns=turn - 1,
                )
            except Exception as exc:
                trace.append(
                    {
                        "type": "run_error",
                        "turn": turn,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "error_detail": _format_exception_detail(exc),
                    }
                )
                self.last_trace = trace
                self._emit(trace[-1])
                raise AgentRunError(f"agent {self.config.name} failed: {exc}", trace=trace) from exc
            turn_elapsed = time.time() - turn_start
            if stream_state["reasoning"] and not assistant_message.get("reasoning_content"):
                assistant_message = dict(assistant_message)
                assistant_message["reasoning_content"] = stream_state["reasoning"]
            if stream_state["content"] and not assistant_message.get("content"):
                assistant_message = dict(assistant_message)
                assistant_message["content"] = stream_state["content"]

            messages.append(assistant_message)
            reasoning = assistant_message.get("reasoning_content") or ""
            if reasoning:
                trace.append(
                    {
                        "type": "thinking",
                        "turn": turn,
                        "content": reasoning,
                        "streamed": bool(stream_state["reasoning"]),
                        "elapsed": turn_elapsed,
                    }
                )
                self._emit(trace[-1])
            trace.append(
                {
                    "type": "assistant_message",
                    "turn": turn,
                    "content": assistant_message.get("content") or "",
                    "tool_call_count": len(assistant_message.get("tool_calls") or []),
                    "streamed_content": bool(stream_state["content"]),
                    "raw": assistant_message,
                }
            )
            self._emit(trace[-1])

            if self.stop_event is not None and self.stop_event.is_set():
                trace.append(
                    {"type": "run_stopped", "turn": turn, "reason": "stop_event set after model response"}
                )
                self.last_trace = trace
                self._emit(trace[-1])
                return AgentRunResult(
                    final_answer="",
                    messages=messages,
                    trace=trace,
                    turns=turn,
                )
            tool_calls = assistant_message.get("tool_calls") or []
            if not tool_calls:
                final_answer = str(assistant_message.get("content") or "")
                trace.append(
                    {
                        "type": "run_finish",
                        "turn": turn,
                        "final_answer": final_answer,
                    }
                )
                self._emit(trace[-1])
                return AgentRunResult(final_answer=final_answer, messages=messages, trace=trace, turns=turn)

            for tool_call in tool_calls:
                if self.stop_event is not None and self.stop_event.is_set():
                    trace.append(
                        {"type": "run_stopped", "turn": turn, "reason": "stop_event set before tool execution"}
                    )
                    self.last_trace = trace
                    self._emit(trace[-1])
                    return AgentRunResult(
                        final_answer="",
                        messages=messages,
                        trace=trace,
                        turns=turn,
                    )
                function = tool_call.get("function") or {}
                name = str(function.get("name") or "")
                raw_args = function.get("arguments") or "{}"
                parsed_args: dict[str, Any] | None = None
                result_content = ""
                is_error = False
                should_execute = False
                try:
                    args = json.loads(raw_args)
                    if not isinstance(args, dict):
                        raise ValueError("tool arguments must be a JSON object")
                    parsed_args = args
                    should_execute = True
                except Exception as exc:
                    result_content = json.dumps({"error": f"invalid tool arguments: {exc}"}, ensure_ascii=False)
                    is_error = True

                trace.append(
                    {
                        "type": "tool_call",
                        "turn": turn,
                        "tool_call_id": tool_call.get("id"),
                        "name": name,
                        "arguments": parsed_args,
                        "raw_arguments": raw_args,
                    }
                )
                self._emit(trace[-1])
                if should_execute:
                    result = self.tool_registry.execute(
                        name,
                        parsed_args or {},
                        enabled=self.config.tools,
                        tool_parameters=self.config.tool_parameters,
                    )
                    result_content = result.content
                    is_error = result.is_error
                    stop_agent = result.stop_agent
                else:
                    stop_agent = False
                trace.append(
                    {
                        "type": "tool_result",
                        "turn": turn,
                        "tool_call_id": tool_call.get("id"),
                        "name": name,
                        "content": result_content,
                        "is_error": is_error,
                        "stop_agent": stop_agent,
                    }
                )
                self._emit(trace[-1])

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.get("id"),
                        "name": name,
                        "content": result_content,
                    }
                )
                if stop_agent:
                    final_answer = result.stop_answer or result_content
                    trace.append(
                        {
                            "type": "run_finish",
                            "turn": turn,
                            "final_answer": final_answer,
                            "reason": "tool_requested_stop",
                        }
                    )
                    self._emit(trace[-1])
                    return AgentRunResult(final_answer=final_answer, messages=messages, trace=trace, turns=turn)

        trace.append(
            {
                "type": "run_error",
                "turn": self.config.max_turns,
                "error_type": "MaxTurnsExceeded",
                "error": f"agent exceeded max_turns={self.config.max_turns}",
            }
        )
        self.last_trace = trace
        self._emit(trace[-1])
        raise AgentRunError(f"agent exceeded max_turns={self.config.max_turns}", trace=trace)

    def _emit(self, event: dict[str, Any]) -> None:
        if self.event_sink is None:
            return
        try:
            self.event_sink(event)
        except Exception:
            pass

    def _complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        delta_sink: ChatDeltaSink | None,
    ) -> dict[str, Any]:
        if delta_sink is not None and _client_accepts_delta_sink(self.client):
            return self.client.complete(messages=messages, tools=tools, delta_sink=delta_sink)
        return self.client.complete(messages=messages, tools=tools)

    def _make_delta_sink(self, *, turn: int, state: dict[str, str]) -> ChatDeltaSink:
        started_at = time.time()

        def sink(delta: dict[str, Any]) -> None:
            delta_type = str(delta.get("type") or "")
            fragment = str(delta.get("content") or "")
            if delta_type == "retry":
                reasoning_chars = len(state["reasoning"])
                content_chars = len(state["content"])
                state["reasoning"] = ""
                state["content"] = ""
                self._emit(
                    {
                        "type": "model_retry",
                        "turn": turn,
                        "attempt": int(delta.get("attempt") or 0),
                        "error_type": str(delta.get("error_type") or "Error"),
                        "error": str(delta.get("error") or ""),
                        "error_detail": str(delta.get("error_detail") or ""),
                        "reasoning_chars": reasoning_chars,
                        "content_chars": content_chars,
                        "elapsed": time.time() - started_at,
                    }
                )
                return

            if not fragment:
                return

            if delta_type == "reasoning":
                state["reasoning"] += fragment
                self._emit(
                    {
                        "type": "thinking_delta",
                        "turn": turn,
                        "content": state["reasoning"],
                        "delta": fragment,
                        "elapsed": time.time() - started_at,
                    }
                )
            elif delta_type == "content":
                state["content"] += fragment
                self._emit(
                    {
                        "type": "assistant_delta",
                        "turn": turn,
                        "content": state["content"],
                        "delta": fragment,
                        "elapsed": time.time() - started_at,
                    }
                )

        return sink


def _client_accepts_delta_sink(client: ChatClient) -> bool:
    try:
        signature = inspect.signature(client.complete)
    except (TypeError, ValueError):
        return False
    return "delta_sink" in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _normalize_message_reasoning(message: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(message)
    reasoning = _first_present_reasoning_value(normalized)
    if reasoning is not _MISSING and "reasoning_content" not in normalized:
        normalized["reasoning_content"] = "" if reasoning is None else str(reasoning)
    return normalized


def _first_present_reasoning_value(message: dict[str, Any]) -> Any:
    for key in _REASONING_KEYS:
        if key in message:
            return message[key]
    return _MISSING


def _format_exception_detail(exc: BaseException) -> str:
    lines = [item.strip() for item in traceback.format_exception_only(type(exc), exc) if item.strip()]
    detail = " ".join(lines)
    cause = exc.__cause__ or exc.__context__
    if cause is not None:
        cause_lines = [item.strip() for item in traceback.format_exception_only(type(cause), cause) if item.strip()]
        cause_detail = " ".join(cause_lines)
        if cause_detail and cause_detail not in detail:
            detail = f"{detail} | caused by {cause_detail}" if detail else cause_detail
    return detail
