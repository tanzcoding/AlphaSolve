import json
import os
import pathlib
import shutil
import sys
from contextlib import contextmanager

import httpx

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import alphasolve.agents.general.general_agent as general_agent_module  # noqa: E402
from alphasolve.agents.general import (  # noqa: E402
    GeneralAgentConfig,
    GeneralPurposeAgent,
    OpenAIChatClient,
    ToolRegistry,
    ToolResult,
    Workspace,
    build_default_tool_registry,
    load_general_agent_config,
)


class FakeChatClient:
    def __init__(self):
        self.calls = 0

    def complete(self, *, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_write",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps(
                                {
                                    "path": "lemmas/lemma-0.md",
                                    "content": "# Lemma 0\n\n## Statement\n\nDemo.\n",
                                }
                            ),
                        },
                    }
                ],
            }
        if self.calls == 2:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_read",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "lemmas/lemma-0.md"}),
                        },
                    }
                ],
            }
        return {"role": "assistant", "content": "demo complete"}


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


def test_general_agent_emits_streaming_delta_events():
    class StreamingClient:
        def __init__(self):
            self.used_delta_sink = False

        def complete(self, *, messages, tools, delta_sink=None):
            del messages, tools
            self.used_delta_sink = delta_sink is not None
            assert delta_sink is not None
            delta_sink({"type": "reasoning", "content": "think "})
            delta_sink({"type": "reasoning", "content": "now"})
            delta_sink({"type": "content", "content": "done"})
            return {
                "role": "assistant",
                "reasoning_content": "think now",
                "content": "done",
            }

    events = []
    client = StreamingClient()
    agent = GeneralPurposeAgent(
        config=GeneralAgentConfig(
            name="streaming",
            system_prompt="You stream.",
            tools=[],
            max_turns=1,
        ),
        client=client,
        tool_registry=ToolRegistry(),
        event_sink=events.append,
    )

    result = agent.run("Stream a small answer.")

    assert client.used_delta_sink
    assert result.final_answer == "done"
    thinking_deltas = [event for event in events if event["type"] == "thinking_delta"]
    assistant_deltas = [event for event in events if event["type"] == "assistant_delta"]
    assert [event["content"] for event in thinking_deltas] == ["think ", "think now"]
    assert [event["delta"] for event in assistant_deltas] == ["done"]
    thinking_final = [event for event in result.trace if event["type"] == "thinking"][0]
    assistant_final = [event for event in result.trace if event["type"] == "assistant_message"][0]
    assert thinking_final["streamed"] is True
    assert assistant_final["streamed_content"] is True


def test_general_agent_resets_stream_state_on_retry_delta():
    class RetryStreamingClient:
        def complete(self, *, messages, tools, delta_sink=None):
            del messages, tools
            assert delta_sink is not None
            delta_sink({"type": "reasoning", "content": "stale reasoning"})
            delta_sink({"type": "content", "content": "stale answer"})
            delta_sink(
                {
                    "type": "retry",
                    "attempt": 1,
                    "error_type": "RemoteProtocolError",
                    "error": "peer closed connection",
                }
            )
            delta_sink({"type": "reasoning", "content": "fresh reasoning"})
            delta_sink({"type": "content", "content": "fresh answer"})
            return {
                "role": "assistant",
                "reasoning_content": "fresh reasoning",
                "content": "fresh answer",
            }

    events = []
    agent = GeneralPurposeAgent(
        config=GeneralAgentConfig(
            name="retry-stream",
            system_prompt="You stream.",
            tools=[],
            max_turns=1,
        ),
        client=RetryStreamingClient(),
        tool_registry=ToolRegistry(),
        event_sink=events.append,
    )

    result = agent.run("Retry a stream.")

    assert result.final_answer == "fresh answer"
    retry_events = [event for event in events if event["type"] == "model_retry"]
    assert len(retry_events) == 1
    assert retry_events[0]["reasoning_chars"] == len("stale reasoning")
    assert retry_events[0]["content_chars"] == len("stale answer")
    thinking_deltas = [event["content"] for event in events if event["type"] == "thinking_delta"]
    assistant_deltas = [event["content"] for event in events if event["type"] == "assistant_delta"]
    assert thinking_deltas == ["stale reasoning", "fresh reasoning"]
    assert assistant_deltas == ["stale answer", "fresh answer"]


