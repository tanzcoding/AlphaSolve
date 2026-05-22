import json
import os
import pathlib
import shutil
import sys
import threading
from contextlib import contextmanager


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import alphasolve.agents.general.general_agent as general_agent_module  # noqa: E402
from alphasolve.agents.general import (  # noqa: E402
    GeneralAgentConfig,
    GeneralPurposeAgent,
    ToolRegistry,
    ToolResult,
    Workspace,
    build_default_tool_registry,
    load_general_agent_config,
)
from alphasolve.agents.general.workspace import READ_PAGE_DEFAULT_LINES, READ_PAGE_MAX_LINES  # noqa: E402
from alphasolve.llm.types import CompletionResponse, Message, StreamDelta, ToolCall  # noqa: E402


def _resp(content: str = "", tool_calls: tuple[ToolCall, ...] = (), finish_reason: str = None, reasoning_content: str = "") -> CompletionResponse:
    return CompletionResponse(
        message=Message(role="assistant", content=content, tool_calls=tool_calls, reasoning_content=reasoning_content),
        finish_reason=finish_reason or ("tool_calls" if tool_calls else "stop"),
    )


class FakeChatClient:
    def __init__(self):
        self.calls = 0

    def complete(self, *, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return _resp(
                tool_calls=(
                    ToolCall(
                        id="call_write",
                        name="Write",
                        args={
                            "path": "propositions/prop-0.md",
                            "content": "# Proposition 0\n\n## Statement\n\nDemo.\n",
                        },
                    ),
                ),
            )
        if self.calls == 2:
            return _resp(
                tool_calls=(
                    ToolCall(
                        id="call_read",
                        name="Read",
                        args={"path": "propositions/prop-0.md"},
                    ),
                ),
            )
        return _resp("demo complete")


@contextmanager
def local_test_dir(name):
    root = pathlib.Path(__file__).resolve().parents[1]
    path = root / "_tmp_general_agent_pytest" / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        if path.exists():
            shutil.rmtree(path)


def test_general_agent_can_write_and_read_workspace_file():
    with local_test_dir("write_read") as tmp_path:
        _assert_agent_can_write_and_read_workspace_file(tmp_path)


def test_default_read_tool_defaults_to_250_lines_reports_total_and_supports_read_all():
    with local_test_dir("read_pages") as tmp_path:
        (tmp_path / "long.md").write_text(
            "".join(f"line {index}\n" for index in range(1, 266)),
            encoding="utf-8",
        )
        registry = build_default_tool_registry(Workspace(tmp_path))
        tool_def = registry.tool_defs(["Read"])[0]
        props = tool_def.parameters["properties"]

        assert props["n_lines"]["default"] == READ_PAGE_DEFAULT_LINES == 250
        assert props["n_lines"]["maximum"] == READ_PAGE_MAX_LINES
        assert "How many lines to return in this Read call" in props["n_lines"]["description"]
        assert props["read_all"]["default"] is False
        assert "ignore n_lines" in props["read_all"]["description"]

        default = registry.execute("Read", {"path": "long.md"}).content
        assert default.startswith("<system>")
        assert "250 lines read from file starting from line 1. File has 265 total lines." in default
        assert "Requested n_lines=250 reached; more lines remain." in default
        assert "   250\tline 250" in default
        assert "   251\tline 251" not in default

        explicit = registry.execute("Read", {"path": "long.md", "n_lines": 32}).content
        assert "32 lines read from file starting from line 1. File has 265 total lines." in explicit
        assert "    32\tline 32" in explicit

        full = registry.execute("Read", {"path": "long.md", "read_all": True}).content
        assert "265 lines read from file starting from line 1. File has 265 total lines." in full
        assert "End of file reached." in full
        assert "   265\tline 265" in full


def test_general_agent_emits_streaming_delta_events():
    class StreamingClient:
        def __init__(self):
            self.used_delta_sink = False

        def complete(self, *, messages, tools, delta_sink=None):
            del messages, tools
            self.used_delta_sink = delta_sink is not None
            assert delta_sink is not None
            delta_sink(StreamDelta(type="text", text="done"))
            return _resp("done", reasoning_content="think now")

    events = []
    client = StreamingClient()
    agent = GeneralPurposeAgent(
        config=GeneralAgentConfig(
            name="streaming",
            system_prompt="You stream.",
            tools=(),
            max_turns=1,
        ),
        client=client,
        tool_registry=ToolRegistry(),
        event_sink=events.append,
    )

    result = agent.run("Stream a small answer.")

    assert client.used_delta_sink
    assert result.final_answer == "done"
    assistant_deltas = [event for event in events if event["type"] == "assistant_delta"]
    assert [event["delta"] for event in assistant_deltas] == ["done"]
    assistant_final = [event for event in result.trace if event["type"] == "assistant_message"][0]
    assert assistant_final["streamed_content"] is True


def test_general_agent_resets_stream_state_on_retry_delta():
    class RetryStreamingClient:
        def complete(self, *, messages, tools, delta_sink=None):
            del messages, tools
            assert delta_sink is not None
            delta_sink(StreamDelta(type="text", text="fresh answer"))
            return _resp("fresh answer", reasoning_content="fresh reasoning")

    events = []
    agent = GeneralPurposeAgent(
        config=GeneralAgentConfig(
            name="retry-stream",
            system_prompt="You stream.",
            tools=(),
            max_turns=1,
        ),
        client=RetryStreamingClient(),
        tool_registry=ToolRegistry(),
        event_sink=events.append,
    )

    result = agent.run("Retry a stream.")

    assert result.final_answer == "fresh answer"
    assistant_deltas = [event["content"] for event in events if event["type"] == "assistant_delta"]
    assert assistant_deltas == ["fresh answer"]


def test_general_agent_normalizes_reasoning_alias_before_tool_followup():
    class AliasReasoningClient:
        def __init__(self):
            self.calls = 0
            self.second_request_messages = None

        def complete(self, *, messages, tools):
            del tools
            self.calls += 1
            if self.calls == 1:
                return _resp(
                    reasoning_content="use the edit tool",
                    tool_calls=(
                        ToolCall(id="call_side_effect", name="side_effect", args={}),
                    ),
                )
            self.second_request_messages = list(messages)
            return _resp("done")

    registry = ToolRegistry()
    registry.register(
        name="side_effect",
        description="Record a side effect.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=lambda _args: ToolResult("ok"),
    )
    client = AliasReasoningClient()
    agent = GeneralPurposeAgent(
        config=GeneralAgentConfig(
            name="alias-reasoning",
            system_prompt="Use a tool.",
            tools=("side_effect",),
            max_turns=2,
        ),
        client=client,
        tool_registry=registry,
    )

    result = agent.run("Call the tool.")

    assert result.final_answer == "done"
    assert client.second_request_messages[2].reasoning_content == "use the edit tool"


def test_general_agent_stops_before_tool_execution_when_interrupt_arrives_after_model_response():
    stop_event = threading.Event()
    tool_calls = []

    class InterruptingClient:
        def complete(self, *, messages, tools):
            del messages, tools
            stop_event.set()
            return _resp(
                tool_calls=(
                    ToolCall(id="call_side_effect", name="side_effect", args={}),
                ),
            )

    registry = ToolRegistry()
    registry.register(
        name="side_effect",
        description="Record a side effect.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=lambda _args: tool_calls.append("called") or ToolResult("done"),
    )
    agent = GeneralPurposeAgent(
        config=GeneralAgentConfig(
            name="interruptible",
            system_prompt="Use a tool.",
            tools=("side_effect",),
            max_turns=1,
        ),
        client=InterruptingClient(),
        tool_registry=registry,
        stop_event=stop_event,
    )

    result = agent.run("Call the tool.")

    assert tool_calls == []
    assert result.final_answer == ""
    assert result.trace[-1]["type"] == "run_stopped"
    assert result.trace[-1]["reason"] == "stop_event set after model response"


def _assert_agent_can_write_and_read_workspace_file(tmp_path):
    workspace = Workspace(tmp_path)
    registry = build_default_tool_registry(workspace)
    config = GeneralAgentConfig(
        name="demo",
        system_prompt="You are a demo agent.",
        tools=["Write", "Read"],
        max_turns=5,
    )
    agent = GeneralPurposeAgent(config=config, client=FakeChatClient(), tool_registry=registry)

    result = agent.run("Create a proposition file and read it back.")

    assert result.final_answer == "demo complete"
    assert (tmp_path / "propositions" / "prop-0.md").read_text(encoding="utf-8").startswith("# Proposition 0")
    assert result.turns == 3
    assert result.trace[0]["type"] == "run_start"
    assert result.trace[-1]["type"] == "run_finish"
    assert result.trace[-1]["final_answer"] == "demo complete"
    tool_calls = [item for item in result.trace if item["type"] == "tool_call"]
    tool_results = [item for item in result.trace if item["type"] == "tool_result"]
    assert [item["name"] for item in tool_calls] == ["Write", "Read"]
    assert len(tool_results) == 2
    assert not any(item["is_error"] for item in tool_results)


def test_workspace_blocks_path_escape():
    with local_test_dir("path_escape") as tmp_path:
        _assert_workspace_blocks_path_escape(tmp_path)


def _assert_workspace_blocks_path_escape(tmp_path):
    workspace = Workspace(tmp_path)
    try:
        workspace.read_text_page("../outside.txt")
    except Exception as exc:
        assert "escapes workspace" in str(exc)
    else:
        raise AssertionError("workspace path escape should fail")


def test_load_general_agent_config():
    with local_test_dir("config") as tmp_path:
        _assert_load_general_agent_config(tmp_path)


def test_load_general_agent_config_appends_skill_markdown():
    with local_test_dir("config_skill") as tmp_path:
        skill_dir = tmp_path / "skills" / "math_review"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\ndescription: Review proofs.\n---\n\nCheck hidden assumptions carefully.",
            encoding="utf-8",
        )
        config_path = tmp_path / "agent.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "version: 1",
                    "agent:",
                    "  name: demo",
                    "  system_prompt: Base prompt",
                    "  skills:",
                    "    - math_review",
                ]
            ),
            encoding="utf-8",
        )

        config = load_general_agent_config(config_path)

        assert config.skills == ("math_review",)
        assert "Base prompt" in config.system_prompt
        assert "# Skills" in config.system_prompt
        assert "## math_review" in config.system_prompt
        assert "Check hidden assumptions carefully." in config.system_prompt


