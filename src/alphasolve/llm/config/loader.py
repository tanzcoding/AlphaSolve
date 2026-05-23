from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from .preset import Preset
from .tier import TierMapping

_VALID_WIRE_FORMATS = {"openai_chat", "anthropic_messages"}
_REQUIRED_PRESET_FIELDS = ("wire_format", "base_url", "api_key_env", "model")


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
    missing = [field for field in _REQUIRED_PRESET_FIELDS if field not in raw]
    if missing:
        raise ValueError(f"preset {name!r}: missing required fields: {missing}")
    wire = raw["wire_format"]
    if wire not in _VALID_WIRE_FORMATS:
        raise ValueError(
            f"preset {name!r}: unknown wire_format {wire!r}; "
            f"expected one of {sorted(_VALID_WIRE_FORMATS)}"
        )
    return Preset(
        name=name,
        wire_format=wire,
        base_url=str(raw["base_url"]),
        api_key_env=str(raw["api_key_env"]),
        model=str(raw["model"]),
        timeout=float(raw.get("timeout", 3600)),
        params=dict(raw.get("params") or {}),
    )


def load_presets(*, repo_path: Path, user_path: Path | None) -> dict[str, Preset]:
    repo_raw = _read_yaml(repo_path)
    user_raw = _read_yaml(user_path) if user_path is not None else {}

    merged: dict[str, Mapping[str, Any]] = {}
    merged.update(repo_raw)
    merged.update(user_raw)  # whole-record replacement

    return {name: _build_preset(name, raw) for name, raw in merged.items()}


def _load_tiers_raw(repo_path: Path, user_path: Path | None) -> tuple[dict[str, str], str | None]:
    repo_raw = _read_yaml(repo_path)
    user_raw = _read_yaml(user_path) if user_path is not None else {}

    default_name = user_raw.pop("default", None) or repo_raw.pop("default", None)

    merged: dict[str, str] = {}
    for k, v in repo_raw.items():
        if not isinstance(k, str):
            continue
        merged[k] = str(v)
    for k, v in user_raw.items():
        if not isinstance(k, str):
            continue
        merged[k] = str(v)
    return merged, default_name


def load_tier_mapping(*, repo_path: Path, user_path: Path | None) -> TierMapping:
    merged, default_name = _load_tiers_raw(repo_path, user_path)
    return TierMapping(name="default", tier_to_preset=merged)


def _user_config_dir() -> Path:
    override = os.getenv("ALPHASOLVE_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".alphasolve"
