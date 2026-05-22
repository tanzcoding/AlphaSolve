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

Profile-driven factory for the agent layer (the CLI path)::

    from pathlib import Path
    from alphasolve.llm import load_active_profile, load_presets, make_client_factory

    profile = load_active_profile(name="balanced", repo_path=Path("profiles.yaml"), user_path=None)
    presets = load_presets(repo_path=Path("presets.yaml"), user_path=None)
    client_factory = make_client_factory(profile, presets)
    # client_factory(agent_config) -> ChatClient, dispatched by agent_config.effective_role()
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
from .config.profile import Profile
from .config.loader import load_presets, load_profile, load_active_profile
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
    "Profile",
    "WireFormat",
    "load_presets",
    "load_profile",
    "load_active_profile",
    "make_client",
    "make_client_factory",
]
