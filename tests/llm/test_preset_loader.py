from __future__ import annotations

from pathlib import Path
import textwrap

import pytest

from alphasolve.llm import load_presets
from alphasolve.llm.config.loader import _user_config_dir


def _write(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def test_loads_subscription_with_codex_default_model(tmp_path):
    path = tmp_path / "presets.yaml"
    _write(path, "subscription: {provider: chatgpt}\n")
    preset = load_presets(repo_path=path, user_path=None)["subscription"]
    assert preset.provider == "chatgpt"
    assert preset.model is None


def test_loads_custom_responses_provider_without_reading_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_PROVIDER_KEY", raising=False)
    path = tmp_path / "presets.yaml"
    _write(path, """
        research:
          provider: deepseek
          model: deepseek-v4-pro
          base_url: https://api.deepseek.com
          api_key_env: TEST_PROVIDER_KEY
          reasoning_effort: high
    """)
    preset = load_presets(repo_path=path, user_path=None)["research"]
    assert preset.provider == "deepseek"
    assert preset.reasoning_effort == "high"
    assert preset.api_key_env == "TEST_PROVIDER_KEY"


@pytest.mark.parametrize("field", ["wire_format", "params", "timeout"])
def test_old_provider_config_reports_migration_instead_of_silent_fallback(tmp_path, field):
    path = tmp_path / "presets.yaml"
    _write(path, f"old: {{model: example, {field}: old-value}}\n")
    with pytest.raises(ValueError, match="旧配置字段") as exc:
        load_presets(repo_path=path, user_path=None)
    assert field in str(exc.value)
    assert "provider: chatgpt" in str(exc.value)


def test_subscription_rejects_api_authentication_fields(tmp_path):
    path = tmp_path / "presets.yaml"
    _write(path, "subscription: {provider: chatgpt, api_key_env: OPENAI_API_KEY}\n")
    with pytest.raises(ValueError, match="Codex 登录"):
        load_presets(repo_path=path, user_path=None)


@pytest.mark.parametrize("missing", ["model", "base_url", "api_key_env"])
def test_custom_provider_requires_explicit_endpoint_model_and_key_variable(tmp_path, missing):
    fields = {
        "provider": "custom", "model": "example-model", "base_url": "https://example.com/v1",
        "api_key_env": "EXAMPLE_KEY",
    }
    fields.pop(missing)
    path = tmp_path / "presets.yaml"
    _write(path, "custom:\n" + "".join(f"  {key}: {value}\n" for key, value in fields.items()))
    with pytest.raises(ValueError, match=missing):
        load_presets(repo_path=path, user_path=None)


@pytest.mark.parametrize("raw", ["reasoning_efffort: high", "model: 123", "provider: ''"])
def test_invalid_field_does_not_select_unintended_defaults(tmp_path, raw):
    path = tmp_path / "presets.yaml"
    _write(path, f"invalid: {{{raw}}}\n")
    with pytest.raises(ValueError, match="invalid"):
        load_presets(repo_path=path, user_path=None)


def test_user_override_replaces_whole_preset(tmp_path):
    repo = tmp_path / "presets.yaml"
    user = tmp_path / "user.yaml"
    _write(repo, "selected: {provider: chatgpt, model: gpt-5.5, reasoning_effort: high}\n")
    _write(user, "selected: {provider: chatgpt}\ncustom: {provider: chatgpt, model: gpt-5.5}\n")
    presets = load_presets(repo_path=repo, user_path=user)
    assert presets["selected"].model is None
    assert presets["selected"].reasoning_effort is None
    assert presets["custom"].model == "gpt-5.5"


def test_missing_user_file_is_allowed(tmp_path):
    repo = tmp_path / "presets.yaml"
    _write(repo, "default: {}\n")
    assert "default" in load_presets(repo_path=repo, user_path=tmp_path / "missing.yaml")


def test_user_config_directory_uses_override(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHASOLVE_CONFIG_DIR", str(tmp_path))
    assert _user_config_dir() == tmp_path


def test_user_config_directory_defaults_to_home(monkeypatch):
    monkeypatch.delenv("ALPHASOLVE_CONFIG_DIR", raising=False)
    assert _user_config_dir() == Path.home() / ".alphasolve"