def _assert_load_general_agent_config(tmp_path):
    config_path = tmp_path / "agent.json"
    config_path.write_text(
        json.dumps(
            {
                "name": "demo",
                "system_prompt": "Demo prompt",
                "tools": ["Read"],
                "skills": ["math_review"],
                "max_turns": 7,
            }
        ),
        encoding="utf-8",
    )

    config = load_general_agent_config(config_path)

    assert config.name == "demo"
    assert config.tools == ("Read",)
    assert config.skills == ("math_review",)
    assert config.max_turns == 7


def test_load_general_agent_config_supports_extend_and_exclude_tools():
    with local_test_dir("extend_config") as tmp_path:
        prompt = tmp_path / "base.md"
        prompt.write_text("Base ${ROLE}", encoding="utf-8")
        base = tmp_path / "base.yaml"
        base.write_text(
            "\n".join(
                [
                    "version: 1",
                    "agent:",
                    "  name: base",
                    "  system_prompt_path: ./base.md",
                    "  system_prompt_args:",
                    "    ROLE: agent",
                    "  role: base_role",
                    "  max_turns: 9",
                    "  tools:",
                    "    - Read",
                    "    - Write",
                    "    - Agent",
                    "  tool_parameters:",
                    "    Agent:",
                    "      type:",
                    "        enum:",
                    "          - reasoning_subagent",
                    "          - compute_subagent",
                ]
            ),
            encoding="utf-8",
        )
        child = tmp_path / "child.yaml"
        child.write_text(
            "\n".join(
                [
                    "version: 1",
                    "agent:",
                    "  extend: ./base.yaml",
                    "  name: child",
                    "  system_prompt_args:",
                    "    ROLE: child",
                    "  exclude_tools:",
                    "    - Write",
                    "  tool_parameters:",
                    "    Agent:",
                    "      type:",
                    "        enum:",
                    "          - reasoning_subagent",
                ]
            ),
            encoding="utf-8",
        )

        config = load_general_agent_config(child)

        assert config.name == "child"
        assert config.system_prompt == "Base child"
        assert config.role == "base_role"
        assert config.max_turns == 9
        assert config.tools == ("Read", "Agent")
        assert config.tool_parameters["Agent"]["type"]["enum"] == ["reasoning_subagent"]


