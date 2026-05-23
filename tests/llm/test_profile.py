from __future__ import annotations

import pytest

from alphasolve.llm.config.tier import TierMapping


def test_tier_mapping_construction():
    tm = TierMapping(
        name="balanced",
        tier_to_preset={
            "orchestrator": "deepseek-pro",
            "verifier": "deepseek-pro",
        },
    )
    assert tm.name == "balanced"
    assert tm.preset_for("verifier") == "deepseek-pro"


def test_preset_for_unknown_tier_raises():
    tm = TierMapping(name="cheap", tier_to_preset={"orchestrator": "deepseek-flash"})
    with pytest.raises(KeyError) as exc:
        tm.preset_for("verifier")
    assert "verifier" in str(exc.value)
    assert "cheap" in str(exc.value)
    assert "orchestrator" in str(exc.value)  # available tiers listed
