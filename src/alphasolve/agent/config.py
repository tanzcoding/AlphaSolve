from __future__ import annotations

import json
from copy import deepcopy
from string import Template
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml


@dataclass(frozen=True)
class AgentConfig:
    name: str
    system_prompt: str
    role: str | None = None
    tools: tuple[str, ...] = ()
    tool_parameters: dict[str, dict[str, Any]] = field(default_factory=dict)
    tool_descriptions: dict[str, dict[str, Any]] = field(default_factory=dict)
    max_turns: int = 80
    skills: tuple[str, ...] = ()
    when_to_use: str = ""
    system_prompt_template: str = ""
    system_prompt_args: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def effective_role(self) -> str:
        return self.role or self.name


@dataclass(frozen=True)
class AgentSuite:
    path: Path
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    subagents: dict[str, AgentConfig] = field(default_factory=dict)
    models: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)


def load_agent_config(path: str | Path, *, base_dir: str | Path | None = None) -> AgentConfig:
    config_path = Path(path)
    if not config_path.is_absolute() and base_dir is not None:
        config_path = Path(base_dir) / config_path
    config_path = config_path.resolve()

    raw = _load_raw_config(config_path)

    if not isinstance(raw, Mapping):
        raise ValueError("agent config must be a JSON object")

    return _build_agent_config(raw, config_path=config_path, fallback_name=config_path.stem)


