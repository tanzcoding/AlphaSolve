from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal

WireFormat = Literal["openai_chat", "anthropic_messages"]


@dataclass(frozen=True)
class Preset:
    name: str
    wire_format: WireFormat
    base_url: str
    api_key_env: str
    model: str
    timeout: float = 3600
    params: dict[str, Any] = field(default_factory=dict)

    def resolve_api_key(self) -> str:
        value = os.getenv(self.api_key_env)
        if not value:
            raise RuntimeError(
                f"env var {self.api_key_env!r} not set "
                f"(required by preset {self.name!r})"
            )
        return value
