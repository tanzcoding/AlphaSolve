from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from alphasolve.llm.config.loader import load_profile, load_active_profile


def _write(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def test_load_named_profile(tmp_path):
    repo = tmp_path / "profiles.yaml"
    _write(repo, """
        balanced:
          orchestrator: deepseek-pro
          verifier: deepseek-pro
        cheap:
          orchestrator: deepseek-flash
          verifier: deepseek-flash
        default: balanced
    """)
    p = load_profile("cheap", repo_path=repo, user_path=None)
    assert p.name == "cheap"
    assert p.preset_for("orchestrator") == "deepseek-flash"


def test_unknown_profile_raises(tmp_path):
    repo = tmp_path / "profiles.yaml"
    _write(repo, """
        balanced:
          orchestrator: deepseek-pro
        default: balanced
    """)
    with pytest.raises(KeyError) as exc:
        load_profile("aggressive", repo_path=repo, user_path=None)
    msg = str(exc.value)
    assert "aggressive" in msg
    assert "balanced" in msg


def test_default_profile_via_active_loader(tmp_path):
    repo = tmp_path / "profiles.yaml"
    _write(repo, """
        balanced:
          orchestrator: deepseek-pro
        cheap:
          orchestrator: deepseek-flash
        default: balanced
    """)
    p = load_active_profile(name=None, repo_path=repo, user_path=None)
    assert p.name == "balanced"


def test_explicit_name_overrides_default(tmp_path):
    repo = tmp_path / "profiles.yaml"
    _write(repo, """
        balanced:
          orchestrator: deepseek-pro
        cheap:
          orchestrator: deepseek-flash
        default: balanced
    """)
    p = load_active_profile(name="cheap", repo_path=repo, user_path=None)
    assert p.name == "cheap"


def test_user_overrides_whole_profile(tmp_path):
    repo = tmp_path / "profiles.yaml"
    user = tmp_path / "user_profiles.yaml"
    _write(repo, """
        strategic:
          orchestrator: qwen-3.7-max
          verifier: deepseek-pro
          generator: deepseek-pro
        default: strategic
    """)
    _write(user, """
        strategic:
          orchestrator: internal-claude
          verifier: deepseek-pro-anthropic
          generator: deepseek-pro
    """)
    p = load_profile("strategic", repo_path=repo, user_path=user)
    assert p.preset_for("orchestrator") == "internal-claude"
    assert p.preset_for("verifier") == "deepseek-pro-anthropic"


def test_user_adds_new_profile(tmp_path):
    repo = tmp_path / "profiles.yaml"
    user = tmp_path / "user_profiles.yaml"
    _write(repo, """
        balanced:
          orchestrator: deepseek-pro
        default: balanced
    """)
    _write(user, """
        myteam:
          orchestrator: my-claude
    """)
    p = load_profile("myteam", repo_path=repo, user_path=user)
    assert p.preset_for("orchestrator") == "my-claude"


def test_no_default_and_no_name_raises(tmp_path):
    repo = tmp_path / "profiles.yaml"
    _write(repo, """
        balanced:
          orchestrator: deepseek-pro
        cheap:
          orchestrator: deepseek-flash
    """)
    with pytest.raises(ValueError) as exc:
        load_active_profile(name=None, repo_path=repo, user_path=None)
    assert "default" in str(exc.value)
