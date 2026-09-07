from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from .preset import Preset
from .tier import TierMapping

_PRESET_FIELDS = {"provider", "model", "base_url", "api_key_env", "reasoning_effort"}
_LEGACY_FIELDS = {"wire_format", "params", "timeout"}


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, Mapping):
        raise ValueError(f"{path}: top-level YAML must be a mapping")
    return dict(data)


def _build_preset(name: str, raw: Mapping[str, Any]) -> Preset:
    if not isinstance(raw, Mapping):
        raise ValueError(f"preset {name!r}: value must be a mapping")
    legacy = _LEGACY_FIELDS.intersection(raw)
    if legacy:
        raise ValueError(
            f"preset {name!r}: 旧配置字段 {sorted(legacy)} 已移除；"
            "请使用 provider、model、reasoning_effort 配置 Codex，"
            "ChatGPT 订阅使用 provider: chatgpt。"
        )
    unknown = set(raw) - _PRESET_FIELDS
    if unknown:
        raise ValueError(f"preset {name!r}: unknown fields: {sorted(unknown)}")
    values = {}
    for field in _PRESET_FIELDS:
        value = raw.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"preset {name!r}: {field} must be a non-empty string")
        values[field] = value
    provider = values["provider"] or "chatgpt"
    if provider == "chatgpt":
        if values["base_url"] is not None or values["api_key_env"] is not None:
            raise ValueError(f"preset {name!r}: chatgpt 使用 Codex 登录，不接受 base_url 或 api_key_env")
    else:
        missing = [field for field in ("model", "base_url", "api_key_env") if not values[field]]
        if missing:
            raise ValueError(f"preset {name!r}: 自定义 Responses provider 缺少 {missing}")
    return Preset(
        name=name,
        provider=provider,
        model=values["model"],
        base_url=values["base_url"],
        api_key_env=values["api_key_env"],
        reasoning_effort=values["reasoning_effort"],
    )


def load_presets(*, repo_path: Path, user_path: Path | None) -> dict[str, Preset]:
    repo_raw = _read_yaml(repo_path)
    user_raw = _read_yaml(user_path) if user_path is not None else {}

    merged: dict[str, Mapping[str, Any]] = {}
    merged.update(repo_raw)
    merged.update(user_raw)  # 用户配置按整条 preset 覆盖。

    return {name: _build_preset(name, raw) for name, raw in merged.items()}


def _load_tiers_raw(repo_path: Path, user_path: Path | None) -> tuple[dict[str, Any], str | None]:
    repo_raw = _read_yaml(repo_path)
    user_raw = _read_yaml(user_path) if user_path is not None else {}

    default_name = user_raw.pop("default", None) or repo_raw.pop("default", None)

    merged: dict[str, Any] = {}
    merged.update(repo_raw)
    merged.update(user_raw)
    return merged, default_name


def load_tier_mapping(*, repo_path: Path, user_path: Path | None) -> TierMapping:
    merged, default_name = _load_tiers_raw(repo_path, user_path)
    tier_to_preset = {str(k): str(v) for k, v in merged.items()}
    return TierMapping(name="default", tier_to_preset=tier_to_preset)


def _user_config_dir() -> Path:
    override = os.getenv("ALPHASOLVE_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".alphasolve"
