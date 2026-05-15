from __future__ import annotations

import json
from copy import deepcopy
from string import Template
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml


@dataclass(frozen=True)
class GeneralAgentConfig:
    name: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    tool_parameters: dict[str, dict[str, Any]] = field(default_factory=dict)
    max_turns: int = 80
    model_config: str | None = None
    skills: list[str] = field(default_factory=list)
    when_to_use: str = ""
    system_prompt_template: str = ""
    system_prompt_args: dict[str, Any] = field(default_factory=dict)
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
    if config_path.is_dir():
        config_path = config_path / "agents.yaml"
    raw = _load_raw_config(config_path)
    if not isinstance(raw, Mapping):
        raise ValueError("agent suite config must be a mapping")

    agents: dict[str, GeneralAgentConfig] = {}
    subagents: dict[str, GeneralAgentConfig] = {}
    agents.update(_load_agent_dir(raw.get("agents_dir"), config_path=config_path))
    subagents.update(_load_agent_dir(raw.get("subagents_dir"), config_path=config_path))
    agents.update(_load_agent_group(raw.get("agents") or {}, config_path=config_path))
    subagents.update(_load_agent_group(raw.get("subagents") or {}, config_path=config_path))
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
        if raw_agent.get("path"):
            config = load_general_agent_config(raw_agent["path"], base_dir=config_path.parent)
        else:
            config = _build_general_agent_config(raw_agent, config_path=config_path, fallback_name=str(name))
        out[str(name)] = config
    return out


def _load_agent_dir(raw_dir: Any, *, config_path: Path) -> dict[str, GeneralAgentConfig]:
    if not raw_dir:
        return {}
    agent_dir = Path(str(raw_dir))
    if not agent_dir.is_absolute():
        agent_dir = config_path.parent / agent_dir
    agent_dir = agent_dir.resolve()
    if not agent_dir.is_dir():
        raise ValueError(f"agent directory does not exist: {agent_dir}")

    out: dict[str, GeneralAgentConfig] = {}
    for path in sorted([*agent_dir.glob("*.yaml"), *agent_dir.glob("*.yml")]):
        config = load_general_agent_config(path)
        if config.name in out:
            raise ValueError(f"duplicate agent name {config.name!r} in {agent_dir}")
        out[config.name] = config
    return out


def _build_general_agent_config(
    raw: Mapping[str, Any],
    *,
    config_path: Path,
    fallback_name: str,
) -> GeneralAgentConfig:
    if "agent" in raw and isinstance(raw.get("agent"), Mapping):
        raw = raw["agent"]
    return _resolve_agent_config(raw, config_path=config_path, fallback_name=fallback_name, seen=frozenset())


def _resolve_agent_config(
    raw: Mapping[str, Any],
    *,
    config_path: Path,
    fallback_name: str,
    seen: frozenset[Path],
) -> GeneralAgentConfig:
    base: GeneralAgentConfig | None = None
    extend = raw.get("extend")
    if extend:
        base_path = Path(str(extend))
        if not base_path.is_absolute():
            base_path = config_path.parent / base_path
        base_path = base_path.resolve()
        if base_path in seen:
            raise ValueError(f"cyclic agent extend detected: {base_path}")
        base_raw = _load_raw_config(base_path)
        if "agent" in base_raw and isinstance(base_raw.get("agent"), Mapping):
            base_raw = base_raw["agent"]
        if not isinstance(base_raw, Mapping):
            raise ValueError(f"extended agent config must be a mapping: {base_path}")
        base = _resolve_agent_config(
            base_raw,
            config_path=base_path,
            fallback_name=base_path.stem,
            seen=seen | {config_path.resolve()},
        )

    prompt_text = str(raw.get("system_prompt") or "")
    prompt_path = raw.get("prompt_path") or raw.get("system_prompt_path")
    if prompt_path:
        prompt_file = Path(str(prompt_path))
        if not prompt_file.is_absolute():
            prompt_file = config_path.parent / prompt_file
        prompt_text = prompt_file.resolve().read_text(encoding="utf-8")
    elif base is not None:
        prompt_text = base.system_prompt_template or base.system_prompt
    prompt_template = prompt_text

    prompt_args = dict(base.system_prompt_args if base else {})
    prompt_args.update(dict(raw.get("system_prompt_args") or raw.get("prompt_args") or {}))
    if prompt_args:
        prompt_text = Template(prompt_text).safe_substitute(prompt_args)

    if not prompt_text.strip():
        raise ValueError("agent config must provide system_prompt or prompt_path")

    raw_tools_provided = "tools" in raw
    tools = _parse_tools(raw.get("tools")) if raw_tools_provided else list(base.tools if base else [])
    tool_parameters = deepcopy(base.tool_parameters if base else {})
    _merge_tool_parameters(tool_parameters, _parse_tool_parameters(raw.get("tools")))
    for key in ("tool_parameters", "tool_args", "tool_argument_constraints"):
        _merge_tool_parameters(tool_parameters, _parse_tool_parameters(raw.get(key)))
    allowed_tools = raw.get("allowed_tools")
    if allowed_tools is not None:
        allowed = set(str(item) for item in allowed_tools)
        tools = [item for item in tools if item in allowed]
    exclude_tools = raw.get("exclude_tools") or []
    if exclude_tools:
        excluded = set(str(item) for item in exclude_tools)
        tools = [item for item in tools if item not in excluded]

    metadata = dict(base.metadata if base else {})
    metadata.update(dict(raw.get("metadata") or {}))

    name = str(raw.get("name") or (base.name if base else fallback_name))
    skills = list(raw.get("skills", base.skills if base else []))
    skill_blocks = _load_skill_blocks(skills, config_path=config_path)
    if skill_blocks:
        prompt_text = _append_skill_blocks(prompt_text, skill_blocks)
    return GeneralAgentConfig(
        name=name,
        system_prompt=prompt_text,
        tools=tools,
        tool_parameters=tool_parameters,
        max_turns=int(raw.get("max_turns", base.max_turns if base else 80)),
        model_config=raw.get("model_config") or raw.get("model") or (base.model_config if base else None),
        skills=skills,
        when_to_use=str(raw.get("when_to_use") or (base.when_to_use if base else "")),
        system_prompt_template=prompt_template,
        system_prompt_args=prompt_args,
        metadata=metadata,
    )


