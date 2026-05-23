"""Public API of the LLM provider abstraction layer.

External callers (agent layer, CLI, workflow) import from this module.
The factory and loaders live underneath; consumers do not reach into
``alphasolve.llm.config.*`` or ``alphasolve.llm.providers.*`` directly.

One-shot completion against a single preset::

    from alphasolve.llm import Message, Preset, make_client

    preset = Preset(name="x", wire_format="openai_chat",
                    base_url="https://api.deepseek.com",
                    api_key_env="DEEPSEEK_API_KEY", model="deepseek-chat")
    client = make_client(preset)
    response = client.complete(
        messages=[Message(role="user", content="Hi.")],
        tools=[],
    )
    print(response.message.content)

Tier-based factory for the agent layer (the CLI path)::

    from pathlib import Path
    from alphasolve.llm import load_tier_mapping, load_presets, make_client_factory

    tier_mapping = load_tier_mapping(repo_path=Path("tiers.yaml"), user_path=None)
    presets = load_presets(repo_path=Path("presets.yaml"), user_path=None)
    client_factory = make_client_factory(tier_mapping, presets)
    # client_factory(agent_config) -> ChatClient, dispatched by agent_config.effective_tier()
"""

from .types import (
    ChatClient,
    ChatDeltaSink,
    ChatCompletionError,
    Message,
    Role,
    ToolCall,
    ToolDef,
    CompletionResponse,
    Usage,
    StreamDelta,
    FinishReason,
)
from .config.preset import Preset, WireFormat
from .config.tier import TierMapping
from .config.loader import load_presets, load_tier_mapping
from .factory import make_client, make_client_factory

__all__ = [
    "ChatClient",
    "ChatDeltaSink",
    "ChatCompletionError",
    "Message",
    "Role",
    "ToolCall",
    "ToolDef",
    "CompletionResponse",
    "Usage",
    "StreamDelta",
    "FinishReason",
    "Preset",
    "TierMapping",
    "WireFormat",
    "load_presets",
    "load_tier_mapping",
    "make_client",
    "make_client_factory",
]
