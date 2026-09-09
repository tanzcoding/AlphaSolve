from __future__ import annotations

import pytest
import dataclasses

from alphasolve.llm.types import (
    Message, ToolCall, ToolDef, Usage,
)


def test_message_is_frozen():
    msg = Message(role="user", content="hi")
    with pytest.raises(dataclasses.FrozenInstanceError):
        msg.content = "no"


def test_message_assistant_with_tool_calls():
    tc = ToolCall(id="call_1", name="Read", args={"path": "foo.md"})
    msg = Message(role="assistant", content="reading", tool_calls=(tc,))
    assert msg.tool_calls == (tc,)
    # 工具调用列表保留为不可变元组。
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


def test_usage_preserves_cached_tokens_separately():
    usage = Usage(input_tokens=5, output_tokens=2, cached_tokens=3)
    assert usage.input_tokens == 5
    assert usage.output_tokens == 2
    assert usage.cached_tokens == 3


def test_visible_reasoning_is_optional_trace_data():
    assert Message(role="assistant", content="done").reasoning_content == ""
    message = Message(role="assistant", content="done", reasoning_content="检查了边界情况。")
    assert message.reasoning_content == "检查了边界情况。"
