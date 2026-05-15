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

_RETRYABLE_EXCEPTIONS = (
    openai.InternalServerError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.RateLimitError,
    httpx.RemoteProtocolError,
)


class OpenAIChatClient:
    def __init__(self, config: Mapping[str, Any], *, http_client: httpx.Client | None = None):
        def resolve(value: Any) -> Any:
            return value() if callable(value) else value

        self.model = resolve(config.get("model"))
        self.temperature = resolve(config.get("temperature", 1.0))
        self.timeout = resolve(config.get("timeout", 3600))
        self.params = resolve(config.get("params", {})) or {}
        self.thinking_mode = _config_enables_thinking(self.params)
        self.client = OpenAI(
            api_key=resolve(config.get("api_key")),
            base_url=resolve(config.get("base_url")),
            timeout=self.timeout,
            max_retries=6,
            http_client=http_client,
        )

    _STREAMING_MAX_RETRIES = 3  # after this many streaming failures, fall back to non-streaming

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        delta_sink: ChatDeltaSink | None = None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": _prepare_messages_for_request(messages, thinking_mode=self.thinking_mode),
            "temperature": self.temperature,
            **self.params,
        }
        if tools:
            request["tools"] = tools

        max_retries = 8
        delay = 5.0
        streaming_failures = 0
        use_streaming = delta_sink is not None

        for attempt in range(max_retries + 1):
            try:
                if use_streaming:
                    return self._complete_streaming(request, delta_sink=delta_sink)
                return self._complete_non_streaming(request, delta_sink=delta_sink)
            except _RETRYABLE_EXCEPTIONS as exc:
                if attempt == max_retries:
                    raise
                if use_streaming:
                    streaming_failures += 1
                    if streaming_failures >= self._STREAMING_MAX_RETRIES:
                        use_streaming = False
                        if delta_sink is not None:
                            delta_sink(
                                {
                                    "type": "retry",
                                    "attempt": attempt + 1,
                                    "error_type": type(exc).__name__,
                                    "error": str(exc),
                                    "error_detail": _format_exception_detail(exc),
                                    "fallback": "non_streaming",
                                }
                            )
                        delay = 5.0  # reset backoff for non-streaming
                        continue
                if delta_sink is not None:
                    delta_sink(
                        {
                            "type": "retry",
                            "attempt": attempt + 1,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "error_detail": _format_exception_detail(exc),
                        }
                    )
                time.sleep(delay + random.uniform(0, delay * 0.5))
                delay = min(delay * 2, 300.0)

        raise RuntimeError("unreachable OpenAI retry state")

    def _complete_non_streaming(
        self, request: dict[str, Any], *, delta_sink: ChatDeltaSink | None = None
    ) -> dict[str, Any]:
        response = self.client.chat.completions.create(**request)
        message = _object_to_dict(response.choices[0].message)
        if delta_sink is not None:
            reasoning = str(message.get("reasoning_content") or "")
            if reasoning:
                delta_sink({"type": "reasoning", "content": reasoning})
            content = str(message.get("content") or "")
            if content:
                delta_sink({"type": "content", "content": content})
        return message

    def _complete_streaming(self, request: dict[str, Any], *, delta_sink: ChatDeltaSink) -> dict[str, Any]:
        stream_request = dict(request)
        stream_request["stream"] = True

        role = "assistant"
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_call_parts: dict[int, dict[str, Any]] = {}

        for chunk in self.client.chat.completions.create(**stream_request):
            chunk_dict = _object_to_dict(chunk)
            choices = chunk_dict.get("choices") or []
            if not choices:
                continue
            choice = _object_to_dict(choices[0])
            delta = _object_to_dict(choice.get("delta") or {})
            if not delta:
                continue

            role = str(delta.get("role") or role)
            reasoning_delta = _first_text_delta(delta, _REASONING_KEYS)
            if reasoning_delta:
                reasoning_parts.append(reasoning_delta)
                delta_sink({"type": "reasoning", "content": reasoning_delta})

            content_delta = _first_text_delta(delta, ("content",))
            if content_delta:
                content_parts.append(content_delta)
                delta_sink({"type": "content", "content": content_delta})

            for raw_tool_delta in delta.get("tool_calls") or []:
                tool_delta = _object_to_dict(raw_tool_delta)
                index = int(tool_delta.get("index") or 0)
                current = tool_call_parts.setdefault(
                    index,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if tool_delta.get("id"):
                    current["id"] = str(tool_delta["id"])
                if tool_delta.get("type"):
                    current["type"] = str(tool_delta["type"])

                function_delta = _object_to_dict(tool_delta.get("function") or {})
                function = current.setdefault("function", {"name": "", "arguments": ""})
                if function_delta.get("name"):
                    function["name"] = str(function.get("name") or "") + str(function_delta["name"])
                if function_delta.get("arguments"):
                    function["arguments"] = str(function.get("arguments") or "") + str(function_delta["arguments"])

        message: dict[str, Any] = {"role": role, "content": "".join(content_parts)}
        reasoning = "".join(reasoning_parts)
        if reasoning:
            message["reasoning_content"] = reasoning
        if tool_call_parts:
            message["tool_calls"] = [tool_call_parts[index] for index in sorted(tool_call_parts)]
        return message


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
            {"role": "user", "content": task},
        ]
        if extra_messages:
            messages.extend(extra_messages)

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


def _object_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return _normalize_message_reasoning(dict(value))
    if hasattr(value, "model_dump"):
        result = value.model_dump(exclude_none=True)
        for attr in _REASONING_KEYS:
            if attr in result or not hasattr(value, attr):
                continue
            attr_value = getattr(value, attr)
            if attr_value is not None:
                result[attr] = str(attr_value)
        return _normalize_message_reasoning(result)
    return _normalize_message_reasoning(dict(value))


def _prepare_messages_for_request(
    messages: list[dict[str, Any]],
    *,
    thinking_mode: bool,
) -> list[dict[str, Any]]:
    return [_normalize_message_for_request(message, thinking_mode=thinking_mode) for message in messages]


def _normalize_message_for_request(message: dict[str, Any], *, thinking_mode: bool) -> dict[str, Any]:
    normalized = _normalize_message_reasoning(message)
    if (
        thinking_mode
        and normalized.get("role") == "assistant"
        and normalized.get("tool_calls")
        and "reasoning_content" not in normalized
    ):
        # thinking 模式下，部分兼容接口会要求工具调用前的 assistant 消息显式带回该字段。
        normalized["reasoning_content"] = ""
    return normalized


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


def _config_enables_thinking(params: Mapping[str, Any]) -> bool:
    extra_body = params.get("extra_body") if isinstance(params, Mapping) else None
    if not isinstance(extra_body, Mapping):
        return False
    if extra_body.get("enable_thinking") is True:
        return True
    reasoning = extra_body.get("reasoning")
    if isinstance(reasoning, Mapping) and reasoning:
        return True
    thinking = extra_body.get("thinking")
    if isinstance(thinking, Mapping):
        thinking_type = str(thinking.get("type") or "").lower()
        return thinking_type in {"enabled", "enable", "on", "true"} or bool(thinking.get("enabled"))
    return False


def _first_text_delta(delta: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = delta.get(key)
        if value:
            return str(value)
    return ""


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
