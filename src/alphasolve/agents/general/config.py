from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml


@dataclass(frozen=True)
class GeneralAgentConfig:
    name: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    max_turns: int = 80
    model_config: Optional[str] = None
    skills: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentSuiteConfig:
    path: Path
    agents: dict[str, GeneralAgentConfig] = field(default_factory=dict)
    subagents: dict[str, GeneralAgentConfig] = field(default_factory=dict)
    models: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)


def load_general_agent_config(path: str | Path, *, base_dir: str | Path | None = None) -> GeneralAgentConfig:
    config_path = Path(path)
    if not config_path.is_absolute() and base_dir is not None:
        config_path = Path(base_dir) / config_path
    config_path = config_path.resolve()

    raw = _load_raw_config(config_path)

    if not isinstance(raw, Mapping):
        raise ValueError("agent config must be a JSON object")

    return _build_general_agent_config(raw, config_path=config_path, fallback_name=config_path.stem)


def load_agent_suite_config(path: str | Path) -> AgentSuiteConfig:
    config_path = Path(path).resolve()
    raw = _load_raw_config(config_path)
    if not isinstance(raw, Mapping):
        raise ValueError("agent suite config must be a mapping")

    agents = _load_agent_group(raw.get("agents") or {}, config_path=config_path)
    subagents = _load_agent_group(raw.get("subagents") or {}, config_path=config_path)
    return AgentSuiteConfig(
        path=config_path,
        agents=agents,
        subagents=subagents,
        models=dict(raw.get("models") or {}),
        settings=dict(raw.get("settings") or {}),
    )


def _load_raw_config(config_path: Path) -> Any:
    with config_path.open("r", encoding="utf-8") as f:
        if config_path.suffix.lower() in {".yaml", ".yml"}:
            return yaml.safe_load(f) or {}
        return json.load(f)


def _load_agent_group(raw_group: Any, *, config_path: Path) -> dict[str, GeneralAgentConfig]:
    if not isinstance(raw_group, Mapping):
        raise ValueError("agent group must be a mapping")

    out: dict[str, GeneralAgentConfig] = {}
    for name, raw_agent in raw_group.items():
        if not isinstance(raw_agent, Mapping):
            raise ValueError(f"agent config for {name!r} must be a mapping")
        out[str(name)] = _build_general_agent_config(raw_agent, config_path=config_path, fallback_name=str(name))
    return out


def _build_general_agent_config(
    raw: Mapping[str, Any],
    *,
    config_path: Path,
    fallback_name: str,
) -> GeneralAgentConfig:
    prompt_text = str(raw.get("system_prompt") or "")
    prompt_path = raw.get("prompt_path")
    if prompt_path:
        prompt_file = Path(str(prompt_path))
        if not prompt_file.is_absolute():
            prompt_file = config_path.parent / prompt_file
        prompt_text = prompt_file.resolve().read_text(encoding="utf-8")

    if not prompt_text.strip():
        raise ValueError("agent config must provide system_prompt or prompt_path")

    name = str(raw.get("name") or fallback_name)
    return GeneralAgentConfig(
        name=name,
        system_prompt=prompt_text,
        tools=_parse_tools(raw.get("tools")),
        max_turns=int(raw.get("max_turns", 80)),
        model_config=raw.get("model_config") or raw.get("model"),
        skills=list(raw.get("skills") or []),
        metadata=dict(raw.get("metadata") or {}),
    )


def _parse_tools(raw_tools: Any) -> list[str]:
    if raw_tools is None:
        return []
    if isinstance(raw_tools, Mapping):
        return list(raw_tools.get("allow") or [])
    return list(raw_tools or [])
