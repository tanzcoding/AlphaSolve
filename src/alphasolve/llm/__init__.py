"""Public API of the LLM provider abstraction layer.

External callers (agent layer, CLI, workflow) import from this module.
The factory and loaders live underneath; consumers do not reach into
`alphasolve.llm.config.*` or `alphasolve.llm.providers.*` directly.
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
