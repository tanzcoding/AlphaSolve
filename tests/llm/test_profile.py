from __future__ import annotations

import pytest

from alphasolve.llm.config.profile import Profile


def test_profile_construction():
    p = Profile(
        name="balanced",
        role_to_preset={
            "orchestrator": "deepseek-pro",
            "verifier": "deepseek-pro",
        },
    )
    assert p.name == "balanced"
    assert p.preset_for("verifier") == "deepseek-pro"


def test_preset_for_unknown_role_raises():
    p = Profile(name="cheap", role_to_preset={"orchestrator": "deepseek-flash"})
    with pytest.raises(KeyError) as exc:
        p.preset_for("verifier")
    assert "verifier" in str(exc.value)
    assert "cheap" in str(exc.value)
    assert "orchestrator" in str(exc.value)  # available roles listed