def _load_skill_blocks(skills: list[Any], *, config_path: Path) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    for raw_skill in skills:
        name = str(raw_skill).strip()
        if not name:
            continue
        skill_file = _resolve_skill_file(name, config_path=config_path)
        if skill_file is None:
            continue
        blocks.append((name, skill_file.read_text(encoding="utf-8")))
    return blocks


def _resolve_skill_file(skill: str, *, config_path: Path) -> Path | None:
    raw = Path(skill)
    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.extend([
            config_path.parent / raw,
            config_path.parent / "skills" / raw,
            config_path.parent / "skills" / f"{skill}.md",
        ])

    for candidate in candidates:
        if candidate.is_dir():
            skill_md = candidate / "SKILL.md"
            if skill_md.is_file():
                return skill_md.resolve()
        if candidate.is_file():
            return candidate.resolve()
    return None


def _append_skill_blocks(prompt_text: str, skill_blocks: list[tuple[str, str]]) -> str:
    parts = [
        prompt_text.rstrip(),
        "",
        "# Skills",
        "",
        "The following skill instructions are attached to this agent. Follow them when they are relevant.",
        "",
    ]
    for name, content in skill_blocks:
        parts.extend([
            f"## {name}",
            "",
            content.strip(),
            "",
        ])
    return "\n".join(parts).rstrip() + "\n"


def _parse_tools(raw_tools: Any) -> list[str]:
    if raw_tools is None:
        return []
    if isinstance(raw_tools, Mapping):
        return list(raw_tools.get("allow") or [])
    return list(raw_tools or [])


def _parse_tool_parameters(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return {}
    if "parameters" in raw and isinstance(raw.get("parameters"), Mapping):
        raw = raw["parameters"]
    elif "args" in raw and isinstance(raw.get("args"), Mapping):
        raw = raw["args"]

    parsed: dict[str, dict[str, Any]] = {}
    for tool_name, raw_params in raw.items():
        if not isinstance(raw_params, Mapping):
            continue
        params: dict[str, Any] = {}
        for param_name, raw_constraint in raw_params.items():
            if isinstance(raw_constraint, Mapping):
                params[str(param_name)] = dict(raw_constraint)
            elif isinstance(raw_constraint, list):
                params[str(param_name)] = {"enum": list(raw_constraint)}
        if params:
            parsed[str(tool_name)] = params
    return parsed


def _merge_tool_parameters(target: dict[str, dict[str, Any]], incoming: dict[str, dict[str, Any]]) -> None:
    for tool_name, params in incoming.items():
        target_params = target.setdefault(tool_name, {})
        for param_name, constraint in params.items():
            base_constraint = dict(target_params.get(param_name) or {})
            base_constraint.update(dict(constraint))
            target_params[param_name] = base_constraint
