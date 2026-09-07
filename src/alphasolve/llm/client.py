from __future__ import annotations

from dataclasses import dataclass

from .config.preset import Preset


@dataclass(frozen=True)
class CodexClient:
    """持有角色选定的模型配置；会话和工具执行由 Codex 适配器管理。"""

    preset: Preset

    @property
    def model(self) -> str | None:
        return self.preset.model
