from __future__ import annotations

from pathlib import Path

from alphasolve.llm import load_presets, load_tier_mapping


CONFIG_ROOT = Path(__file__).resolve().parents[2] / "src" / "alphasolve" / "config"


def test_shipped_presets_are_subscription_or_explicit_responses_apis():
    presets = load_presets(repo_path=CONFIG_ROOT / "presets.yaml", user_path=None)
    assert "gpt-5.6-sol" in presets
    assert "gpt-5.6-luna" in presets
    for preset in presets.values():
        if preset.provider == "chatgpt":
            assert preset.api_key_env is None
            assert preset.base_url is None
        else:
            assert preset.model
            assert preset.api_key_env
            assert preset.base_url.startswith("https://")


def test_shipped_tiers_use_luna_for_cheap_and_sol_for_balanced_and_max():
    mapping = load_tier_mapping(repo_path=CONFIG_ROOT / "tiers.yaml", user_path=None)
    presets = load_presets(repo_path=CONFIG_ROOT / "presets.yaml", user_path=None)
    assert mapping.tier_to_preset == {
        "cheap": "gpt-5.6-luna",
        "balanced": "gpt-5.6-sol",
        "max": "gpt-5.6-sol",
    }
    for name in mapping.tier_to_preset.values():
        assert presets[name].provider == "chatgpt"
        assert presets[name].model == name
    assert presets["gpt-5.6-luna"].reasoning_effort is None
    assert presets["gpt-5.6-sol"].provider == "chatgpt"
    assert presets["gpt-5.6-sol"].model == "gpt-5.6-sol"
    assert presets["gpt-5.6-sol"].reasoning_effort == "max"


def test_shipped_configs_do_not_use_legacy_model_config_field():
    for path in CONFIG_ROOT.rglob("*.yaml"):
        assert "model_config:" not in path.read_text(encoding="utf-8")
