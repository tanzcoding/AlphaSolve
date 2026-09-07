from __future__ import annotations


def test_codex_configuration_and_trace_types_are_public():
    import alphasolve.llm as module

    expected = {
        "CodexClient", "Message", "Role", "ToolCall", "ToolDef", "Usage",
        "Preset", "TierMapping", "load_presets", "load_tier_mapping",
        "make_client", "make_client_factory",
    }
    assert set(module.__all__) == expected
    for name in expected:
        assert hasattr(module, name)


def test_legacy_provider_protocol_is_not_exported():
    import alphasolve.llm as module

    for name in ("ChatClient", "CompletionResponse", "WireFormat", "StreamDelta"):
        assert not hasattr(module, name)
