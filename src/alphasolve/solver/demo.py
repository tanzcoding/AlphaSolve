from __future__ import annotations

import re

from alphasolve.agent import AgentConfig
from alphasolve.llm.types import (
    ChatDeltaSink,
    CompletionResponse,
    Message,
    ToolCall,
    ToolDef,
)


def _assistant(content: str = "", tool_calls: tuple[ToolCall, ...] = ()) -> CompletionResponse:
    return CompletionResponse(
        message=Message(role="assistant", content=content, tool_calls=tool_calls),
        finish_reason="tool_calls" if tool_calls else "stop",
    )


class DemoChatClient:
    def __init__(self, role: str) -> None:
        self.role = role
        self.calls = 0

    def complete(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDef],
        delta_sink: ChatDeltaSink | None = None,
    ) -> CompletionResponse:
        self.calls += 1
        if self.role == "orchestrator":
            return self._orchestrator()
        if self.role == "generator":
            return self._generator(messages)
        if self.role.startswith("verifier"):
            return _assistant("Verdict: pass\n\nThis demo proposition is accepted.")
        if self.role == "review_verdict_judge":
            return _assistant("pass")
        if self.role == "theorem_checker":
            return _assistant(
                "Solves original problem: yes\n\nThe verified demo proposition states exactly the problem."
            )
        if self.role == "reviser":
            return _assistant("No revision needed in demo mode.")
        return _assistant("Demo subagent result.")

    def _orchestrator(self) -> CompletionResponse:
        if self.calls == 1:
            return _assistant(
                tool_calls=(
                    ToolCall(
                        id="spawn_demo_worker",
                        name="SpawnWorker",
                        args={"hint": "Produce a small self-contained demo proposition."},
                    ),
                )
            )
        if self.calls == 2:
            return _assistant(
                tool_calls=(
                    ToolCall(id="wait_demo_worker", name="TaskOutput", args={}),
                )
            )
        return _assistant("Demo run complete.")

    def _generator(self, messages: list[Message]) -> CompletionResponse:
        if self.calls == 1:
            task = "\n".join(message.content for message in messages)
            matches = re.findall(r"`(unverified_propositions/prop-[^`]+)`", task)
            concrete = [item for item in matches if "*" not in item]
            worker_dir = concrete[-1] if concrete else "unverified_propositions/prop-demo"
            return _assistant(
                tool_calls=(
                    ToolCall(
                        id="write_demo_proposition",
                        name="Write",
                        args={
                            "path": f"{worker_dir}/proposition.md",
                            "content": (
                                "## Statement\n\n"
                                "For every real number x, x = x.\n\n"
                                "## Proof\n\n"
                                "This follows from reflexivity of equality.\n"
                            ),
                        },
                    ),
                )
            )
        return _assistant("Generator wrote the demo proposition.")


def make_demo_client_factory():
    def factory(config: AgentConfig) -> DemoChatClient:
        return DemoChatClient(config.name)

    return factory