def load_agent_suite(path: str | Path) -> AgentSuite:
    config_path = Path(path).resolve()
    if config_path.is_dir():
        config_path = config_path / "agents.yaml"
    raw = _load_raw_config(config_path)
    if not isinstance(raw, Mapping):
        raise ValueError("agent suite config must be a mapping")

    agents: dict[str, AgentConfig] = {}
    subagents: dict[str, AgentConfig] = {}
    agents.update(_load_agent_dir(raw.get("agents_dir"), config_path=config_path))
    subagents.update(_load_agent_dir(raw.get("subagents_dir"), config_path=config_path))
    agents.update(_load_agent_group(raw.get("agents") or {}, config_path=config_path))
    subagents.update(_load_agent_group(raw.get("subagents") or {}, config_path=config_path))
    return AgentSuite(
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


def _load_agent_group(raw_group: Any, *, config_path: Path) -> dict[str, AgentConfig]:
    if not isinstance(raw_group, Mapping):
        raise ValueError("agent group must be a mapping")

    out: dict[str, AgentConfig] = {}
    for name, raw_agent in raw_group.items():
        if not isinstance(raw_agent, Mapping):
            raise ValueError(f"agent config for {name!r} must be a mapping")
        if raw_agent.get("path"):
            config = load_agent_config(raw_agent["path"], base_dir=config_path.parent)
        else:
            config = _build_agent_config(raw_agent, config_path=config_path, fallback_name=str(name))
        out[str(name)] = config
    return out


def _load_agent_dir(raw_dir: Any, *, config_path: Path) -> dict[str, AgentConfig]:
    if not raw_dir:
        return {}
    agent_dir = Path(str(raw_dir))
    if not agent_dir.is_absolute():
        agent_dir = config_path.parent / agent_dir
    agent_dir = agent_dir.resolve()
    if not agent_dir.is_dir():
        raise ValueError(f"agent directory does not exist: {agent_dir}")

    out: dict[str, AgentConfig] = {}
    for path in sorted([*agent_dir.glob("*.yaml"), *agent_dir.glob("*.yml")]):
        config = load_agent_config(path)
        if config.name in out:
            raise ValueError(f"duplicate agent name {config.name!r} in {agent_dir}")
        out[config.name] = config
    return out


def _build_agent_config(
    raw: Mapping[str, Any],
    *,
    config_path: Path,
    fallback_name: str,
) -> AgentConfig:
    if "agent" in raw and isinstance(raw.get("agent"), Mapping):
        raw = raw["agent"]
    return _resolve_agent_config(raw, config_path=config_path, fallback_name=fallback_name, seen=frozenset())


def _resolve_agent_config(
    raw: Mapping[str, Any],
    *,
    config_path: Path,
    fallback_name: str,
    seen: frozenset[Path],
) -> AgentConfig:
    if "model_config" in raw:
        raise ValueError(
            f"{config_path}: field 'model_config' is no longer supported; "
            f"replace with 'role: <role-name>' (see "
            f"docs/superpowers/specs/2026-05-22-alphasolve-llm-provider-abstraction-design.md §5.5)"
        )
    base: AgentConfig | None = None
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
    tool_descriptions = deepcopy(base.tool_descriptions if base else {})
    raw_descriptions = raw.get("tool_descriptions") or {}
    _merge_tool_descriptions(tool_descriptions, _parse_tool_descriptions(raw_descriptions))
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
    return AgentConfig(
        name=name,
        system_prompt=prompt_text,
        role=raw.get("role") or (base.role if base else None),
        tools=tuple(tools),
        tool_parameters=tool_parameters,
        tool_descriptions=tool_descriptions,
        max_turns=int(raw.get("max_turns", base.max_turns if base else 80)),
        skills=tuple(skills),
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


def _parse_tool_descriptions(raw: Any) -> dict[str, dict[str, Any]]:
    """解析 YAML 的 tool_descriptions 字段。

    支持三种覆盖语义：
      tool_descriptions:
        Read:
          suffix: "..."        # 追加到工具默认 description 末尾（最常用）
          override: "..."      # 完全替换默认 description（与 suffix 互斥）
          parameters:
            n_lines:
              description: "..."   # 覆盖某个参数自己的 description
    """
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for tool_name, raw_override in raw.items():
        if not isinstance(raw_override, Mapping):
            continue
        entry: dict[str, Any] = {}
        if "suffix" in raw_override and "override" in raw_override:
            raise ValueError(
                f"tool_descriptions.{tool_name}: 'suffix' and 'override' are mutually exclusive"
            )
        if "suffix" in raw_override:
            suffix = str(raw_override["suffix"]).strip()
            if suffix:
                entry["suffix"] = suffix
        if "override" in raw_override:
            override = str(raw_override["override"]).strip()
            if override:
                entry["override"] = override
        if "parameters" in raw_override and isinstance(raw_override["parameters"], Mapping):
            params: dict[str, dict[str, str]] = {}
            for param_name, param_spec in raw_override["parameters"].items():
                if not isinstance(param_spec, Mapping):
                    continue
                if "description" in param_spec:
                    params[str(param_name)] = {"description": str(param_spec["description"])}
            if params:
                entry["parameters"] = params
        if entry:
            out[str(tool_name)] = entry
    return out


def _merge_tool_descriptions(target: dict[str, dict[str, Any]], incoming: dict[str, dict[str, Any]]) -> None:
    """合并 tool_descriptions：base agent 与当前 agent 的覆盖叠加。

    顶层 key（工具名）独立合并；每个工具内部：
    - suffix / override：incoming 覆盖 target；并且当 incoming 设置其中一个时
      会移除 target 上另一个，保持 suffix/override 互斥（与 parser 单文档检查一致）
    - parameters：按参数名递归合并（incoming 参数级覆盖 target 同名参数）
    """
    for tool_name, entry in incoming.items():
        target_entry = target.setdefault(tool_name, {})
        for key, value in entry.items():
            if key == "parameters":
                target_params = target_entry.setdefault("parameters", {})
                for param_name, param_spec in value.items():
                    base_spec = dict(target_params.get(param_name, {}))
                    base_spec.update(param_spec)
                    target_params[param_name] = base_spec
            else:
                if key == "suffix":
                    target_entry.pop("override", None)
                elif key == "override":
                    target_entry.pop("suffix", None)
                target_entry[key] = value
