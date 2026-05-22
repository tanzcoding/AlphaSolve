from __future__ import annotations

from typing import Callable, Protocol

from .config.preset import Preset
from .config.profile import Profile
from .types import ChatClient


class _AgentConfigLike(Protocol):
    """Minimal shape consumed by the client factory.

    Why: keeps alphasolve.llm decoupled from alphasolve.agent — any object
    exposing ``effective_role()`` works, including test doubles.
    """

    def effective_role(self) -> str: ...


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
    profile: Profile,
    presets: dict[str, Preset],
) -> Callable[[_AgentConfigLike], ChatClient]:
    """Returns a ClientFactory: agent_config -> ChatClient via profile role lookup."""
    def factory(agent_config: _AgentConfigLike) -> ChatClient:
        role = agent_config.effective_role()
        preset_name = profile.preset_for(role)
        if preset_name not in presets:
            raise KeyError(
                f"profile {profile.name!r} maps role {role!r} to preset {preset_name!r}, "
                f"but no such preset is defined; available presets: {sorted(presets)}"
            )
        return make_client(presets[preset_name])
    return factory
