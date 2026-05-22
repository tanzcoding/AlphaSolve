from __future__ import annotations

import pytest

from alphasolve.llm.config.preset import Preset


def test_preset_construction():
    p = Preset(
        name="deepseek-pro",
        wire_format="openai_chat",
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        model="deepseek-v4-pro",
    )
    assert p.name == "deepseek-pro"
    assert p.timeout == 3600
    assert p.params == {}


def test_preset_with_params():
    p = Preset(
        name="kimi",
        wire_format="openai_chat",
        base_url="https://api.moonshot.cn/v1",
        api_key_env="MOONSHOT_API_KEY",
        model="kimi-k2-thinking",
        params={"temperature": 1.0},
    )
    assert p.params == {"temperature": 1.0}


def test_preset_is_frozen():
    p = Preset(name="x", wire_format="openai_chat", base_url="u", api_key_env="K", model="m")
    with pytest.raises(Exception):  # FrozenInstanceError
        p.name = "y"


def test_resolve_api_key_happy(monkeypatch):
    monkeypatch.setenv("MY_SECRET", "sk-xyz")
    p = Preset(name="x", wire_format="openai_chat", base_url="u", api_key_env="MY_SECRET", model="m")
    assert p.resolve_api_key() == "sk-xyz"


def test_resolve_api_key_missing_env(monkeypatch):
    monkeypatch.delenv("NEVER_SET_VAR", raising=False)
    p = Preset(name="prov", wire_format="openai_chat", base_url="u", api_key_env="NEVER_SET_VAR", model="m")
    with pytest.raises(RuntimeError) as exc:
        p.resolve_api_key()
    assert "NEVER_SET_VAR" in str(exc.value)
    assert "prov" in str(exc.value)


def test_unknown_wire_format_type_hint_does_not_enforce_at_construction():
    # Literal types are not runtime-enforced; loader must check.
    Preset(name="x", wire_format="gemini_native", base_url="u", api_key_env="K", model="m")  # type: ignore[arg-type]
