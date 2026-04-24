import json
import os
import pathlib
import shutil
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alphasolve.agents.general import (  # noqa: E402
    GeneralAgentConfig,
    GeneralPurposeAgent,
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