def test_openai_chat_client_reconstructs_streaming_tool_calls():
    class FakeCompletions:
        def __init__(self):
            self.requests = []

        def create(self, **request):
            self.requests.append(request)
            return [
                {"choices": [{"delta": {"role": "assistant", "reasoning_content": "plan "}}]},
                {"choices": [{"delta": {"reasoning_content": "tool"}}]},
                {"choices": [{"delta": {"content": "Preparing."}}]},
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_write",
                                        "type": "function",
                                        "function": {"name": "write_", "arguments": "{\"path\":"},
                                    }
                                ]
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {"name": "file", "arguments": "\"lemma.md\"}"},
                                    }
                                ]
                            }
                        }
                    ]
                },
            ]

    class FakeOpenAI:
        def __init__(self):
            self.chat = type("Chat", (), {"completions": FakeCompletions()})()

    fake_openai = FakeOpenAI()
    client = OpenAIChatClient({"api_key": "test", "model": "fake-model"})
    client.client = fake_openai
    deltas = []

    message = client.complete(messages=[], tools=[{"type": "function"}], delta_sink=deltas.append)

    request = fake_openai.chat.completions.requests[0]
    assert request["stream"] is True
    assert message["role"] == "assistant"
    assert message["reasoning_content"] == "plan tool"
    assert message["content"] == "Preparing."
    assert message["tool_calls"][0]["id"] == "call_write"
    assert message["tool_calls"][0]["function"]["name"] == "write_file"
    assert message["tool_calls"][0]["function"]["arguments"] == '{"path":"lemma.md"}'
    assert deltas == [
        {"type": "reasoning", "content": "plan "},
        {"type": "reasoning", "content": "tool"},
        {"type": "content", "content": "Preparing."},
    ]


def test_openai_chat_client_retries_remote_protocol_error_before_first_delta():
    class FlakyCompletions:
        def __init__(self):
            self.calls = 0

        def create(self, **request):
            del request
            self.calls += 1
            if self.calls == 1:
                raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")
            return [{"choices": [{"delta": {"content": "ok"}}]}]

    class FakeOpenAI:
        def __init__(self):
            self.chat = type("Chat", (), {"completions": FlakyCompletions()})()

    fake_openai = FakeOpenAI()
    client = OpenAIChatClient({"api_key": "test", "model": "fake-model"})
    client.client = fake_openai
    old_sleep = general_agent_module.time.sleep
    general_agent_module.time.sleep = lambda _seconds: None
    try:
        message = client.complete(messages=[], tools=[], delta_sink=lambda _delta: None)
    finally:
        general_agent_module.time.sleep = old_sleep

    assert fake_openai.chat.completions.calls == 2
    assert message["content"] == "ok"


def test_openai_chat_client_retries_remote_protocol_error_after_delta_with_reset():
    class FlakyCompletions:
        def __init__(self):
            self.calls = 0

        def create(self, **request):
            del request
            self.calls += 1
            if self.calls == 1:
                def broken_stream():
                    yield {"choices": [{"delta": {"reasoning_content": "stale"}}]}
                    raise httpx.RemoteProtocolError(
                        "peer closed connection without sending complete message body"
                    )

                return broken_stream()
            return [{"choices": [{"delta": {"reasoning_content": "fresh", "content": "ok"}}]}]

    class FakeOpenAI:
        def __init__(self):
            self.chat = type("Chat", (), {"completions": FlakyCompletions()})()

    fake_openai = FakeOpenAI()
    client = OpenAIChatClient({"api_key": "test", "model": "fake-model"})
    client.client = fake_openai
    old_sleep = general_agent_module.time.sleep
    general_agent_module.time.sleep = lambda _seconds: None
    deltas = []
    try:
        message = client.complete(messages=[], tools=[], delta_sink=deltas.append)
    finally:
        general_agent_module.time.sleep = old_sleep

    assert fake_openai.chat.completions.calls == 2
    assert message["reasoning_content"] == "fresh"
    assert message["content"] == "ok"
    assert [delta["type"] for delta in deltas] == ["reasoning", "retry", "reasoning", "content"]
    assert deltas[1]["error_type"] == "RemoteProtocolError"


