from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from alphasolve.llm.config.loader import load_presets
from alphasolve.llm.config.preset import Preset


def _write(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def test_loads_simple_preset(tmp_path):
    repo_yaml = tmp_path / "presets.yaml"
    _write(repo_yaml, """
        deepseek-pro:
          wire_format: openai_chat
          base_url: https://api.deepseek.com
          api_key_env: DEEPSEEK_API_KEY
          model: deepseek-v4-pro
    """)
    presets = load_presets(repo_path=repo_yaml, user_path=None)
    assert "deepseek-pro" in presets
    p = presets["deepseek-pro"]
    assert isinstance(p, Preset)
    assert p.model == "deepseek-v4-pro"
    assert p.timeout == 3600  # default
    assert p.params == {}     # default


def test_loads_preset_with_params(tmp_path):
    repo_yaml = tmp_path / "presets.yaml"
    _write(repo_yaml, """
        kimi:
          wire_format: openai_chat
          base_url: https://api.moonshot.cn/v1
          api_key_env: MOONSHOT_API_KEY
          model: kimi-k2-thinking
          timeout: 1800
          params:
            temperature: 1.0
            extra_body: { reasoning: { effort: max } }
    """)
    presets = load_presets(repo_path=repo_yaml, user_path=None)
    p = presets["kimi"]
    assert p.timeout == 1800
    assert p.params["temperature"] == 1.0
    assert p.params["extra_body"]["reasoning"]["effort"] == "max"


def test_unknown_wire_format_raises(tmp_path):
    repo_yaml = tmp_path / "presets.yaml"
    _write(repo_yaml, """
        bad:
          wire_format: gemini_native
          base_url: https://example.com
          api_key_env: X
          model: y
    """)
    with pytest.raises(ValueError) as exc:
        load_presets(repo_path=repo_yaml, user_path=None)
    assert "gemini_native" in str(exc.value)
    assert "bad" in str(exc.value)


def test_missing_required_field_raises(tmp_path):
    repo_yaml = tmp_path / "presets.yaml"
    _write(repo_yaml, """
        broken:
          wire_format: openai_chat
          base_url: https://example.com
          # missing api_key_env and model
    """)
    with pytest.raises(ValueError) as exc:
        load_presets(repo_path=repo_yaml, user_path=None)
    assert "broken" in str(exc.value)


def test_user_override_replaces_whole_record(tmp_path):
    repo_yaml = tmp_path / "presets.yaml"
    user_yaml = tmp_path / "user_presets.yaml"
    _write(repo_yaml, """
        deepseek-pro:
          wire_format: openai_chat
          base_url: https://api.deepseek.com
          api_key_env: DEEPSEEK_API_KEY
          model: deepseek-v4-pro
          params: { extra_body: { reasoning: { effort: max } } }
    """)
    _write(user_yaml, """
        deepseek-pro:
          wire_format: openai_chat
          base_url: https://my-proxy.internal/deepseek
          api_key_env: MY_PROXY_KEY
          model: deepseek-v4-pro
          timeout: 7200
        # note: params absent → user wins, no merge from repo
    """)
    presets = load_presets(repo_path=repo_yaml, user_path=user_yaml)
    p = presets["deepseek-pro"]
    assert p.base_url == "https://my-proxy.internal/deepseek"
    assert p.api_key_env == "MY_PROXY_KEY"
    assert p.timeout == 7200
    assert p.params == {}, "user-provided preset replaces wholesale, no field merge"


def test_user_adds_new_preset(tmp_path):
    repo_yaml = tmp_path / "presets.yaml"
    user_yaml = tmp_path / "user_presets.yaml"
    _write(repo_yaml, """
        deepseek-pro:
          wire_format: openai_chat
          base_url: https://api.deepseek.com
          api_key_env: DEEPSEEK_API_KEY
          model: deepseek-v4-pro
    """)
    _write(user_yaml, """
        my-claude:
          wire_format: anthropic_messages
          base_url: https://api.anthropic.com
          api_key_env: ANTHROPIC_API_KEY
          model: claude-opus-4
    """)
    presets = load_presets(repo_path=repo_yaml, user_path=user_yaml)
    assert "deepseek-pro" in presets
    assert "my-claude" in presets
    assert presets["my-claude"].wire_format == "anthropic_messages"


def test_user_path_nonexistent_is_ok(tmp_path):
    repo_yaml = tmp_path / "presets.yaml"
    _write(repo_yaml, """
        x:
          wire_format: openai_chat
          base_url: u
          api_key_env: K
          model: m
    """)
    user_yaml = tmp_path / "does_not_exist.yaml"
    presets = load_presets(repo_path=repo_yaml, user_path=user_yaml)
    assert "x" in presets


def test_user_config_dir_resolves_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHASOLVE_CONFIG_DIR", str(tmp_path))
    from alphasolve.llm.config.loader import _user_config_dir
    assert _user_config_dir() == tmp_path


def test_user_config_dir_falls_back_to_home(monkeypatch):
    monkeypatch.delenv("ALPHASOLVE_CONFIG_DIR", raising=False)
    from alphasolve.llm.config.loader import _user_config_dir
    assert _user_config_dir() == Path.home() / ".alphasolve"
