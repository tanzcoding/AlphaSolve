"""离线工作流测试复用 Codex 事件适配，不把旧模型循环带入生产代码。"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest


# 避免本机其他 checkout 的 editable 安装抢先于当前工作区。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(autouse=True)
def scripted_codex_session(monkeypatch):
    from alphasolve.agent import Agent
    from alphasolve.llm import CodexClient
    from tests.codex_fakes import ScriptedCodexSession

    original = Agent._make_session

    def make_session(agent):
        if isinstance(agent.client, CodexClient):
            return original(agent)
        return ScriptedCodexSession(
            client=agent.client,
            system_prompt=agent.config.system_prompt,
            tools=agent.tool_registry.tool_defs(
                agent.config.tools, agent.config.tool_parameters, agent.config.tool_descriptions,
            ),
        )

    monkeypatch.setattr(Agent, "_make_session", make_session)
