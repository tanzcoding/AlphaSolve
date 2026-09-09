from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    """一个 Codex 模型配置；省略模型时使用 Codex 的默认模型。"""

    name: str
    provider: str = "chatgpt"
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    reasoning_effort: str | None = None
