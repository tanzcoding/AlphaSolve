"""Codex 适配器公共 API 测试：保留工具和角色接口，移除旧上下文策略。"""
from __future__ import annotations


def test_agent_public_api_complete():
    """每个名字都能 from alphasolve.agent import；__all__ 与实际 export 一致。"""
    import alphasolve.agent as mod
    expected = {
        # 运行时。
        "Agent", "AgentRunResult", "AgentRunError", "AgentEventSink",
        # 配置。
        "AgentConfig", "AgentSuite", "load_agent_config", "load_agent_suite",
        # 工具。
        "ToolRegistry", "ToolResult", "RegisteredTool",
        "build_default_tool_registry",
        "SubagentDispatcher", "register_agent_tool",
        # 工作区。
        "Workspace", "WorkspaceLike", "PagedReadResult",
    }
    assert set(mod.__all__) == expected, (
        f"__all__ drift. Missing: {expected - set(mod.__all__)}, "
        f"Extra: {set(mod.__all__) - expected}"
    )
    for name in expected:
        assert hasattr(mod, name), f"alphasolve.agent missing: {name}"


def test_agent_does_not_reexport_model_configuration_or_trace_types():
    """模型配置和可见 trace 类型继续从 alphasolve.llm 获取。"""
    import alphasolve.agent as mod
    forbidden = {
        "Message", "CodexClient", "ToolDef", "ToolCall", "Usage", "Role",
        "Preset", "TierMapping",
        "load_presets", "load_tier_mapping",
        "make_client", "make_client_factory",
    }
    for name in forbidden:
        assert not hasattr(mod, name), (
            f"{name} should only be in alphasolve.llm, not re-exported by alphasolve.agent"
        )


def test_agent_does_not_export_legacy_context_policy():
    import alphasolve.agent as module

    assert not hasattr(module, "AgentContextPolicy")
    assert not hasattr(module, "AgentContextPolicyInput")


def test_agent_does_not_import_workflow():
    """第二层 import alphasolve.solver.* 是反向依赖，禁止。"""
    import alphasolve.agent.agent as mod_a
    import alphasolve.agent.config as mod_c
    import alphasolve.agent.tools as mod_t
    import alphasolve.agent.workspace as mod_w
    for mod in (mod_a, mod_c, mod_t, mod_w):
        for name in dir(mod):
            obj = getattr(mod, name)
            module = getattr(obj, "__module__", "")
            assert not module.startswith("alphasolve.solver"), (
                f"{mod.__name__} pulls in {name} from {module}"
            )
