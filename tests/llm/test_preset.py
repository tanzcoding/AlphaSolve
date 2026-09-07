from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from alphasolve.llm import CodexClient, Preset


def test_default_preset_uses_subscription_without_fixing_model():
    preset = Preset(name="default")
    assert preset.provider == "chatgpt"
    assert preset.model is None
    assert preset.base_url is None
    assert preset.api_key_env is None


def test_preset_can_select_model_and_reasoning_effort():
    preset = Preset(name="fixed", model="gpt-5.5", reasoning_effort="high")
    assert CodexClient(preset).model == "gpt-5.5"
    assert preset.reasoning_effort == "high"


def test_role_configuration_cannot_mutate_shared_preset():
    preset = Preset(name="shared")
    with pytest.raises(FrozenInstanceError):
        preset.provider = "other"
