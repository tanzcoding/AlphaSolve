from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from openai import OpenAI

from .config import GeneralAgentConfig
from .tool_registry import ToolRegistry


class ChatClient(Protocol):
    def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
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


class OpenAIChatClient:
    def __init__(self, config: Mapping[str, Any]):
        def resolve(value: Any) -> Any:
            return value() if callable(value) else value

        self.model = resolve(config.get("model"))
        self.temperature = resolve(config.get("temperature", 1.0))
        self.timeout = resolve(config.get("timeout", 3600))
        self.params = resolve(config.get("params", {})) or {}
        self.client = OpenAI(
            api_key=resolve(config.get("api_key")),
            base_url=resolve(config.get("base_url")),
            timeout=self.timeout,
        )

    def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            **self.params,
        }
        if tools:
            request["tools"] = tools

        response = self.client.chat.completions.create(**request)
        message = response.choices[0].message
        if hasattr(message, "model_dump"):
            return message.model_dump(exclude_none=True)
        return dict(message)


class GeneralPurposeAgent:
    def __init__(
        self,
        *,
        config: GeneralAgentConfig,
        client: ChatClient,
        tool_registry: ToolRegistry,
        event_sink: AgentEventSink | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.tool_registry = tool_registry
        self.last_trace: list[dict[str, Any]] = []
        self.event_sink = event_sink

    def run(self, task: str, *, extra_messages: list[dict[str, Any]] | None = None) -> AgentRunResult:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.config.system_prompt},
            {"role": "user", "content": task},
        ]
        if extra_messages:
            messages.extend(extra_messages)

        tools = self.tool_registry.openai_tools(self.config.tools)
        final_answer = ""
        trace: list[dict[str, Any]] = [
            {
                "type": "run_start",
                "agent": self.config.name,
                "task": task,
                "enabled_tools": list(self.config.tools),
            }
        ]
        self.last_trace = trace
        self._emit(trace[-1])

        for turn in range(1, self.config.max_turns + 1):
            trace.append({"type": "model_request", "agent": self.config.name, "turn": turn})
            self._emit(trace[-1])
            try:
                assistant_message = self.client.complete(messages=messages, tools=tools)
            except Exception as exc:
                trace.append(
                    {
                        "type": "run_error",
                        "turn": turn,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
                self.last_trace = trace
                self._emit(trace[-1])
                raise AgentRunError(f"agent {self.config.name} failed: {exc}", trace=trace) from exc
            messages.append(assistant_message)
            trace.append(
                {
                    "type": "assistant_message",
                    "turn": turn,
                    "content": assistant_message.get("content") or "",
                    "tool_call_count": len(assistant_message.get("tool_calls") or []),
                    "raw": assistant_message,
                }
            )
            self._emit(trace[-1])

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
                    result = self.tool_registry.execute(name, parsed_args or {})
                    result_content = result.content
                    is_error = result.is_error
                trace.append(
                    {
                        "type": "tool_result",
                        "turn": turn,
                        "tool_call_id": tool_call.get("id"),
                        "name": name,
                        "content": result_content,
                        "is_error": is_error,
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
