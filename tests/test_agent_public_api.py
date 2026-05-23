"""第二层公共 API 烟雾测试：__all__ 完整可用且不泄漏 LLM 类型。"""
from __future__ import annotations


def test_agent_public_api_complete():
    """每个名字都能 from alphasolve.agent import；__all__ 与实际 export 一致。"""
    import alphasolve.agent as mod
    expected = {
        # Agent runtime
        "Agent", "AgentRunResult", "AgentRunError", "AgentEventSink",
        # Config
        "AgentConfig", "AgentSuite", "load_agent_config", "load_agent_suite",
        # Tools
        "ToolRegistry", "ToolResult", "RegisteredTool",
        "build_default_tool_registry",
        "SubagentDispatcher", "register_agent_tool",
        # Workspace
        "Workspace", "WorkspaceLike", "PagedReadResult",
    }
    assert set(mod.__all__) == expected, (
        f"__all__ drift. Missing: {expected - set(mod.__all__)}, "
        f"Extra: {set(mod.__all__) - expected}"
    )
    for name in expected:
        assert hasattr(mod, name), f"alphasolve.agent missing: {name}"


def test_agent_does_not_reexport_llm_types():
    """LLM 类型继续从 alphasolve.llm 拿；第二层不做中转。"""
    import alphasolve.agent as mod
    forbidden = {
        "Message", "ChatClient", "ChatDeltaSink", "ToolDef", "ToolCall",
        "CompletionResponse", "Usage", "StreamDelta", "FinishReason", "Role",
        "Preset", "Profile", "WireFormat",
        "load_presets", "load_profile", "load_active_profile",
        "make_client", "make_client_factory",
    }
    for name in forbidden:
        assert not hasattr(mod, name), (
            f"{name} should only be in alphasolve.llm, not re-exported by alphasolve.agent"
        )


def test_agent_does_not_import_workflow():
    """第二层 import alphasolve.workflow.* 是反向依赖，禁止。"""
    import alphasolve.agent.agent as mod_a
    import alphasolve.agent.config as mod_c
    import alphasolve.agent.tools as mod_t
    import alphasolve.agent.workspace as mod_w
    for mod in (mod_a, mod_c, mod_t, mod_w):
        for name in dir(mod):
            obj = getattr(mod, name)
            module = getattr(obj, "__module__", "")
            assert not module.startswith("alphasolve.workflow"), (
                f"{mod.__name__} pulls in {name} from {module}"
            )
