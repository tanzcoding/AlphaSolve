from __future__ import annotations

from pathlib import Path

from alphasolve.llm.config.loader import load_presets, load_active_profile

PRESETS_PATH = Path(__file__).parent.parent.parent / "src" / "alphasolve" / "config" / "presets.yaml"
PROFILES_PATH = Path(__file__).parent.parent.parent / "src" / "alphasolve" / "config" / "profiles.yaml"


def test_shipped_presets_load():
    presets = load_presets(repo_path=PRESETS_PATH, user_path=None)
    expected_openai = {
        "deepseek-flash", "deepseek-pro", "parasail-deepseek", "longcat",
        "moonshot-kimi", "volcano-doubao", "volcano-deepseek",
        "dashscope-deepseek", "mimo", "openrouter-gemini",
        "qwen-3.7-max",
    }
    expected_anthropic = {"deepseek-pro-anthropic", "moonshot-kimi-anthropic"}
    assert set(presets) == expected_openai | expected_anthropic
    for name in expected_openai:
        assert presets[name].wire_format == "openai_chat", name
    for name in expected_anthropic:
        assert presets[name].wire_format == "anthropic_messages", name


def test_balanced_profile_role_coverage():
    p = load_active_profile(name="balanced", repo_path=PROFILES_PATH, user_path=None)
    required = {"orchestrator", "generator", "verifier", "reviser",
                "curator", "compute_subagent", "proof_subagent"}
    assert set(p.role_to_preset) == required


def test_each_profile_role_resolves_to_a_real_preset():
    presets = load_presets(repo_path=PRESETS_PATH, user_path=None)
    for profile_name in ("cheap", "balanced", "strategic"):
        p = load_active_profile(name=profile_name, repo_path=PROFILES_PATH, user_path=None)
        for role, preset_name in p.role_to_preset.items():
            assert preset_name in presets, (
                f"profile {profile_name!r} role {role!r} -> preset {preset_name!r} not found"
            )


def test_default_profile_is_balanced():
    p = load_active_profile(name=None, repo_path=PROFILES_PATH, user_path=None)
    assert p.name == "balanced"
