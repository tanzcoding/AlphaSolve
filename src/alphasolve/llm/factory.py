from __future__ import annotations

from typing import Callable, Protocol

from .client import CodexClient
from .config.preset import Preset
from .config.tier import TierMapping


class _AgentConfigLike(Protocol):
    """只依赖 tier 选择接口，避免模型配置反向依赖角色实现。"""

    def effective_tier(self) -> str: ...


def make_client(preset: Preset) -> CodexClient:
    """选择模型配置；此处不启动 Codex，也不读取登录凭据。"""
    return CodexClient(preset)


def make_client_factory(
    tier_mapping: TierMapping,
    presets: dict[str, Preset],
) -> Callable[[_AgentConfigLike], CodexClient]:
    def factory(agent_config: _AgentConfigLike) -> CodexClient:
        tier = agent_config.effective_tier()
        preset_name = tier_mapping.preset_for(tier)
        if preset_name not in presets:
            raise KeyError(
                f"tier mapping maps tier {tier!r} to preset {preset_name!r}, "
                f"but no such preset is defined; available presets: {sorted(presets)}"
            )
        return make_client(presets[preset_name])
    return factory
