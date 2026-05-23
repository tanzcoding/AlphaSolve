from __future__ import annotations

from typing import Callable, Protocol

from .config.preset import Preset
from .config.tier import TierMapping
from .types import ChatClient


class _AgentConfigLike(Protocol):
    """Minimal shape consumed by the client factory.

    Why: keeps alphasolve.llm decoupled from alphasolve.agent — any object
    exposing ``effective_tier()`` works, including test doubles.
    """

    def effective_tier(self) -> str: ...


def make_client(preset: Preset) -> ChatClient:
    """Construct a ChatClient for the given preset. Dispatched on wire_format."""
    if preset.wire_format == "openai_chat":
        from .providers.openai_chat import OpenAIChatClient
        return OpenAIChatClient(preset)
    if preset.wire_format == "anthropic_messages":
        from .providers.anthropic_messages import AnthropicMessagesClient
        return AnthropicMessagesClient(preset)
    raise ValueError(f"unknown wire_format: {preset.wire_format!r}")


def make_client_factory(
    tier_mapping: TierMapping,
    presets: dict[str, Preset],
) -> Callable[[_AgentConfigLike], ChatClient]:
    def factory(agent_config: _AgentConfigLike) -> ChatClient:
        tier = agent_config.effective_tier()
        preset_name = tier_mapping.preset_for(tier)
        if preset_name not in presets:
            raise KeyError(
                f"tier mapping maps tier {tier!r} to preset {preset_name!r}, "
                f"but no such preset is defined; available presets: {sorted(presets)}"
            )
        return make_client(presets[preset_name])
    return factory
