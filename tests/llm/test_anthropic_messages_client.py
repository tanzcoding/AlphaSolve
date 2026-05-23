from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from alphasolve.llm.config.preset import Preset
from alphasolve.llm.providers.anthropic_messages import AnthropicMessagesClient
from alphasolve.llm.types import Message, ToolCall, ToolDef


def _preset(**overrides) -> Preset:
    defaults = dict(
        name="test-anthropic",
        wire_format="anthropic_messages",
        base_url="https://api.anthropic.example",
        api_key_env="ANTHROPIC_TEST_KEY",
        model="claude-test-1",
        timeout=120,
        params={},
    )
    defaults.update(overrides)
    return Preset(**defaults)


def _mock_anthropic_response(*, text: str = "", tool_uses=None, stop_reason: str = "end_turn"):
    """Mimic anthropic.types.Message shape."""
    content_blocks = []
    if text:
        b = MagicMock()
        b.type = "text"
        b.text = text
        content_blocks.append(b)
    for tu in tool_uses or []:
        b = MagicMock()
        b.type = "tool_use"
        b.id = tu["id"]
        b.name = tu["name"]
        b.input = tu["input"]   # dict, NOT string
        content_blocks.append(b)
    resp = MagicMock()
    resp.content = content_blocks
    resp.stop_reason = stop_reason
    resp.usage = MagicMock(input_tokens=10, output_tokens=5, cache_read_input_tokens=0)
    resp.model_dump = MagicMock(return_value={"content": "[mock]"})
    return resp


def test_constructs_with_preset(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "sk-anthropic")
    with patch("alphasolve.llm.providers.anthropic_messages.Anthropic") as MockAnthropic:
        AnthropicMessagesClient(_preset())
        kwargs = MockAnthropic.call_args.kwargs
        assert kwargs["api_key"] == "sk-anthropic"
        assert kwargs["base_url"] == "https://api.anthropic.example"


def test_system_message_lifted_to_kwarg(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "sk-anthropic")
    with patch("alphasolve.llm.providers.anthropic_messages.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client
        mock_client.messages.create.return_value = _mock_anthropic_response(text="hello")

        client = AnthropicMessagesClient(_preset())
        client.complete(
            messages=[
                Message(role="system", content="you are helpful"),
                Message(role="user", content="hi"),
            ],
            tools=[],
        )
        kwargs = mock_client.messages.create.call_args.kwargs
        assert kwargs["system"] == "you are helpful"
        # the user message goes in messages=, with NO system entry there
        assert kwargs["messages"][0]["role"] == "user"
        assert all(m["role"] != "system" for m in kwargs["messages"])


def test_tool_messages_collapse_into_user_tool_result(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "sk-anthropic")
    with patch("alphasolve.llm.providers.anthropic_messages.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client
        mock_client.messages.create.return_value = _mock_anthropic_response(text="ok")

        client = AnthropicMessagesClient(_preset())
        client.complete(
            messages=[
                Message(role="user", content="edit foo"),
                Message(
                    role="assistant",
                    content="",
                    tool_calls=(ToolCall(id="t1", name="Edit", args={"old_str": "a", "new_str": "b"}),),
                ),
                Message(role="tool", content="ok", tool_call_id="t1", name="Edit"),
            ],
            tools=[],
        )
        sent = mock_client.messages.create.call_args.kwargs["messages"]
        # Expected shape:
        #   [0] user: text
        #   [1] assistant: tool_use block (Anthropic native form)
        #   [2] user: tool_result block (the role="tool" message folded back into user)
        assert sent[0]["role"] == "user"
        assert sent[1]["role"] == "assistant"
        assert any(blk["type"] == "tool_use" for blk in sent[1]["content"])
        assert sent[2]["role"] == "user"
        assert sent[2]["content"][0]["type"] == "tool_result"
        assert sent[2]["content"][0]["tool_use_id"] == "t1"


def test_tool_use_input_is_dict_not_stringified(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "sk-anthropic")
    with patch("alphasolve.llm.providers.anthropic_messages.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client
        mock_client.messages.create.return_value = _mock_anthropic_response(
            text="",
            tool_uses=[{
                "id": "tu_1",
                "name": "Edit",
                "input": {"old_str": "\\frac{1}{2}", "new_str": "\\frac{2}{3}"},
            }],
            stop_reason="tool_use",
        )

        client = AnthropicMessagesClient(_preset())
        resp = client.complete(messages=[Message(role="user", content="edit")], tools=[])
        assert resp.finish_reason == "tool_calls"
        tc = resp.message.tool_calls[0]
        # KEY ASSERTION: protocol advantage preserved — args is a dict, no double-stringification
        assert isinstance(tc.args, dict)
        assert tc.args["old_str"] == "\\frac{1}{2}"
        assert tc.args["new_str"] == "\\frac{2}{3}"


def test_tool_def_converts_to_anthropic_schema(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "sk-anthropic")
    with patch("alphasolve.llm.providers.anthropic_messages.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client
        mock_client.messages.create.return_value = _mock_anthropic_response(text="ok")

        client = AnthropicMessagesClient(_preset())
        td = ToolDef(name="Read", description="read a file", parameters={"type": "object", "properties": {"path": {"type": "string"}}})
        client.complete(messages=[Message(role="user", content="x")], tools=[td])
        sent_tools = mock_client.messages.create.call_args.kwargs["tools"]
        assert len(sent_tools) == 1
        assert sent_tools[0]["name"] == "Read"
        assert sent_tools[0]["description"] == "read a file"
        # Anthropic calls it input_schema, not parameters
        assert sent_tools[0]["input_schema"] == {"type": "object", "properties": {"path": {"type": "string"}}}
        # ...and there is no `parameters` key
        assert "parameters" not in sent_tools[0]


def test_usage_extracted(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "sk-anthropic")
    with patch("alphasolve.llm.providers.anthropic_messages.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        MockAnthropic.return_value = mock_client
        mock_client.messages.create.return_value = _mock_anthropic_response(text="ok")

        client = AnthropicMessagesClient(_preset())
        resp = client.complete(messages=[Message(role="user", content="x")], tools=[])
        assert resp.usage.input_tokens == 10
        assert resp.usage.output_tokens == 5
