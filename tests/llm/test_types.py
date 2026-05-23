from __future__ import annotations

import pytest
import dataclasses

from alphasolve.llm.types import (
    Message, ToolCall, ToolDef, CompletionResponse, Usage, StreamDelta,
)


def test_message_is_frozen():
    msg = Message(role="user", content="hi")
    with pytest.raises(dataclasses.FrozenInstanceError):
        msg.content = "no"


def test_message_assistant_with_tool_calls():
    tc = ToolCall(id="call_1", name="Read", args={"path": "foo.md"})
    msg = Message(role="assistant", content="reading", tool_calls=(tc,))
    assert msg.tool_calls == (tc,)
    # tool_calls is a tuple — cannot append
    with pytest.raises(AttributeError):
        msg.tool_calls.append(tc)


def test_tool_call_args_is_dict_not_string():
    tc = ToolCall(id="x", name="Edit", args={"old_str": "\\frac{1}{2}"})
    assert isinstance(tc.args, dict)
    assert tc.args["old_str"] == "\\frac{1}{2}"


def test_tool_def_fields():
    td = ToolDef(name="Read", description="read a file", parameters={"type": "object"})
    assert td.name == "Read"
    assert td.parameters == {"type": "object"}


def test_completion_response_with_usage():
    msg = Message(role="assistant", content="ok")
    usage = Usage(input_tokens=5, output_tokens=2)
    resp = CompletionResponse(message=msg, finish_reason="stop", usage=usage)
    assert resp.usage.input_tokens == 5


def test_completion_response_raw_excluded_from_equality():
    msg = Message(role="assistant", content="ok")
    a = CompletionResponse(message=msg, finish_reason="stop", raw={"x": 1})
    b = CompletionResponse(message=msg, finish_reason="stop", raw={"x": 2})
    assert a == b  # raw differs but is excluded from comparison


def test_stream_delta_text():
    d = StreamDelta(type="text", text="hello")
    assert d.text == "hello"


def test_stream_delta_tool_input():
    d = StreamDelta(type="tool_input", tool_call_id="call_1", arg_delta='{"path"')
    assert d.tool_call_id == "call_1"
