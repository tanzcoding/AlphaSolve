from __future__ import annotations

from dataclasses import dataclass

import pytest

from alphasolve.llm import CodexClient, Preset, TierMapping, make_client, make_client_factory


@dataclass
class _AgentConfig:
    tier: str

    def effective_tier(self) -> str:
        return self.tier


def test_factory_selects_subscription_and_api_for_different_roles(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    subscription = Preset(name="subscription")
    api = Preset(
        name="api", provider="deepseek", model="deepseek-v4-pro",
        base_url="https://api.deepseek.com", api_key_env="DEEPSEEK_API_KEY",
    )
    factory = make_client_factory(
        TierMapping(name="default", tier_to_preset={"balanced": "subscription", "cheap": "api"}),
        {"subscription": subscription, "api": api},
    )

    assert factory(_AgentConfig("balanced")).preset is subscription
    assert factory(_AgentConfig("cheap")).preset is api


def test_client_construction_does_not_start_codex_or_require_authentication():
    client = make_client(Preset(name="default"))
    assert isinstance(client, CodexClient)
    assert client.model is None
    assert not hasattr(client, "complete")


def test_factory_rejects_unknown_tier():
    factory = make_client_factory(
        TierMapping(name="default", tier_to_preset={"balanced": "default"}),
        {"default": Preset(name="default")},
    )
    with pytest.raises(KeyError, match="max"):
        factory(_AgentConfig("max"))


def test_factory_rejects_missing_preset():
    factory = make_client_factory(
        TierMapping(name="default", tier_to_preset={"balanced": "missing"}), {},
    )
    with pytest.raises(KeyError, match="missing"):
        factory(_AgentConfig("balanced"))
