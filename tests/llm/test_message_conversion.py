from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from alphasolve.llm.config.preset import Preset
from alphasolve.llm.providers.openai_chat import OpenAIChatClient
from alphasolve.llm.providers.anthropic_messages import AnthropicMessagesClient
from alphasolve.llm.types import Message, ToolCall, ToolDef

LATEX_OLD = "\\frac{1}{2}"
LATEX_NEW = "\\frac{2}{3}"


def _openai_preset():
    return Preset(name="t", wire_format="openai_chat", base_url="u", api_key_env="OAI_K", model="m")


def _anthropic_preset():
    return Preset(name="t", wire_format="anthropic_messages", base_url="u", api_key_env="ANT_K", model="m")


def test_openai_latex_roundtrip(monkeypatch):
    """Send a tool-call assistant message containing LaTeX; assert it survives the wire."""
    monkeypatch.setenv("OAI_K", "sk-x")

    captured_wire = {}

    def _capture(**kwargs):
        captured_wire.update(kwargs)
        # Return a minimal response so .complete() finishes
        msg = MagicMock()
        msg.model_dump = MagicMock(return_value={"role": "assistant", "content": "ack"})
        msg.dict = msg.model_dump
        choice = MagicMock()
        choice.message = msg
        choice.finish_reason = "stop"
        resp = MagicMock()
        resp.choices = [choice]
        resp.usage = None
        return resp

    with patch("alphasolve.llm.providers.openai_chat.OpenAI") as MockOpenAI:
        mock = MagicMock()
        MockOpenAI.return_value = mock
        mock.chat.completions.create.side_effect = _capture

        client = OpenAIChatClient(_openai_preset())
        client.complete(
            messages=[Message(
                role="assistant",
                content="",
                tool_calls=(ToolCall(id="call_1", name="Edit", args={"old_str": LATEX_OLD, "new_str": LATEX_NEW}),),
            )],
            tools=[],
        )
        sent_args_str = captured_wire["messages"][0]["tool_calls"][0]["function"]["arguments"]
        # Wire is JSON string. Parse it back and verify LaTeX survives.
        import json as _json
        parsed = _json.loads(sent_args_str)
        assert parsed["old_str"] == LATEX_OLD
        assert parsed["new_str"] == LATEX_NEW


def test_anthropic_latex_roundtrip(monkeypatch):
    """Anthropic delivers tool_use input as dict - no JSON-escape gymnastics."""
    monkeypatch.setenv("ANT_K", "sk-x")

    captured_wire = {}

    def _capture(**kwargs):
        captured_wire.update(kwargs)
        b = MagicMock()
        b.type = "text"
        b.text = "ack"
        resp = MagicMock()
        resp.content = [b]
        resp.stop_reason = "end_turn"
        resp.usage = MagicMock(input_tokens=0, output_tokens=0, cache_read_input_tokens=0)
        return resp

    with patch("alphasolve.llm.providers.anthropic_messages.Anthropic") as MockAnthropic:
        mock = MagicMock()
        MockAnthropic.return_value = mock
        mock.messages.create.side_effect = _capture

        client = AnthropicMessagesClient(_anthropic_preset())
        client.complete(
            messages=[Message(
                role="assistant",
                content="",
                tool_calls=(ToolCall(id="t1", name="Edit", args={"old_str": LATEX_OLD, "new_str": LATEX_NEW}),),
            )],
            tools=[],
        )
        # Anthropic native form: tool_use block in assistant content with input as DICT.
        assistant_block = captured_wire["messages"][0]["content"]
        tool_use = next(b for b in assistant_block if b["type"] == "tool_use")
        assert isinstance(tool_use["input"], dict)
        assert tool_use["input"]["old_str"] == LATEX_OLD
        assert tool_use["input"]["new_str"] == LATEX_NEW


def test_openai_response_args_parse_back_to_dict(monkeypatch):
    """The reverse path: when OpenAI returns a tool_call with stringified args, the lingua-franca exposes them as dict."""
    monkeypatch.setenv("OAI_K", "sk-x")
    with patch("alphasolve.llm.providers.openai_chat.OpenAI") as MockOpenAI:
        mock = MagicMock()
        MockOpenAI.return_value = mock
        msg = MagicMock()
        # IMPORTANT: arguments uses JSON-string-encoded LaTeX (2 escaping levels)
        msg.model_dump = MagicMock(return_value={
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "Edit",
                    "arguments": '{"old_str": "\\\\frac{1}{2}", "new_str": "\\\\frac{2}{3}"}',
                },
            }],
        })
        msg.dict = msg.model_dump
        choice = MagicMock(); choice.message = msg; choice.finish_reason = "tool_calls"
        resp = MagicMock(); resp.choices = [choice]; resp.usage = None
        mock.chat.completions.create.return_value = resp

        client = OpenAIChatClient(_openai_preset())
        out = client.complete(messages=[Message(role="user", content="x")], tools=[])
        tc = out.message.tool_calls[0]
        assert isinstance(tc.args, dict)
        assert tc.args["old_str"] == LATEX_OLD
        assert tc.args["new_str"] == LATEX_NEW
