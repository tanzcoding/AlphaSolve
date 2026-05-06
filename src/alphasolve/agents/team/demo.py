from __future__ import annotations

import json
import re
from typing import Any

from alphasolve.agents.general import GeneralAgentConfig


class DemoChatClient:
    def __init__(self, role: str) -> None:
        self.role = role
        self.calls = 0

    def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls += 1
        if self.role == "orchestrator":
            return self._orchestrator()
        if self.role == "generator":
            return self._generator(messages)
        if self.role.startswith("verifier"):
            return {
                "role": "assistant",
                "content": "Verdict: pass\n\nThis demo proposition is accepted.",
            }
        if self.role == "review_verdict_judge":
            return {
                "role": "assistant",
                "content": "pass",
            }
        if self.role == "theorem_checker":
            return {
                "role": "assistant",
                "content": "Solves original problem: yes\n\nThe verified demo proposition states exactly the problem.",
            }
        if self.role == "reviser":
            return {"role": "assistant", "content": "No revision needed in demo mode."}
        return {"role": "assistant", "content": "Demo subagent result."}

    def _orchestrator(self) -> dict[str, Any]:
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "spawn_demo_worker",
                        "type": "function",
                        "function": {
                            "name": "SpawnWorker",
                            "arguments": json.dumps({"hint": "Produce a small self-contained demo proposition."}),
                        },
                    }
                ],
            }
        if self.calls == 2:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "wait_demo_worker",
                        "type": "function",
                        "function": {
                            "name": "TaskOutput",
                            "arguments": json.dumps({"seconds": 5}),
                        },
                    }
                ],
            }
        return {"role": "assistant", "content": "Demo run complete."}

    def _generator(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        if self.calls == 1:
            task = "\n".join(str(message.get("content") or "") for message in messages)
            matches = re.findall(r"`(unverified_propositions/prop-[^`]+)`", task)
            concrete = [item for item in matches if "*" not in item]
            worker_dir = concrete[-1] if concrete else "unverified_propositions/prop-demo"
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "write_demo_proposition",
                        "type": "function",
                        "function": {
                            "name": "Write",
                            "arguments": json.dumps(
                                {
                                    "path": f"{worker_dir}/proposition.md",
                                    "content": (
                                        "# Demo Proposition\n\n"
                                        "## Statement\n\n"
                                        "For every real number x, x = x.\n\n"
                                        "## Proof\n\n"
                                        "This follows from reflexivity of equality.\n"
                                    ),
                                }
                            ),
                        },
                    }
                ],
            }
        return {"role": "assistant", "content": "Generator wrote the demo proposition."}


def make_demo_client_factory():
    def factory(config: GeneralAgentConfig) -> DemoChatClient:
        return DemoChatClient(config.name)

    return factory
