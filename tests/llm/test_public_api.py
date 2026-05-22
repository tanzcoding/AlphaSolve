from __future__ import annotations


def test_public_api_exports_exactly_19_names():
    import alphasolve.llm as mod
    expected = {
        # Types
        "ChatClient", "ChatDeltaSink", "ChatCompletionError",
        "Message", "Role", "ToolCall", "ToolDef",
        "CompletionResponse", "Usage", "StreamDelta", "FinishReason",
        # Config
        "Preset", "Profile", "WireFormat",
        # Loaders + factory
        "load_presets", "load_profile", "load_active_profile",
        "make_client", "make_client_factory",
    }
    actual = set(mod.__all__)
    assert actual == expected, f"missing: {expected - actual}, extra: {actual - expected}"


def test_public_api_names_are_importable():
    import alphasolve.llm as mod
    for name in mod.__all__:
        assert hasattr(mod, name), f"{name} listed in __all__ but not actually exported"
