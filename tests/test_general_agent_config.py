from __future__ import annotations

from pathlib import Path

import pytest

from alphasolve.agents.general.config import (
    GeneralAgentConfig,
    load_general_agent_config,
)


def test_general_agent_config_has_role_field():
    cfg = GeneralAgentConfig(
        name="verifier_adversarial",
        system_prompt="...",
        role="verifier",
    )
    assert cfg.role == "verifier"
    assert cfg.effective_role() == "verifier"


def test_effective_role_falls_back_to_name():
    cfg = GeneralAgentConfig(name="curator", system_prompt="...")
    assert cfg.role is None
    assert cfg.effective_role() == "curator"


def test_tools_and_skills_are_tuples():
    cfg = GeneralAgentConfig(
        name="x",
        system_prompt="...",
        tools=("Read", "Write"),
        skills=("latex-escaping",),
    )
    assert isinstance(cfg.tools, tuple)
    assert isinstance(cfg.skills, tuple)


def test_loading_yaml_with_model_config_raises(tmp_path):
    yaml_path = tmp_path / "broken.yaml"
    yaml_path.write_text(
        "version: 1\n"
        "agent:\n"
        "  name: x\n"
        "  system_prompt: hi\n"
        "  model_config: VERIFIER_CONFIG\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as exc:
        load_general_agent_config(yaml_path)
    msg = str(exc.value)
    assert "model_config" in msg
    assert "role" in msg


def test_loading_yaml_with_role_works(tmp_path):
    yaml_path = tmp_path / "ok.yaml"
    yaml_path.write_text(
        "version: 1\n"
        "agent:\n"
        "  name: verifier_adversarial\n"
        "  system_prompt: stand in for verifier\n"
        "  role: verifier\n"
        "  max_turns: 60\n",
        encoding="utf-8",
    )
    cfg = load_general_agent_config(yaml_path)
    assert cfg.name == "verifier_adversarial"
    assert cfg.role == "verifier"
    assert cfg.effective_role() == "verifier"
    assert cfg.max_turns == 60
