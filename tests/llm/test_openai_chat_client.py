from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from alphasolve.llm.config.preset import Preset
from alphasolve.llm.providers.openai_chat import OpenAIChatClient


def _preset(**overrides) -> Preset:
    defaults = dict(
        name="test-preset",
        wire_format="openai_chat",
        base_url="https://api.test.example/v1",
        api_key_env="TEST_KEY",
        model="test-model-1",
        timeout=120,
        params={},
    )
    defaults.update(overrides)
    return Preset(**defaults)


def test_constructs_with_preset(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    with patch("alphasolve.llm.providers.openai_chat.OpenAI") as MockOpenAI:
        client = OpenAIChatClient(_preset())
        MockOpenAI.assert_called_once()
        kwargs = MockOpenAI.call_args.kwargs
        assert kwargs["api_key"] == "sk-test"
        assert kwargs["base_url"] == "https://api.test.example/v1"
        assert kwargs["timeout"] == 120


def test_missing_api_key_raises_at_construction(monkeypatch):
    monkeypatch.delenv("TEST_KEY", raising=False)
    with pytest.raises(RuntimeError) as exc:
        OpenAIChatClient(_preset())
    assert "TEST_KEY" in str(exc.value)


from alphasolve.llm.types import Message, ToolDef


def _mock_response(content: str = "", tool_calls=None, finish_reason: str = "stop"):
    msg = MagicMock()
    msg.model_dump = MagicMock(return_value={
        "role": "assistant",
        "content": content,
        "tool_calls": tool_calls or [],
    })
    msg.dict = msg.model_dump
    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = finish_reason
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = None
    return resp


def test_complete_text_response(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    with patch("alphasolve.llm.providers.openai_chat.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        mock_client.chat.completions.create.return_value = _mock_response(content="hello")

        client = OpenAIChatClient(_preset())
        resp = client.complete(messages=[Message(role="user", content="hi")], tools=[])
        assert resp.message.role == "assistant"
        assert resp.message.content == "hello"
        assert resp.finish_reason == "stop"
        assert resp.message.tool_calls == ()


def test_complete_tool_call_response(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    with patch("alphasolve.llm.providers.openai_chat.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        mock_client.chat.completions.create.return_value = _mock_response(
            content="",
            tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "Edit",
                    "arguments": '{"old_str": "\\\\frac{1}{2}", "new_str": "\\\\frac{2}{3}"}',
                },
            }],
            finish_reason="tool_calls",
        )

        client = OpenAIChatClient(_preset())
        resp = client.complete(
            messages=[Message(role="user", content="edit")],
            tools=[ToolDef(name="Edit", description="", parameters={})],
        )
        assert resp.finish_reason == "tool_calls"
        assert len(resp.message.tool_calls) == 1
        tc = resp.message.tool_calls[0]
        assert tc.id == "call_1"
        assert tc.name == "Edit"
        # CRITICAL: args is a dict, and the LaTeX backslashes are preserved as single backslash strings
        assert isinstance(tc.args, dict)
        assert tc.args["old_str"] == "\\frac{1}{2}"
        assert tc.args["new_str"] == "\\frac{2}{3}"


def test_complete_invalid_tool_args_raises(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    with patch("alphasolve.llm.providers.openai_chat.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        mock_client.chat.completions.create.return_value = _mock_response(
            content="",
            tool_calls=[{
                "id": "call_bad",
                "type": "function",
                "function": {"name": "Edit", "arguments": '{"path": "broken'},
            }],
            finish_reason="tool_calls",
        )

        from alphasolve.llm.types import ChatCompletionError
        client = OpenAIChatClient(_preset())
        with pytest.raises(ChatCompletionError) as exc:
            client.complete(messages=[Message(role="user", content="x")], tools=[])
        assert "call_bad" in str(exc.value)


def test_messages_serialize_tool_calls_to_openai_dict(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    with patch("alphasolve.llm.providers.openai_chat.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        mock_client.chat.completions.create.return_value = _mock_response(content="ack")

        from alphasolve.llm.types import ToolCall
        client = OpenAIChatClient(_preset())
        msgs = [
            Message(role="user", content="please edit"),
            Message(
                role="assistant",
                content="",
                tool_calls=(ToolCall(id="t1", name="Edit", args={"old_str": "a", "new_str": "b"}),),
            ),
            Message(role="tool", content="ok", tool_call_id="t1", name="Edit"),
        ]
        client.complete(messages=msgs, tools=[])
        # Inspect the kwargs the OpenAI SDK was called with
        sent = mock_client.chat.completions.create.call_args.kwargs["messages"]
        assert len(sent) == 3
        assert sent[1]["tool_calls"][0]["function"]["name"] == "Edit"
        # arguments is a JSON string at the wire layer
        assert sent[1]["tool_calls"][0]["function"]["arguments"] == '{"old_str": "a", "new_str": "b"}'
        assert sent[2]["role"] == "tool"
        assert sent[2]["tool_call_id"] == "t1"
