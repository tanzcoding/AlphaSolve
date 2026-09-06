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


# ---------------------------------------------------------------------------
# _messages_to_openai repair logic — prevents DeepSeek 400 errors
# ---------------------------------------------------------------------------

from alphasolve.llm.providers.openai_chat import _messages_to_openai


def test_empty_tool_call_id_backfilled():
    """Assistant with empty tool_call id gets a non-empty backfill id."""
    msgs = [
        Message(role="user", content="hi"),
        Message(role="assistant", content="", tool_calls=(
            ToolCall(id="", name="Read", args={}),
        )),
        Message(role="tool", content="result", tool_call_id="", name="Read"),
    ]
    out = _messages_to_openai(msgs, thinking_mode=False)
    assistant_tc_id = out[1]["tool_calls"][0]["id"]
    tool_tc_id = out[2]["tool_call_id"]
    assert assistant_tc_id != ""
    assert tool_tc_id == assistant_tc_id  # tool result matches


def test_mismatched_tool_call_id_reconciled():
    """Tool message with wrong tool_call_id is reconciled to the assistant's id."""
    msgs = [
        Message(role="user", content="hi"),
        Message(role="assistant", content="", tool_calls=(
            ToolCall(id="real_id", name="Read", args={}),
        )),
        Message(role="tool", content="result", tool_call_id="wrong_id", name="Read"),
    ]
    out = _messages_to_openai(msgs, thinking_mode=False)
    assert out[2]["tool_call_id"] == "real_id"


def test_missing_tool_results_backfilled():
    """Assistant with 2 tool_calls but only 1 tool result gets a placeholder for the 2nd."""
    msgs = [
        Message(role="user", content="hi"),
        Message(role="assistant", content="", tool_calls=(
            ToolCall(id="call_a", name="Read", args={}),
            ToolCall(id="call_b", name="Grep", args={}),
        )),
        Message(role="tool", content="result_a", tool_call_id="call_a", name="Read"),
        # call_b has no tool result — next message is a new user turn
        Message(role="user", content="next"),
    ]
    out = _messages_to_openai(msgs, thinking_mode=False)
    # After the tool result for call_a, a placeholder for call_b should appear
    # before the next user message.
    tool_msgs = [m for m in out if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    assert tool_msgs[0]["tool_call_id"] == "call_a"
    assert tool_msgs[1]["tool_call_id"] == "call_b"
    assert tool_msgs[1]["content"] == ""  # placeholder


def test_orphan_tool_message_dropped():
    """A tool message with no preceding assistant tool_calls is dropped."""
    msgs = [
        Message(role="user", content="hi"),
        Message(role="tool", content="orphan", tool_call_id="ghost", name="Read"),
        Message(role="assistant", content="hello"),
    ]
    out = _messages_to_openai(msgs, thinking_mode=False)
    assert not any(m["role"] == "tool" for m in out)


def test_duplicate_tool_call_ids_backfilled():
    """Two tool_calls with the same id each get unique ids."""
    msgs = [
        Message(role="user", content="hi"),
        Message(role="assistant", content="", tool_calls=(
            ToolCall(id="same", name="Read", args={}),
            ToolCall(id="same", name="Grep", args={}),
        )),
        Message(role="tool", content="r1", tool_call_id="same", name="Read"),
        Message(role="tool", content="r2", tool_call_id="same", name="Grep"),
    ]
    out = _messages_to_openai(msgs, thinking_mode=False)
    tc_ids = [tc["id"] for tc in out[1]["tool_calls"]]
    assert len(tc_ids) == 2
    assert tc_ids[0] != tc_ids[1]  # deduplicated
    # Both tool results should match their respective ids
    tool_msgs = [m for m in out if m["role"] == "tool"]
    assert tool_msgs[0]["tool_call_id"] == tc_ids[0]
    assert tool_msgs[1]["tool_call_id"] == tc_ids[1]


def test_tail_missing_tool_results_backfilled():
    """If the last message is an assistant with tool_calls and no results, placeholders are appended."""
    msgs = [
        Message(role="user", content="hi"),
        Message(role="assistant", content="", tool_calls=(
            ToolCall(id="call_x", name="Read", args={}),
        )),
    ]
    out = _messages_to_openai(msgs, thinking_mode=False)
    assert out[-1]["role"] == "tool"
    assert out[-1]["tool_call_id"] == "call_x"
    assert out[-1]["content"] == ""