def test_general_agent_enforces_enabled_tools_and_parameter_constraints():
    class InvalidToolClient:
        def __init__(self):
            self.calls = 0

        def complete(self, *, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return _resp(
                    tool_calls=(
                        ToolCall(
                            id="blocked_tool",
                            name="Write",
                            args={"path": "blocked.md", "content": "no"},
                        ),
                    ),
                )
            if self.calls == 2:
                return _resp(
                    tool_calls=(
                        ToolCall(
                            id="bad_arg",
                            name="Read",
                            args={"path": "forbidden.md"},
                        ),
                    ),
                )
            return _resp("done")

    with local_test_dir("tool_constraints") as tmp_path:
        workspace = Workspace(tmp_path)
        registry = build_default_tool_registry(workspace)
        config = GeneralAgentConfig(
            name="guarded",
            system_prompt="You are a guarded agent.",
            tools=("Read",),
            tool_parameters={"Read": {"path": {"enum": ["allowed.md"]}}},
            max_turns=5,
        )
        agent = GeneralPurposeAgent(config=config, client=InvalidToolClient(), tool_registry=registry)

        result = agent.run("Try invalid tools and arguments.")

        tool_results = [item for item in result.trace if item["type"] == "tool_result"]
        assert result.final_answer == "done"
        assert tool_results[0]["is_error"]
        assert "tool is not enabled" in tool_results[0]["content"]
        assert tool_results[1]["is_error"]
        assert "must be one of" in tool_results[1]["content"]


def test_tool_parameter_constraints_apply_runtime_defaults():
    registry = ToolRegistry()

    registry.register(
        name="show_path",
        description="Return the path argument.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
            },
            "required": [],
        },
        handler=lambda args: ToolResult(str(args["path"])),
    )

    constraints = {"show_path": {"path": {"default": "knowledge", "const": "knowledge"}}}

    ok = registry.execute("show_path", {}, enabled=["show_path"], tool_parameters=constraints)
    blocked = registry.execute("show_path", {"path": "."}, enabled=["show_path"], tool_parameters=constraints)

    assert ok.content == "knowledge"
    assert not ok.is_error
    assert blocked.is_error
    assert "must be 'knowledge'" in blocked.content


def _run_as_script():
    root = pathlib.Path(__file__).resolve().parents[1]
    tmp_root = root / "_tmp_general_agent_test"
    if tmp_root.exists():
        shutil.rmtree(tmp_root)
    (tmp_root / "a").mkdir(parents=True)
    (tmp_root / "b").mkdir(parents=True)
    (tmp_root / "c").mkdir(parents=True)
    try:
        _assert_agent_can_write_and_read_workspace_file(tmp_root / "a")
        _assert_workspace_blocks_path_escape(tmp_root / "b")
        _assert_load_general_agent_config(tmp_root / "c")
        print("general agent demo ok")
    finally:
        if tmp_root.exists():
            shutil.rmtree(tmp_root)


if __name__ == "__main__":
    _run_as_script()
