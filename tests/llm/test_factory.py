from __future__ import annotations

from dataclasses import dataclass

import pytest

from alphasolve.llm import make_client, make_client_factory
from alphasolve.llm.config.preset import Preset
from alphasolve.llm.config.profile import Profile
from alphasolve.llm.providers.openai_chat import OpenAIChatClient
from alphasolve.llm.providers.anthropic_messages import AnthropicMessagesClient


def _preset(name: str, wire: str = "openai_chat") -> Preset:
    return Preset(
        name=name, wire_format=wire,
        base_url="https://example.com", api_key_env="X", model="m",
    )


@dataclass
class _FakeAgentConfig:
    name: str
    role: str | None = None

    def effective_role(self) -> str:
        return self.role or self.name


def test_make_client_dispatches_openai(monkeypatch):
    monkeypatch.setenv("X", "sk")
    c = make_client(_preset("a", "openai_chat"))
    assert isinstance(c, OpenAIChatClient)


def test_make_client_dispatches_anthropic(monkeypatch):
    monkeypatch.setenv("X", "sk")
    c = make_client(_preset("a", "anthropic_messages"))
    assert isinstance(c, AnthropicMessagesClient)


def test_make_client_unknown_wire_format(monkeypatch):
    monkeypatch.setenv("X", "sk")
    # Construct manually bypassing Literal type checking
    p = Preset.__new__(Preset)
    object.__setattr__(p, "name", "x")
    object.__setattr__(p, "wire_format", "gemini_native")
    object.__setattr__(p, "base_url", "u")
    object.__setattr__(p, "api_key_env", "X")
    object.__setattr__(p, "model", "m")
    object.__setattr__(p, "timeout", 3600.0)
    object.__setattr__(p, "params", {})
    with pytest.raises(ValueError) as exc:
        make_client(p)
    assert "gemini_native" in str(exc.value)


def test_make_client_factory_resolves_role(monkeypatch):
    monkeypatch.setenv("X", "sk")
    presets = {"p1": _preset("p1"), "p2": _preset("p2")}
    profile = Profile(name="t", role_to_preset={"verifier": "p1", "curator": "p2"})

    factory = make_client_factory(profile, presets)
    client_v = factory(_FakeAgentConfig(name="verifier_adversarial", role="verifier"))
    client_c = factory(_FakeAgentConfig(name="curator"))  # role falls back to name
    assert isinstance(client_v, OpenAIChatClient)
    assert isinstance(client_c, OpenAIChatClient)
    # Different presets selected
    assert client_v.preset.name == "p1"
    assert client_c.preset.name == "p2"


def test_make_client_factory_unknown_role(monkeypatch):
    monkeypatch.setenv("X", "sk")
    presets = {"p1": _preset("p1")}
    profile = Profile(name="t", role_to_preset={"verifier": "p1"})
    factory = make_client_factory(profile, presets)
    with pytest.raises(KeyError):
        factory(_FakeAgentConfig(name="orchestrator"))


def test_make_client_factory_role_maps_to_missing_preset(monkeypatch):
    monkeypatch.setenv("X", "sk")
    presets = {"p1": _preset("p1")}
    profile = Profile(name="t", role_to_preset={"verifier": "p_ghost"})
    factory = make_client_factory(profile, presets)
    with pytest.raises(KeyError) as exc:
        factory(_FakeAgentConfig(name="verifier"))
    assert "p_ghost" in str(exc.value)