def _assert_agent_can_write_and_read_workspace_file(tmp_path):
    workspace = Workspace(tmp_path)
    registry = build_default_tool_registry(workspace)
    config = GeneralAgentConfig(
        name="demo",
        system_prompt="You are a demo agent.",
        tools=["write_file", "read_file"],
        max_turns=5,
    )
    agent = GeneralPurposeAgent(config=config, client=FakeChatClient(), tool_registry=registry)

    result = agent.run("Create a lemma file and read it back.")

    assert result.final_answer == "demo complete"
    assert (tmp_path / "lemmas" / "lemma-0.md").read_text(encoding="utf-8").startswith("# Lemma 0")
    assert result.turns == 3
    assert result.trace[0]["type"] == "run_start"
    assert result.trace[-1]["type"] == "run_finish"
    assert result.trace[-1]["final_answer"] == "demo complete"
    tool_calls = [item for item in result.trace if item["type"] == "tool_call"]
    tool_results = [item for item in result.trace if item["type"] == "tool_result"]
    assert [item["name"] for item in tool_calls] == ["write_file", "read_file"]
    assert len(tool_results) == 2
    assert not any(item["is_error"] for item in tool_results)


def test_workspace_blocks_path_escape():
    with local_test_dir("path_escape") as tmp_path:
        _assert_workspace_blocks_path_escape(tmp_path)


def _assert_workspace_blocks_path_escape(tmp_path):
    workspace = Workspace(tmp_path)
    try:
        workspace.read_text("../outside.txt")
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

        assert config.skills == ["math_review"]
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
                "tools": ["read_file"],
                "skills": ["math_review"],
                "max_turns": 7,
            }
        ),
        encoding="utf-8",
    )

    config = load_general_agent_config(config_path)

    assert config.name == "demo"
    assert config.tools == ["read_file"]
    assert config.skills == ["math_review"]
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
                    "  model_config: BASE_MODEL",
                    "  max_turns: 9",
                    "  tools:",
                    "    - read_file",
                    "    - write_file",
                    "    - agent",
                    "  tool_parameters:",
                    "    agent:",
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
                    "    - write_file",
                    "  tool_parameters:",
                    "    agent:",
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
        assert config.model_config == "BASE_MODEL"
        assert config.max_turns == 9
        assert config.tools == ["read_file", "agent"]
        assert config.tool_parameters["agent"]["type"]["enum"] == ["reasoning_subagent"]


def test_general_agent_enforces_enabled_tools_and_parameter_constraints():
    class InvalidToolClient:
        def __init__(self):
            self.calls = 0

        def complete(self, *, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "blocked_tool",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps({"path": "blocked.md", "content": "no"}),
                            },
                        }
                    ],
                }
            if self.calls == 2:
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "bad_arg",
                            "type": "function",
                            "function": {
                                "name": "list_dir",
                                "arguments": json.dumps({"path": "forbidden"}),
                            },
                        }
                    ],
                }
            return {"role": "assistant", "content": "done"}

    with local_test_dir("tool_constraints") as tmp_path:
        workspace = Workspace(tmp_path)
        registry = build_default_tool_registry(workspace)
        config = GeneralAgentConfig(
            name="guarded",
            system_prompt="You are a guarded agent.",
            tools=["list_dir"],
            tool_parameters={"list_dir": {"path": {"enum": ["."]}}},
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
