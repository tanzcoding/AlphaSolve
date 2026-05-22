from __future__ import annotations

import inspect
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from alphasolve.llm.types import (
    ChatClient,
    ChatDeltaSink,
    CompletionResponse,
    Message,
    StreamDelta,
    ToolCall,
    ToolDef,
)

from .config import AgentConfig
from .tools import ToolRegistry


__all__ = [
    "Agent",
    "AgentEventSink",
    "AgentRunError",
    "AgentRunResult",
    "ChatClient",
    "ChatDeltaSink",
]


@dataclass(frozen=True)
class AgentRunResult:
    final_answer: str
    messages: list[Message]
    trace: list[dict[str, Any]]
    turns: int


class AgentRunError(RuntimeError):
    def __init__(self, message: str, *, trace: list[dict[str, Any]]):
        super().__init__(message)
        self.trace = trace


AgentEventSink = Callable[[dict[str, Any]], None]


class Agent:
    def __init__(
        self,
        *,
        config: AgentConfig,
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

    def run(
        self,
        task: str,
        *,
        description: str = "",
        extra_messages: list[Message] | None = None,
    ) -> AgentRunResult:
        messages: list[Message] = [Message(role="system", content=self.config.system_prompt)]
        if extra_messages:
            messages.extend(extra_messages)
        messages.append(Message(role="user", content=task))

        tools: list[ToolDef] = self.tool_registry.tool_defs(
            self.config.tools, self.config.tool_parameters, self.config.tool_descriptions,
        )
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
                response = self._complete(messages=messages, tools=tools, delta_sink=delta_sink)
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

            assistant_message = response.message
            # Streaming reasoning/content may have been captured into stream_state but not into the
            # final message (some providers stream deltas but return an empty body). Patch the
            # captured text back so the trace and the persisted message both reflect what the user saw.
            if stream_state["reasoning"] and not assistant_message.reasoning_content:
                assistant_message = _replace_message(assistant_message, reasoning_content=stream_state["reasoning"])
            if stream_state["content"] and not assistant_message.content:
                assistant_message = _replace_message(assistant_message, content=stream_state["content"])

            messages.append(assistant_message)
            reasoning = assistant_message.reasoning_content
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
                    "content": assistant_message.content,
                    "tool_call_count": len(assistant_message.tool_calls),
                    "streamed_content": bool(stream_state["content"]),
                    "raw": _message_to_log(assistant_message),
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

            tool_calls = assistant_message.tool_calls
            if not tool_calls:
                final_answer = assistant_message.content
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
                name = tool_call.name
                parsed_args = tool_call.args
                trace.append(
                    {
                        "type": "tool_call",
                        "turn": turn,
                        "tool_call_id": tool_call.id,
                        "name": name,
                        "arguments": parsed_args,
                    }
                )
                self._emit(trace[-1])

                result = self.tool_registry.execute(
                    name,
                    parsed_args,
                    enabled=list(self.config.tools),
                    tool_parameters=self.config.tool_parameters,
                )
                result_content = result.content
                is_error = result.is_error
                stop_agent = result.stop_agent
                trace.append(
                    {
                        "type": "tool_result",
                        "turn": turn,
                        "tool_call_id": tool_call.id,
                        "name": name,
                        "content": result_content,
                        "is_error": is_error,
                        "stop_agent": stop_agent,
                    }
                )
                self._emit(trace[-1])

                messages.append(
                    Message(
                        role="tool",
                        content=result_content,
                        tool_call_id=tool_call.id,
                        name=name,
                    )
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
        messages: list[Message],
        tools: list[ToolDef],
        delta_sink: ChatDeltaSink | None,
    ) -> CompletionResponse:
        if delta_sink is not None and _client_accepts_delta_sink(self.client):
            return self.client.complete(messages=messages, tools=tools, delta_sink=delta_sink)
        return self.client.complete(messages=messages, tools=tools)

    def _make_delta_sink(self, *, turn: int, state: dict[str, str]) -> ChatDeltaSink:
        started_at = time.time()

        def sink(delta: StreamDelta) -> None:
            if delta.type != "text" or not delta.text:
                return
            state["content"] += delta.text
            self._emit(
                {
                    "type": "assistant_delta",
                    "turn": turn,
                    "content": state["content"],
                    "delta": delta.text,
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


def _replace_message(msg: Message, **changes: Any) -> Message:
    from dataclasses import replace
    return replace(msg, **changes)


def _message_to_log(msg: Message) -> dict[str, Any]:
    from dataclasses import asdict
    return asdict(msg)


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
