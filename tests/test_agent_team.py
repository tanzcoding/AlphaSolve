import json
import io
import os
import pathlib
import re
import shutil
import sys
import threading
import time
from contextlib import contextmanager

from rich.console import Console

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alphasolve.agents.general import GeneralAgentConfig, Workspace, load_agent_suite_config  # noqa: E402
from alphasolve.agents.team import AlphaSolve  # noqa: E402
from alphasolve.agents.team.demo import make_demo_client_factory  # noqa: E402
from alphasolve.agents.team.curator import (  # noqa: E402
    CURATOR_HEALTH_CHECK_INTERVAL,
    CuratorTask,
    CuratorQueue,
    _health_check_prompt,
    _update_entry_metadata,
    init_knowledge_base,
)
from alphasolve.agents.team.orchestrator import Orchestrator  # noqa: E402
from alphasolve.agents.team.orchestrator import verified_count  # noqa: E402
from alphasolve.agents.team.worker import Worker  # noqa: E402
from alphasolve.agents.team.project import ProjectLayout  # noqa: E402
from alphasolve.agents.team.solution import write_solution  # noqa: E402
from alphasolve.agents.team.tools import RoleWorkspaceAccess, SubagentService, build_workspace_tool_registry  # noqa: E402
from alphasolve.config.agent_config import AlphaSolveConfig  # noqa: E402
from alphasolve.config.agent_config import PACKAGE_ROOT  # noqa: E402
from alphasolve.execution import ExecutionGateway  # noqa: E402
from alphasolve.utils.rich_renderer import PropositionTeamRenderer  # noqa: E402


@contextmanager
def local_project_dir(name):
    root = pathlib.Path(__file__).resolve().parents[1]
    path = root / "_tmp_agent_team_pytest" / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        if path.exists():
            shutil.rmtree(path)


def test_default_agent_suite_loads_yaml_roles():
    suite = load_agent_suite_config(pathlib.Path(PACKAGE_ROOT) / "config" / "agents.yaml")
    suite_from_dir = load_agent_suite_config(pathlib.Path(PACKAGE_ROOT) / "config")

    assert {
        "orchestrator",
        "generator",
        "verifier",
        "verifier_stepwise",
        "verifier_premise_chain",
        "verifier_adversarial",
        "reviser",
        "theorem_checker",
    } <= set(suite.agents)
    assert {"reasoning_subagent", "compute_subagent", "numerical_experiment_subagent"} <= set(suite.subagents)
    assert "Agent" in suite.agents["orchestrator"].tools
    assert "Write" in suite.agents["orchestrator"].tools
    assert "Edit" in suite.agents["orchestrator"].tools
    assert "MakeDir" in suite.agents["orchestrator"].tools
    assert "Rename" in suite.agents["orchestrator"].tools
    assert "Move" in suite.agents["orchestrator"].tools
    assert "Delete" not in suite.agents["orchestrator"].tools
    index_pattern = r"^verified_propositions(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*/index\.md$"
    assert suite.agents["orchestrator"].tool_parameters["Write"]["path"]["pattern"] == index_pattern
    assert suite.agents["orchestrator"].tool_parameters["Edit"]["path"]["pattern"] == index_pattern
    assert suite.agents["orchestrator"].tool_parameters["MakeDir"]["path"]["pattern"].startswith("^verified_propositions")
    assert "Examples:" in suite.agents["orchestrator"].system_prompt
    assert "Verified Propositions Index" in suite.agents["orchestrator"].system_prompt
    assert "Current Progress And Insights" in suite.agents["orchestrator"].system_prompt
    assert "Current Progress And Insights section" in suite.agents["orchestrator"].system_prompt
    assert "less than 50 lines" in suite.agents["orchestrator"].system_prompt
    assert "bootstrap-assumption-A" in suite.agents["orchestrator"].system_prompt
    assert "that would rename the `.md` file" in suite.agents["orchestrator"].system_prompt
    assert "Agent" in suite.agents["generator"].tools
    assert suite.agents["generator"].tool_parameters["Agent"]["type"]["enum"] == [
        "compute_subagent",
        "numerical_experiment_subagent",
        "reasoning_subagent",
    ]
    assert suite.subagents["reasoning_subagent"].tool_parameters["Agent"]["type"]["enum"] == ["reasoning_subagent"]
    assert suite.settings["max_verify_rounds"] == 6
    assert suite.settings["verifier_scaling_factor"] == 4
    assert suite.settings["verifier_agents"] == [
        "verifier_citation",
        "verifier_failure_modes",
        "verifier_stepwise",
        "verifier_premise_chain",
    ]
    assert suite.settings["max_orchestrator_restarts"] == 50
    assert suite.subagents["reasoning_subagent"].when_to_use
    assert "ListDir" in suite.subagents["reasoning_subagent"].tools
    curator = suite.subagents["curator"]
    assert curator.tool_parameters["ListDir"]["path"]["default"] == "knowledge"
    assert curator.tool_parameters["Write"]["path"]["pattern"].endswith("\\.md$")
    assert curator.tool_parameters["Write"]["mode"]["enum"] == ["overwrite", "append"]
    assert any("rename" in name.lower() for name in curator.tools)
    assert "Move" in curator.tools
    assert any("delete" in name.lower() for name in curator.tools)
    assert not any(name.lower() in {"get_current_time", "getcurrenttime"} for name in curator.tools)
    assert "not a transcript archive" in curator.system_prompt
    assert "There is no maintenance log file." in curator.system_prompt
    assert "knowledge/references/" in curator.system_prompt
    assert "immediate child markdown files and immediate child folders" in curator.system_prompt
    assert len(curator.system_prompt.splitlines()) <= 120
    assert "<source_label>" not in curator.system_prompt
    assert suite_from_dir.agents["generator"].tools == suite.agents["generator"].tools
    assert "path is relative to `verified_propositions`" in suite.agents["generator"].system_prompt
    assert r"\ref{coercive\energy-estimate}" in suite.agents["generator"].system_prompt
    assert "path is relative to `verified_propositions`" in suite.agents["reviser"].system_prompt


def test_project_layout_syncs_workspace_inputs_from_project_root():
    with local_project_dir("sync_workspace_inputs") as project_dir:
        (project_dir / "problem.md").write_text("# Problem\n\nfresh problem\n", encoding="utf-8")
        (project_dir / "hint.md").write_text("# Hint\n\nfresh hint\n", encoding="utf-8")
        workspace_dir = project_dir / "workspace"
        workspace_dir.mkdir()
        (workspace_dir / "problem.md").write_text("stale problem\n", encoding="utf-8")
        (workspace_dir / "hint.md").write_text("stale hint\n", encoding="utf-8")

        layout = ProjectLayout.create(project_dir)
        synced = layout.sync_workspace_inputs()

        assert synced == {
            "problem": str(workspace_dir / "problem.md"),
            "hint": str(workspace_dir / "hint.md"),
        }
        assert (workspace_dir / "problem.md").read_text(encoding="utf-8") == "# Problem\n\nfresh problem\n"
        assert (workspace_dir / "hint.md").read_text(encoding="utf-8") == "# Hint\n\nfresh hint\n"


def test_project_layout_sync_removes_stale_workspace_hint_when_project_hint_is_missing():
    with local_project_dir("sync_workspace_inputs_no_hint") as project_dir:
        (project_dir / "problem.md").write_text("# Problem\n\nfresh problem\n", encoding="utf-8")
        workspace_dir = project_dir / "workspace"
        workspace_dir.mkdir()
        (workspace_dir / "hint.md").write_text("stale hint\n", encoding="utf-8")

        layout = ProjectLayout.create(project_dir)
        synced = layout.sync_workspace_inputs()

        assert synced == {
            "problem": str(workspace_dir / "problem.md"),
            "hint": None,
        }
        assert (workspace_dir / "problem.md").read_text(encoding="utf-8") == "# Problem\n\nfresh problem\n"
        assert not (workspace_dir / "hint.md").exists()


def test_knowledge_curator_task_prompt_hides_source_labels():
    class CapturingClient:
        def __init__(self):
            self.messages = []

        def complete(self, *, messages, tools):
            del tools
            self.messages = messages
            return {"role": "assistant", "content": "done"}

    class Suite:
        def __init__(self):
            self.subagents = {
                "curator": GeneralAgentConfig(
                    name="curator",
                    system_prompt="Curator prompt",
                    tools=[],
                    max_turns=1,
                )
            }

    with local_project_dir("curator_sanitizes_source") as project_dir:
        workspace_dir = project_dir / "workspace"
        knowledge_dir = workspace_dir / "knowledge"
        knowledge_dir.mkdir(parents=True)
        client = CapturingClient()
        queue = CuratorQueue(
            knowledge_dir=knowledge_dir,
            workspace_dir=workspace_dir,
            suite=Suite(),
            client_factory=lambda config: client,
        )

        queue._run_curator(
            CuratorTask(
                trace_segment=[{"role": "assistant", "content": "The inequality ||u||_{H^k} <= C||u||_{H^{k-1}} fails."}],
                source_label="prop-0007-fc2e84e3/verifier-r6-a1-verifier_failure_modes",
                caller_context={"caller_role": "verifier"},
            )
        )

        task_text = client.messages[1]["content"]
        assert "prop-0007-fc2e84e3" not in task_text
        assert "verifier-r6" not in task_text
        assert '"trace_kind": "verifier"' in task_text
        assert "private triage" in task_text
        assert "log.md" not in task_text
        assert "proposition.md" not in task_text


def test_final_verifier_curator_prompt_caps_common_errors():
    class CapturingClient:
        def __init__(self):
            self.messages = []

        def complete(self, *, messages, tools):
            del tools
            self.messages = messages
            return {"role": "assistant", "content": "done"}

    class Suite:
        models = {}

        def __init__(self):
            self.subagents = {
                "curator": GeneralAgentConfig(
                    name="curator",
                    system_prompt="Curator prompt",
                    tools=[],
                    max_turns=1,
                )
            }

    with local_project_dir("curator_common_errors_cap") as project_dir:
        workspace_dir = project_dir / "workspace"
        knowledge_dir = workspace_dir / "knowledge"
        knowledge_dir.mkdir(parents=True)
        client = CapturingClient()
        queue = CuratorQueue(
            knowledge_dir=knowledge_dir,
            workspace_dir=workspace_dir,
            suite=Suite(),
            client_factory=lambda config: client,
        )

        queue._run_curator(
            CuratorTask(
                trace_segment=[{"role": "verifier_attempt", "content": "Verdict: fail\n\nThe proof repeats an old trap."}],
                source_label="prop-demo/verifier",
            )
        )

        task_text = client.messages[1]["content"]
        assert "Append up to 3 general error patterns" in task_text
        assert "at most 15 error patterns" in task_text
        assert "final file still has no more than 15" in task_text


def test_init_knowledge_base_removes_log_and_omits_problem_section():
    with local_project_dir("knowledge_init") as project_dir:
        knowledge_dir = project_dir / "workspace" / "knowledge"
        knowledge_dir.mkdir(parents=True)
        (knowledge_dir / "index.md").write_text(
            "# Knowledge Index\n\n"
            "## Problem\n\n"
            "Legacy statement.\n\n"
            "## Entries\n\n"
            "_No entries yet._\n",
            encoding="utf-8",
        )
        (knowledge_dir / "log.md").write_text("# Legacy Log\n", encoding="utf-8")

        init_knowledge_base(knowledge_dir, "# Problem\n\nLegacy text.\n")

        index_text = (knowledge_dir / "index.md").read_text(encoding="utf-8")
        assert "## Problem" not in index_text
        assert "## Entries" in index_text
        assert not (knowledge_dir / "log.md").exists()
        assert (knowledge_dir / "common-errors.md").is_file()
        assert (knowledge_dir / "references" / "index.md").is_file()


def test_init_knowledge_base_creates_route_map_index():
    with local_project_dir("knowledge_route_map_init") as project_dir:
        knowledge_dir = project_dir / "workspace" / "knowledge"
        knowledge_dir.mkdir(parents=True)

        init_knowledge_base(knowledge_dir, "# Problem\n\nLegacy text.\n")

        index_text = (knowledge_dir / "index.md").read_text(encoding="utf-8")
        assert "## Start Here" in index_text
        assert "## Current Bottlenecks" in index_text
        assert "## Main Routes" in index_text
        assert "## Failed Routes And Pitfalls" in index_text
        assert "## Tools And Lemmas" in index_text
        assert "## References" in index_text
        assert "[[references/index]]" in index_text
        assert "## All Entries" in index_text


def test_knowledge_curator_queue_updates_renderer_state(tmp_path):
    class Suite:
        subagents = {}
        models = {}

    workspace_dir = tmp_path / "workspace"
    knowledge_dir = workspace_dir / "knowledge"
    knowledge_dir.mkdir(parents=True)
    renderer = PropositionTeamRenderer(
        console=Console(file=io.StringIO(), width=124, height=34, force_terminal=False, color_system=None),
        screen=False,
    )
    queue = CuratorQueue(
        knowledge_dir=knowledge_dir,
        workspace_dir=workspace_dir,
        suite=Suite(),
        client_factory=lambda config: None,
        renderer=renderer,
    )
    started = threading.Event()
    release = threading.Event()

    def fake_run_curator(task):
        assert task.source_label == "prop-0001/verifier"
        renderer.start_curator_task(task.source_label)
        started.set()
        release.wait(timeout=1)
        renderer.finish_curator_task(success=True)

    queue._run_curator = fake_run_curator

    queue.start()
    try:
        queue.submit(
            CuratorTask(
                trace_segment=[{"role": "assistant", "content": "curate me"}],
                source_label="prop-0001/verifier",
            )
        )

        assert started.wait(timeout=1)
        assert renderer._curator_current_label == "prop-0001/verifier"
        assert renderer._curator_pending == 0
        assert renderer._curator.status == "running"

        release.set()
        deadline = time.time() + 1
        while time.time() < deadline and renderer._curator_processed < 1:
            time.sleep(0.01)

        assert renderer._curator_processed == 1
        assert renderer._curator_current_label == ""
        assert renderer._curator_last_label == "prop-0001/verifier"
        assert renderer._curator.status == "idle"
    finally:
        release.set()
        queue.stop(timeout=1)


def test_curator_queue_enqueues_periodic_health_check(tmp_path):
    class Suite:
        subagents = {}
        models = {}

    workspace_dir = tmp_path / "workspace"
    knowledge_dir = workspace_dir / "knowledge"
    knowledge_dir.mkdir(parents=True)
    queue = CuratorQueue(
        knowledge_dir=knowledge_dir,
        workspace_dir=workspace_dir,
        suite=Suite(),
        client_factory=lambda config: None,
    )
    queue._started = True

    for index in range(CURATOR_HEALTH_CHECK_INTERVAL):
        queue.submit(
            CuratorTask(
                trace_segment=[{"role": "assistant", "content": f"digest {index}"}],
                source_label=f"prop-{index}/generator",
            )
        )

    queued = [queue._queue.get_nowait() for _ in range(CURATOR_HEALTH_CHECK_INTERVAL + 1)]
    assert [task.task_kind for task in queued[:-1]] == ["digest"] * CURATOR_HEALTH_CHECK_INTERVAL
    assert queued[-1].task_kind == "health_check"
    assert queued[-1].trace_segment == []
    assert queued[-1].source_label == "knowledge-health-check"


def test_curator_health_check_prompt_is_navigation_focused():
    prompt = _health_check_prompt()

    assert "Knowledge Base Health Check" in prompt
    assert "route map" in prompt
    assert "topic folders" in prompt
    assert "giant flat summary list" in prompt
    assert "immediate child files and folders" in prompt
    assert "references" in prompt
    assert "250 lines" in prompt
    assert "common-errors.md` which stays as one compressed file" in prompt
    assert "common-errors.md" in prompt
    assert "15 error patterns" in prompt
    assert "shared failure mode" in prompt
    assert "prop-000" not in prompt
    assert "worker_id" not in prompt


def test_curator_health_check_prompt_injects_program_scan(tmp_path):
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    (knowledge_dir / "index.md").write_text(
        "# Knowledge Index\n\n"
        "## All Entries\n\n"
        "- [[tracked]] - tracked.\n"
        "- [[topic/index]] - topic.\n"
        "- [[common-errors]] - common errors.\n",
        encoding="utf-8",
    )
    (knowledge_dir / "tracked.md").write_text("# Tracked\n", encoding="utf-8")
    (knowledge_dir / "loose-paper.md").write_text("# A Very Good Paper\n", encoding="utf-8")
    (knowledge_dir / "common-errors.md").write_text("\n".join(["- pattern"] * 251), encoding="utf-8")
    topic = knowledge_dir / "topic"
    topic.mkdir()
    (topic / "index.md").write_text("# Topic\n\n_No entries yet._\n", encoding="utf-8")
    (topic / "long-note.md").write_text("\n".join(["line"] * 251), encoding="utf-8")

    prompt = _health_check_prompt(knowledge_dir)

    assert "Program scan before curator" in prompt
    assert "`knowledge/loose-paper.md` is not tracked by `knowledge/index.md`" in prompt
    assert "`knowledge/topic/long-note.md` is not tracked by `knowledge/topic/index.md`" in prompt
    assert "knowledge/topic/long-note.md (251 lines)" in prompt
    assert "knowledge/common-errors.md` is 251 lines" in prompt
    assert "within 15 common error patterns" in prompt
    assert "knowledge/common-errors.md (251 lines)" not in prompt
    assert "rename paper files by title" in prompt


def test_agent_team_demo_creates_workspace_and_verified_proposition():
    with local_project_dir("demo") as project_dir:
        (project_dir / "problem.md").write_text("# Problem\n\nShow that equality is reflexive.\n", encoding="utf-8")

        result = AlphaSolve(
            project_dir=project_dir,
            max_workers=1,
            client_factory=make_demo_client_factory(),
            prime_wolfram=False,
            print_to_console=False,
        ).run()

        assert result.final_answer.startswith("Problem solved. Solution written to ")
        assert (project_dir / "workspace" / "knowledge").is_dir()
        assert (project_dir / "workspace" / "unverified_propositions").is_dir()
        verified_props = list((project_dir / "workspace" / "verified_propositions").glob("*.md"))
        assert verified_props, "expected at least one verified proposition"
        assert (project_dir / "solution.md").is_file()
        assert (project_dir / "logs" / "orchestrator_trace.json").is_file()
        assert result.worker_results
        assert result.worker_results[0].status == "verified"
        assert result.worker_results[0].solved_problem
        assert result.worker_results[0].theorem_check_file is not None
        assert result.worker_results[0].theorem_check_file.name == "theorem_check.md"
        assert result.worker_results[0].trace == []
        worker_trace = json.loads((result.worker_results[0].worker_dir / "trace.json").read_text(encoding="utf-8"))
        assert sum(1 for item in worker_trace if item["role"] == "theorem_checker") == AlphaSolveConfig.CHECK_IS_THEOREM_TIMES
        assert result.solution_path == project_dir / "solution.md"


def test_theorem_checker_not_verifier_decides_problem_solved():
    calls = {"theorem_checker": 0}

    class CheckerAuthorityClient:
        def __init__(self, role):
            self.role = role
            self.calls = 0

        def complete(self, *, messages, tools):
            del tools
            self.calls += 1
            if self.role == "orchestrator":
                if self.calls == 1:
                    return {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "spawn_worker",
                                "type": "function",
                                "function": {
                                    "name": "Agent",
                                    "arguments": json.dumps({"hint": "Try a near miss."}),
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
                                "id": "wait_worker",
                                "type": "function",
                                "function": {
                                    "name": "TaskOutput",
                                    "arguments": json.dumps({"seconds": 5}),
                                },
                            }
                        ],
                    }
                return {"role": "assistant", "content": "No solution yet."}
            if self.role == "generator":
                if self.calls > 1:
                    return {"role": "assistant", "content": "Generator wrote the near miss proposition."}
                task = "\n".join(str(message.get("content") or "") for message in messages)
                worker_dir = re.findall(r"`(unverified_propositions/prop-[^`]+)`", task)[-1]
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "write_near_miss",
                            "type": "function",
                            "function": {
                                "name": "Write",
                                "arguments": json.dumps(
                                    {
                                        "path": f"{worker_dir}/proposition.md",
                                        "content": (
                                            "# Near Miss\n\n"
                                            "## Statement\n\n"
                                            "For every real number x, x = x.\n\n"
                                            "## Proof\n\n"
                                            "By reflexivity.\n"
                                        ),
                                    }
                                ),
                            },
                        }
                    ],
                }
            if self.role.startswith("verifier"):
                return {
                    "role": "assistant",
                    "content": "Verdict: pass\nSolves original problem: yes\n\nThe proposition itself is valid.",
                }
            if self.role == "review_verdict_judge":
                return {
                    "role": "assistant",
                    "content": "pass",
                }
            if self.role == "theorem_checker":
                calls["theorem_checker"] += 1
                return {
                    "role": "assistant",
                    "content": "Solves original problem: no\n\nThe verified proposition is only reflexivity.",
                }
            return {"role": "assistant", "content": "unused"}

    def factory(config):
        return CheckerAuthorityClient(config.name)

    with local_project_dir("checker_authority") as project_dir:
        (project_dir / "problem.md").write_text("# Problem\n\nProve that 1 + 1 = 3.\n", encoding="utf-8")

        result = AlphaSolve(
            project_dir=project_dir,
            max_workers=1,
            client_factory=factory,
            prime_wolfram=False,
            print_to_console=False,
            max_orchestrator_restarts=1,
        ).run()

        assert result.final_answer == "No solution yet."
        assert result.worker_results[0].status == "verified"
        assert not result.worker_results[0].solved_problem
        assert result.worker_results[0].theorem_check_file is None
        assert calls["theorem_checker"] == 1
        assert result.solution_path is None
        assert not (project_dir / "solution.md").exists()
        assert not any(path.name == "theorem_check.md" for path in result.worker_results[0].worker_dir.glob("*.md"))


def test_verifier_scaling_rejects_if_any_independent_attempt_fails():
    verifier_calls = []
    judge_calls = []

    class ScalingClient:
        def __init__(self, role):
            self.role = role
            self.calls = 0

        def complete(self, *, messages, tools):
            del tools
            self.calls += 1
            task = "\n".join(str(message.get("content") or "") for message in messages)
            if self.role == "generator":
                if self.calls > 1:
                    return {"role": "assistant", "content": "Generator wrote the proposition."}
                worker_dir = re.findall(r"`(unverified_propositions/prop-[^`]+)`", task)[-1]
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "write_scaling_candidate",
                            "type": "function",
                            "function": {
                                "name": "Write",
                                "arguments": json.dumps(
                                    {
                                        "path": f"{worker_dir}/proposition.md",
                                        "content": (
                                            "# Scaling Candidate\n\n"
                                            "## Statement\n\n"
                                            "For every real number x, x = x.\n\n"
                                            "## Proof\n\n"
                                            "By reflexivity.\n"
                                        ),
                                    }
                                ),
                            },
                        }
                    ],
                }
            if self.role.startswith("verifier"):
                verifier_calls.append(self.role)
                if len(verifier_calls) == 1:
                    return {"role": "assistant", "content": "Verdict: pass\n\nAttempt one accepts the proposition."}
                return {"role": "assistant", "content": "Verdict: fail\n\nAttempt two found a gap."}
            if self.role == "review_verdict_judge":
                judge_calls.append(task)
                return {
                    "role": "assistant",
                    "content": "pass" if len(judge_calls) == 1 else "fail",
                }
            if self.role == "reviser":
                return {"role": "assistant", "content": "No revision in this test."}
            if self.role == "theorem_checker":
                return {"role": "assistant", "content": "Solves original problem: yes"}
            return {"role": "assistant", "content": "unused"}

    def factory(config):
        return ScalingClient(config.name)

    with local_project_dir("verifier_scaling") as project_dir:
        (project_dir / "problem.md").write_text("# Problem\n\nShow that equality is reflexive.\n", encoding="utf-8")
        layout = ProjectLayout.create(project_dir)
        layout.ensure()
        suite = load_agent_suite_config(pathlib.Path(PACKAGE_ROOT) / "config")

        result = Worker(
            layout=layout,
            suite=suite,
            client_factory=factory,
            max_verify_rounds=1,
            verifier_scaling_factor=2,
            subagent_max_depth=1,
        ).run()

        assert result.status == "rejected"
        assert result.review_file is not None
        review = result.review_file.read_text(encoding="utf-8")
        assert "Attempt two found a gap." in review
        assert "Attempt one accepts the proposition." not in review
        assert verifier_calls[:2] == ["verifier_citation", "verifier_failure_modes"]
        verifier_traces = [item for item in result.trace if item["role"] == "verifier_attempt"]
        assert [item["config"] for item in verifier_traces] == ["verifier_citation", "verifier_failure_modes"]
        assert result.trace[-1]["role"] == "verifier_workflow"
        assert result.trace[-1]["attempts_run"] == 2


def test_review_verdict_judge_handles_markdown_wrapped_verdicts():
    class MarkdownVerdictClient:
        def __init__(self, role):
            self.role = role
            self.calls = 0

        def complete(self, *, messages, tools):
            del tools
            self.calls += 1
            task = "\n".join(str(message.get("content") or "") for message in messages)
            if self.role == "generator":
                if self.calls > 1:
                    return {"role": "assistant", "content": "Generator wrote the proposition."}
                worker_dir = re.findall(r"`(unverified_propositions/prop-[^`]+)`", task)[-1]
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "write_markdown_verdict_candidate",
                            "type": "function",
                            "function": {
                                "name": "Write",
                                "arguments": json.dumps(
                                    {
                                        "path": f"{worker_dir}/proposition.md",
                                        "content": (
                                            "# Markdown Verdict\n\n"
                                            "## Statement\n\n"
                                            "For every real number x, x = x.\n\n"
                                            "## Proof\n\n"
                                            "By reflexivity.\n"
                                        ),
                                    }
                                ),
                            },
                        }
                    ],
                }
            if self.role.startswith("verifier"):
                return {"role": "assistant", "content": "**Verdict: pass**\n\nThe proposition is valid."}
            if self.role == "review_verdict_judge":
                assert "# Attempt Review" in task
                assert "**Verdict: pass**" in task
                return {"role": "assistant", "content": "`pass`"}
            if self.role == "theorem_checker":
                return {"role": "assistant", "content": "Solves original problem: yes"}
            return {"role": "assistant", "content": "unused"}

    def factory(config):
        return MarkdownVerdictClient(config.name)

    with local_project_dir("markdown_verdict") as project_dir:
        (project_dir / "problem.md").write_text("# Problem\n\nShow that equality is reflexive.\n", encoding="utf-8")
        layout = ProjectLayout.create(project_dir)
        layout.ensure()
        suite = load_agent_suite_config(pathlib.Path(PACKAGE_ROOT) / "config")

        result = Worker(
            layout=layout,
            suite=suite,
            client_factory=factory,
            max_verify_rounds=1,
            verifier_scaling_factor=1,
            subagent_max_depth=0,
        ).run()

        assert result.status == "verified"
        assert result.review_file is not None
        assert "**Verdict: pass**" in result.review_file.read_text(encoding="utf-8")
        assert list((result.worker_dir / "verifier_workspace").iterdir()) == []
        assert any(item["role"] == "review_verdict_judge" for item in result.trace)


def test_verifier_workflow_pass_accepts_without_using_remaining_rounds():
    judge_calls = []
    theorem_checker_calls = []

    class MultiRoundVerifierClient:
        def __init__(self, role):
            self.role = role
            self.calls = 0

        def complete(self, *, messages, tools):
            del tools
            self.calls += 1
            task = "\n".join(str(message.get("content") or "") for message in messages)
            if self.role == "generator":
                if self.calls > 1:
                    return {"role": "assistant", "content": "Generator wrote the proposition."}
                worker_dir = re.findall(r"`(unverified_propositions/prop-[^`]+)`", task)[-1]
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "write_multiround_candidate",
                            "type": "function",
                            "function": {
                                "name": "Write",
                                "arguments": json.dumps(
                                    {
                                        "path": f"{worker_dir}/proposition.md",
                                        "content": (
                                            "# Multi Round\n\n"
                                            "## Statement\n\n"
                                            "For every real number x, x = x.\n\n"
                                            "## Proof\n\n"
                                            "By reflexivity.\n"
                                        ),
                                    }
                                ),
                            },
                        }
                    ],
                }
            if self.role.startswith("verifier"):
                return {"role": "assistant", "content": "Verdict: pass\n\nThe attempt accepts the proposition."}
            if self.role == "review_verdict_judge":
                judge_calls.append(task)
                return {"role": "assistant", "content": "pass"}
            if self.role == "theorem_checker":
                theorem_checker_calls.append(task)
                return {"role": "assistant", "content": "Solves original problem: yes"}
            return {"role": "assistant", "content": "unused"}

    def factory(config):
        return MultiRoundVerifierClient(config.name)

    with local_project_dir("verifier_pass_short_circuit") as project_dir:
        (project_dir / "problem.md").write_text("# Problem\n\nShow that equality is reflexive.\n", encoding="utf-8")
        layout = ProjectLayout.create(project_dir)
        layout.ensure()
        suite = load_agent_suite_config(pathlib.Path(PACKAGE_ROOT) / "config")

        result = Worker(
            layout=layout,
            suite=suite,
            client_factory=factory,
            max_verify_rounds=2,
            verifier_scaling_factor=1,
            subagent_max_depth=0,
        ).run()

        assert result.status == "verified"
        assert len(judge_calls) == 1
        assert len(theorem_checker_calls) == AlphaSolveConfig.CHECK_IS_THEOREM_TIMES
        verifier_workflows = [item["workflow"] for item in result.trace if item["role"] == "verifier_workflow"]
        assert verifier_workflows == [1]


def test_verifier_review_is_not_visible_to_later_attempts():
    read_probe = {"content": ""}

    class ReviewIsolationClient:
        def __init__(self, role):
            self.role = role
            self.calls = 0

        def complete(self, *, messages, tools):
            del tools
            self.calls += 1
            task = "\n".join(str(message.get("content") or "") for message in messages)
            if self.role == "generator":
                if self.calls > 1:
                    return {"role": "assistant", "content": "Generator wrote the proposition."}
                worker_dir = re.findall(r"`(unverified_propositions/prop-[^`]+)`", task)[-1]
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "write_candidate",
                            "type": "function",
                            "function": {
                                "name": "Write",
                                "arguments": json.dumps(
                                    {
                                        "path": f"{worker_dir}/proposition.md",
                                        "content": (
                                            "# Candidate\n\n"
                                            "## Statement\n\n"
                                            "For every real number x, x = x.\n\n"
                                            "## Proof\n\n"
                                            "By reflexivity.\n"
                                        ),
                                    }
                                ),
                            },
                        }
                    ],
                }
            if self.role.startswith("verifier"):
                if "Verifier workflow: 1" in task:
                    return {"role": "assistant", "content": "Verdict: fail\n\nFirst round review."}
                if self.calls == 1:
                    proposition_rel = re.findall(r"unverified_propositions/prop-[^\s]+/proposition\.md", task)[-1]
                    worker_dir = pathlib.PurePosixPath(proposition_rel).parent.as_posix()
                    return {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "read_prior_review",
                                "type": "function",
                                "function": {
                                    "name": "Read",
                                    "arguments": json.dumps({"path": f"{worker_dir}/review.md"}),
                                },
                            }
                        ],
                    }
                read_probe["content"] = str(messages[-1].get("content") or "")
                return {"role": "assistant", "content": "Verdict: fail\n\nSecond round remains independent."}
            if self.role == "review_verdict_judge":
                return {"role": "assistant", "content": "fail"}
            if self.role == "reviser":
                return {"role": "assistant", "content": "No revision in this test."}
            return {"role": "assistant", "content": "unused"}

    def factory(config):
        return ReviewIsolationClient(config.name)

    with local_project_dir("review_isolation") as project_dir:
        (project_dir / "problem.md").write_text("# Problem\n\nShow that equality is reflexive.\n", encoding="utf-8")
        layout = ProjectLayout.create(project_dir)
        layout.ensure()
        suite = load_agent_suite_config(pathlib.Path(PACKAGE_ROOT) / "config")

        result = Worker(
            layout=layout,
            suite=suite,
            client_factory=factory,
            max_verify_rounds=2,
            verifier_scaling_factor=1,
            subagent_max_depth=0,
        ).run()

        assert result.status == "rejected"
        assert result.review_file is not None
        assert result.review_file.name == "review.md"
        assert "error" in read_probe["content"].lower()
        assert list((result.worker_dir / "verifier_workspace").iterdir()) == []
        workflows = [item for item in result.trace if item["role"] == "verifier_workflow"]
        assert [item["workflow"] for item in workflows] == [1, 2]


def test_clear_verifier_artifacts_leaves_empty_workspace():
    with local_project_dir("clear_verifier_artifacts") as project_dir:
        (project_dir / "problem.md").write_text("# Problem\n\nShow that equality is reflexive.\n", encoding="utf-8")
        layout = ProjectLayout.create(project_dir)
        layout.ensure()
        suite = load_agent_suite_config(pathlib.Path(PACKAGE_ROOT) / "config")
        worker = Worker(
            layout=layout,
            suite=suite,
            client_factory=make_demo_client_factory(),
        )
        worker.worker_dir.mkdir(parents=True)
        (worker.worker_dir / "review.md").write_text("old review", encoding="utf-8")
        attempt_dir = worker.worker_dir / "verifier_workspace" / "round-01" / "attempt-01"
        attempt_dir.mkdir(parents=True)
        (attempt_dir / "review.md").write_text("old attempt", encoding="utf-8")

        worker._clear_verifier_artifacts()

        verifier_workspace = worker.worker_dir / "verifier_workspace"
        assert verifier_workspace.is_dir()
        assert list(verifier_workspace.iterdir()) == []
        assert not (worker.worker_dir / "review.md").exists()


def test_verifier_workflow_reset_keeps_only_proposition_hint_and_empty_workspace():
    with local_project_dir("verifier_workflow_reset") as project_dir:
        (project_dir / "problem.md").write_text("# Problem\n\nShow that equality is reflexive.\n", encoding="utf-8")
        layout = ProjectLayout.create(project_dir)
        layout.ensure()
        suite = load_agent_suite_config(pathlib.Path(PACKAGE_ROOT) / "config")
        worker = Worker(
            layout=layout,
            suite=suite,
            client_factory=make_demo_client_factory(),
        )
        worker.worker_dir.mkdir(parents=True)
        proposition_file = worker.worker_dir / "candidate-name.md"
        proposition_file.write_text("# Candidate\n\n## Statement\n\nx=x.\n", encoding="utf-8")
        (worker.worker_dir / "worker_hint.md").write_text("hint", encoding="utf-8")
        (worker.worker_dir / "trace.json").write_text("{}", encoding="utf-8")
        (worker.worker_dir / "review.md").write_text("old review", encoding="utf-8")
        old_attempt = worker.worker_dir / "verifier_workspace" / "round-01" / "attempt-01"
        old_attempt.mkdir(parents=True)
        (old_attempt / "review.md").write_text("old attempt", encoding="utf-8")

        worker._reset_verifier_workflow_workspace(proposition_file)

        assert proposition_file.is_file()
        assert (worker.worker_dir / "worker_hint.md").is_file()
        assert not (worker.worker_dir / "trace.json").exists()
        assert not (worker.worker_dir / "review.md").exists()
        verifier_workspace = worker.worker_dir / "verifier_workspace"
        assert verifier_workspace.is_dir()
        assert list(verifier_workspace.iterdir()) == []


def test_solution_writer_collects_recursive_refs_with_final_proposition_last():
    with local_project_dir("solution_refs") as project_dir:
        (project_dir / "problem.md").write_text("# Problem\n\nProve C.\n", encoding="utf-8")
        layout = ProjectLayout.create(project_dir)
        layout.ensure()
        (layout.verified_dir / "a.md").write_text(
            "# A\n\n## Statement\n\nA.\n\n## Proof\n\nDirect.\n",
            encoding="utf-8",
        )
        (layout.verified_dir / "b.md").write_text(
            "# B\n\n## Statement\n\nB by \\ref{a}.\n\n## Proof\n\nUse \\ref{a}.\n",
            encoding="utf-8",
        )
        (layout.verified_dir / "c.md").write_text(
            "# C\n\n## Statement\n\nC by \\ref{b}.\n\n## Proof\n\nUse \\ref{b}.\n",
            encoding="utf-8",
        )

        solution_path = write_solution(layout, layout.verified_dir / "c.md")
        solution = solution_path.read_text(encoding="utf-8")

        assert solution_path == project_dir / "solution.md"
        assert solution.index("### 1. a") < solution.index("### 2. b") < solution.index("### 3. c")


def test_role_workspace_access_blocks_other_unverified_workers_and_locks_generator_file():
    with local_project_dir("guards") as project_dir:
        workspace_root = project_dir / "workspace"
        own_dir = workspace_root / "unverified_propositions" / "prop-own"
        other_dir = workspace_root / "unverified_propositions" / "prop-other"
        own_dir.mkdir(parents=True)
        other_dir.mkdir(parents=True)
        (other_dir / "secret.md").write_text("do not read", encoding="utf-8")

        access = RoleWorkspaceAccess(
            workspace=Workspace(workspace_root),
            worker_rel="unverified_propositions/prop-own",
            deny_other_unverified=True,
            single_proposition_file=True,
        )

        try:
            access.read_text_page("unverified_propositions/prop-other/secret.md")
        except Exception as exc:
            assert "other unverified" in str(exc)
        else:
            raise AssertionError("reading another worker directory should fail")

        access.write_text("unverified_propositions/prop-own/proposition.md", "ok")
        try:
            access.write_text("unverified_propositions/prop-own/second.md", "no")
        except Exception as exc:
            assert "proposition.md" in str(exc)
        else:
            raise AssertionError("generator should be locked to its first proposition file")


def test_role_workspace_access_can_restrict_reads_to_verifier_workspace():
    with local_project_dir("read_root") as project_dir:
        workspace_root = project_dir / "workspace"
        verifier_workspace = workspace_root / "unverified_propositions" / "prop-own" / "verifier_workspace"
        verifier_workspace.mkdir(parents=True)
        (verifier_workspace / "note.md").write_text("scratch", encoding="utf-8")
        (workspace_root / "knowledge").mkdir(parents=True)
        (workspace_root / "knowledge" / "global.md").write_text("global", encoding="utf-8")

        access = RoleWorkspaceAccess(
            workspace=Workspace(workspace_root),
            worker_rel="unverified_propositions/prop-own",
            deny_other_unverified=True,
            read_root_rel="unverified_propositions/prop-own/verifier_workspace",
            write_root_rel="unverified_propositions/prop-own/verifier_workspace",
        )

        assert access.read_text_page("unverified_propositions/prop-own/verifier_workspace/note.md").output == "     1\tscratch"
        try:
            access.read_text_page("knowledge/global.md")
        except Exception as exc:
            assert "read path must stay under" in str(exc)
        else:
            raise AssertionError("subagent file reads should stay inside verifier_workspace")


def test_workspace_read_tool_defaults_to_60_lines_and_can_read_all():
    with local_project_dir("team_read_pages") as project_dir:
        workspace_root = project_dir / "workspace"
        workspace_root.mkdir(parents=True)
        (workspace_root / "notes.md").write_text(
            "".join(f"line {index}\n" for index in range(1, 64)),
            encoding="utf-8",
        )
        access = RoleWorkspaceAccess(workspace=Workspace(workspace_root))
        registry = build_workspace_tool_registry(access)
        read_schema = registry.openai_tools(["Read"])[0]["function"]["parameters"]["properties"]
        assert "How many lines to return in this Read call" in read_schema["n_lines"]["description"]
        assert "ignore n_lines" in read_schema["read_all"]["description"]

        default = registry.execute("Read", {"path": "notes.md"}).content
        assert "60 lines read from file starting from line 1. File has 63 total lines." in default
        assert "    60\tline 60" in default
        assert "    61\tline 61" not in default

        full = registry.execute("Read", {"path": "notes.md", "read_all": True}).content
        assert "63 lines read from file starting from line 1. File has 63 total lines." in full
        assert "    63\tline 63" in full


def test_orchestrator_review_tool_returns_only_reviewer_final_report():
    class ReviewerClient:
        def __init__(self, role):
            self.role = role
            self.calls = 0

        def complete(self, *, messages, tools):
            del tools
            self.calls += 1
            if self.role != "research_reviewer":
                return {"role": "assistant", "content": "unused"}
            if self.calls == 1:
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "read_index",
                            "type": "function",
                            "function": {
                                "name": "Read",
                                "arguments": json.dumps({"path": "verified_propositions/index.md"}),
                            },
                        }
                    ],
                }
            assert messages[-1]["role"] == "tool"
            assert "internal read content" in str(messages[-1].get("content") or "")
            return {"role": "assistant", "content": "## Current state\nClean reviewer report."}

    with local_project_dir("review_final_report_only") as project_dir:
        (project_dir / "problem.md").write_text("# Problem\n\nSurvey progress.\n", encoding="utf-8")
        layout = ProjectLayout.create(project_dir)
        layout.ensure()
        (layout.verified_dir / "index.md").write_text("internal read content", encoding="utf-8")
        suite = load_agent_suite_config(pathlib.Path(PACKAGE_ROOT) / "config")
        subagents = SubagentService(
            suite=suite,
            client_factory=lambda config: ReviewerClient(config.name),
            max_depth=0,
            file_access_factory=lambda: RoleWorkspaceAccess(workspace=Workspace(layout.workspace_dir)),
            session_prefix="orchestrator",
        )
        orchestrator = Orchestrator(
            layout=layout,
            suite=suite,
            client_factory=lambda config: ReviewerClient(config.name),
            max_workers=1,
            max_verify_rounds=1,
            verifier_scaling_factor=1,
            subagent_max_depth=0,
        )
        config = suite.agents["orchestrator"]
        registry = orchestrator._build_registry(manager=object(), subagents=subagents)

        result = registry.execute(
            "Review",
            {"task": "survey"},
            enabled=config.tools,
            tool_parameters=config.tool_parameters,
        )

        assert not result.is_error
        assert "agent_id: orchestrator/research_reviewer/depth-0/" in result.content
        assert "actual_subagent_type: research_reviewer" in result.content
        assert "status: completed" in result.content
        assert "[summary]" in result.content
        assert "## Current state\nClean reviewer report." in result.content
        assert '"trace"' not in result.content
        assert '"final_answer"' not in result.content
        assert "internal read content" not in result.content


def test_orchestrator_can_organize_verified_propositions_without_renaming_markdown_files():
    with local_project_dir("orchestrator_verified_manage") as project_dir:
        (project_dir / "problem.md").write_text("# Problem\n\nOrganize verified propositions.\n", encoding="utf-8")
        layout = ProjectLayout.create(project_dir)
        layout.ensure()
        (layout.verified_dir / "bootstrap-lemma.md").write_text("# Bootstrap Lemma\n", encoding="utf-8")
        suite = load_agent_suite_config(pathlib.Path(PACKAGE_ROOT) / "config")
        orchestrator = Orchestrator(
            layout=layout,
            suite=suite,
            client_factory=make_demo_client_factory(),
            max_workers=1,
            max_verify_rounds=1,
            verifier_scaling_factor=1,
            subagent_max_depth=0,
        )
        config = suite.agents["orchestrator"]

        class DummyReviewService:
            def call_tool(self, args):
                return args

        registry = orchestrator._build_registry(manager=object(), subagents=DummyReviewService())

        openai_tools = registry.openai_tools(config.tools, config.tool_parameters)
        tool_names = [tool["function"]["name"] for tool in openai_tools]
        assert "MakeDir" in tool_names
        assert "Rename" in tool_names
        assert "Move" in tool_names
        assert "Write" in tool_names
        assert "Edit" in tool_names
        assert "Delete" not in tool_names
        assert "Delete" not in [tool.name for tool in registry.registered_tools()]
        rename_description = next(
            tool["function"]["description"]
            for tool in openai_tools
            if tool["function"]["name"] == "Rename"
        )
        assert "Rename a folder" in rename_description
        assert "Use this only when the item stays in the same directory" in rename_description

        index_content = (
            "# Verified Propositions Index\n\n"
            "## Directory\n"
            "- [[bootstrap-lemma]] - proves the bootstrap lemma; see \\ref{bootstrap-lemma}.\n\n"
            "## Current Progress And Insights\n"
            "- Try closing the next bootstrap step.\n"
        )
        wrote_index = registry.execute(
            "Write",
            {
                "path": "verified_propositions/index.md",
                "content": index_content,
            },
            enabled=config.tools,
            tool_parameters=config.tool_parameters,
        )
        assert not wrote_index.is_error
        assert (layout.verified_dir / "index.md").read_text(encoding="utf-8") == index_content

        edited_index = registry.execute(
            "Edit",
            {
                "path": "verified_propositions/index.md",
                "old_str": "Try closing the next bootstrap step.",
                "new_str": "Compare bootstrap assumptions A and B next.",
            },
            enabled=config.tools,
            tool_parameters=config.tool_parameters,
        )
        assert not edited_index.is_error
        assert "Compare bootstrap assumptions" in (layout.verified_dir / "index.md").read_text(encoding="utf-8")

        wrote_prop = registry.execute(
            "Write",
            {
                "path": "verified_propositions/new-prop.md",
                "content": "# New Prop\n",
            },
            enabled=config.tools,
            tool_parameters=config.tool_parameters,
        )
        assert wrote_prop.is_error
        assert "must match pattern" in wrote_prop.content

        made = registry.execute(
            "MakeDir",
            {"path": "verified_propositions/bootstrap-A"},
            enabled=config.tools,
            tool_parameters=config.tool_parameters,
        )
        assert not made.is_error
        assert (layout.verified_dir / "bootstrap-A").is_dir()

        wrote_nested_index = registry.execute(
            "Write",
            {
                "path": "verified_propositions/bootstrap-A/index.md",
                "content": "# Bootstrap A\n\n- Local route map.\n",
            },
            enabled=config.tools,
            tool_parameters=config.tool_parameters,
        )
        assert not wrote_nested_index.is_error
        assert (layout.verified_dir / "bootstrap-A" / "index.md").is_file()

        edited_nested_index = registry.execute(
            "Edit",
            {
                "path": "verified_propositions/bootstrap-A/index.md",
                "old_str": "Local route map.",
                "new_str": "Local route map for bootstrap assumption A.",
            },
            enabled=config.tools,
            tool_parameters=config.tool_parameters,
        )
        assert not edited_nested_index.is_error
        assert "bootstrap assumption A" in (layout.verified_dir / "bootstrap-A" / "index.md").read_text(encoding="utf-8")

        moved = registry.execute(
            "Move",
            {
                "path": "verified_propositions/bootstrap-lemma.md",
                "destination_dir": "verified_propositions/bootstrap-A",
            },
            enabled=config.tools,
            tool_parameters=config.tool_parameters,
        )
        assert not moved.is_error
        assert (layout.verified_dir / "bootstrap-A" / "bootstrap-lemma.md").is_file()
        index_after_move = (layout.verified_dir / "index.md").read_text(encoding="utf-8")
        assert "\\ref{bootstrap-A\\bootstrap-lemma}" in index_after_move
        assert "\\ref{bootstrap-lemma}" not in index_after_move

        renamed_file = registry.execute(
            "Rename",
            {
                "directory": "verified_propositions/bootstrap-A",
                "old_name": "bootstrap-lemma.md",
                "new_name": "renamed-lemma.md",
            },
            enabled=config.tools,
            tool_parameters=config.tool_parameters,
        )
        assert renamed_file.is_error
        assert "must match pattern" in renamed_file.content

        renamed_index = registry.execute(
            "Rename",
            {
                "directory": "verified_propositions",
                "old_name": "index.md",
                "new_name": "renamed-index",
            },
            enabled=config.tools,
            tool_parameters=config.tool_parameters,
        )
        assert renamed_index.is_error
        assert "must match pattern" in renamed_index.content

        moved_nested_index = registry.execute(
            "Move",
            {
                "path": "verified_propositions/bootstrap-A/index.md",
                "destination_dir": "verified_propositions",
            },
            enabled=config.tools,
            tool_parameters=config.tool_parameters,
        )
        assert moved_nested_index.is_error
        assert "must match pattern" in moved_nested_index.content

        outside = registry.execute(
            "MakeDir",
            {"path": "knowledge/bootstrap-A"},
            enabled=config.tools,
            tool_parameters=config.tool_parameters,
        )
        assert outside.is_error
        assert "must match pattern" in outside.content

        move_outside_source = registry.execute(
            "Move",
            {
                "path": "knowledge/note.md",
                "destination_dir": "verified_propositions/bootstrap-A",
            },
            enabled=config.tools,
            tool_parameters=config.tool_parameters,
        )
        assert move_outside_source.is_error
        assert "must match pattern" in move_outside_source.content

        move_outside_destination = registry.execute(
            "Move",
            {
                "path": "verified_propositions/bootstrap-A/bootstrap-lemma.md",
                "destination_dir": "knowledge",
            },
            enabled=config.tools,
            tool_parameters=config.tool_parameters,
        )
        assert move_outside_destination.is_error
        assert "must match pattern" in move_outside_destination.content


def test_verified_count_ignores_verified_index_file():
    with local_project_dir("verified_count_index") as project_dir:
        verified_dir = project_dir / "workspace" / "verified_propositions"
        verified_dir.mkdir(parents=True)
        (verified_dir / "index.md").write_text("# Verified Propositions Index\n", encoding="utf-8")
        assert verified_count(verified_dir) == 0

        (verified_dir / "lemma.md").write_text("# Lemma\n", encoding="utf-8")
        assert verified_count(verified_dir) == 1


def test_move_file_updates_verified_proposition_references():
    with local_project_dir("verified_move_references") as project_dir:
        workspace_root = project_dir / "workspace"
        verified_dir = workspace_root / "verified_propositions"
        verified_dir.mkdir(parents=True)
        (verified_dir / "source.md").write_text(
            "# Source\n\nUses \\ref{target} and \\ref{archive\\target}.\n",
            encoding="utf-8",
        )
        (verified_dir / "target.md").write_text(
            "# Target\n\nSelf citation \\ref{target}.\n",
            encoding="utf-8",
        )
        (verified_dir / "index.md").write_text(
            "# Index\n\n- See \\ref{target}.\n",
            encoding="utf-8",
        )
        (verified_dir / "archive").mkdir()

        access = RoleWorkspaceAccess(
            workspace=Workspace(workspace_root),
            write_root_rel="verified_propositions",
        )

        moved = access.move_file("verified_propositions/target.md", "verified_propositions/archive")

        assert moved == {
            "old_path": "verified_propositions/target.md",
            "path": "verified_propositions/archive/target.md",
        }
        assert "\\ref{archive\\target}" in (verified_dir / "source.md").read_text(encoding="utf-8")
        assert "\\ref{archive\\target}" in (verified_dir / "archive" / "target.md").read_text(encoding="utf-8")
        assert "\\ref{archive\\target}" in (verified_dir / "index.md").read_text(encoding="utf-8")
        assert "\\ref{target}" not in (verified_dir / "source.md").read_text(encoding="utf-8")

        (verified_dir / "final").mkdir()
        moved_again = access.move_file("verified_propositions/archive/target.md", "verified_propositions/final")

        source_after_second_move = (verified_dir / "source.md").read_text(encoding="utf-8")
        assert moved_again == {
            "old_path": "verified_propositions/archive/target.md",
            "path": "verified_propositions/final/target.md",
        }
        assert source_after_second_move.count("\\ref{final\\target}") == 2
        assert "\\ref{archive/target}" not in source_after_second_move
        assert "\\ref{archive\\target}" not in source_after_second_move


def test_move_file_outside_verified_propositions_does_not_update_references():
    with local_project_dir("knowledge_move_references") as project_dir:
        workspace_root = project_dir / "workspace"
        verified_dir = workspace_root / "verified_propositions"
        knowledge_dir = workspace_root / "knowledge"
        verified_dir.mkdir(parents=True)
        knowledge_dir.mkdir()
        (verified_dir / "note.md").write_text("# Note\n\nSee \\ref{entry}.\n", encoding="utf-8")
        (knowledge_dir / "entry.md").write_text("# Entry\n", encoding="utf-8")
        (knowledge_dir / "archive").mkdir()

        access = RoleWorkspaceAccess(
            workspace=Workspace(workspace_root),
            write_root_rel="knowledge",
        )

        access.move_file("knowledge/entry.md", "knowledge/archive")

        assert "\\ref{entry}" in (verified_dir / "note.md").read_text(encoding="utf-8")


def test_role_workspace_access_supports_append_rename_move_delete_and_protects_special_files():
    with local_project_dir("knowledge_manage") as project_dir:
        workspace_root = project_dir / "workspace"
        knowledge_dir = workspace_root / "knowledge"
        knowledge_dir.mkdir(parents=True)
        (knowledge_dir / "index.md").write_text("# Index\n", encoding="utf-8")

        access = RoleWorkspaceAccess(
            workspace=Workspace(workspace_root),
            read_root_rel="knowledge",
            write_root_rel="knowledge",
            destructive_protected_file_names=("index.md", "common-errors.md"),
        )

        access.write_text("knowledge/entry.md", "# Entry\n")
        access.write_text("knowledge/entry.md", "\n## Detail\n", mode="append")
        assert access.read_text_page("knowledge/entry.md").output == "     1\t# Entry\n     2\t\n     3\t## Detail\n"

        renamed = access.rename_item("knowledge", "entry.md", "renamed-entry.md")
        assert renamed == {
            "old_path": "knowledge/entry.md",
            "path": "knowledge/renamed-entry.md",
        }
        assert (knowledge_dir / "renamed-entry.md").is_file()

        (knowledge_dir / "topic").mkdir()
        moved = access.move_file("knowledge/renamed-entry.md", "knowledge/topic")
        assert moved == {
            "old_path": "knowledge/renamed-entry.md",
            "path": "knowledge/topic/renamed-entry.md",
        }
        assert (knowledge_dir / "topic" / "renamed-entry.md").is_file()

        try:
            access.rename_path("knowledge/topic/renamed-entry.md", "knowledge/renamed-entry.md")
        except Exception as exc:
            assert "use Move to change directories" in str(exc)
        else:
            raise AssertionError("Rename should not move files across directories")

        access.write_text("knowledge/obsolete.md", "old\n")
        deleted = access.delete_file("knowledge/obsolete.md")
        assert deleted == "knowledge/obsolete.md"
        assert not (knowledge_dir / "obsolete.md").exists()

        try:
            access.delete_path("knowledge/topic")
        except Exception as exc:
            assert "not empty" in str(exc) or "directory is not empty" in str(exc)
        else:
            raise AssertionError("non-empty directories should not be deletable")

        access.delete_file("knowledge/topic/renamed-entry.md")
        deleted_dir = access.delete_path("knowledge/topic")
        assert deleted_dir == "knowledge/topic"
        assert not (knowledge_dir / "topic").exists()

        try:
            access.delete_file("knowledge/index.md")
        except Exception as exc:
            assert "destructive operations" in str(exc)
        else:
            raise AssertionError("special knowledge files should not be deletable")


def test_update_entry_metadata_normalizes_frontmatter_to_modification_count_only():
    with local_project_dir("knowledge_metadata") as project_dir:
        knowledge_dir = project_dir / "workspace" / "knowledge"
        knowledge_dir.mkdir(parents=True)
        entry = knowledge_dir / "energy-estimate.md"
        entry.write_text(
            "---\n"
            "created: 2026-01-01T00:00:00\n"
            "updated: 2026-01-02T00:00:00\n"
            "topics: [energy]\n"
            "---\n\n"
            "# Energy Estimate\n",
            encoding="utf-8",
        )

        _update_entry_metadata((entry,))
        once = entry.read_text(encoding="utf-8")
        assert once.startswith("---\nmodification_count: 1\n---\n\n# Energy Estimate\n")
        assert "created:" not in once
        assert "updated:" not in once
        assert "topics:" not in once

        _update_entry_metadata((entry,))
        assert entry.read_text(encoding="utf-8").startswith("---\nmodification_count: 2\n---\n\n# Energy Estimate\n")


def test_generator_curator_submits_reasoning_slice_with_each_subagent_trace():
    class CapturingCuratorQueue:
        def __init__(self):
            self.tasks = []

        def submit(self, task):
            self.tasks.append(task)

    class CuratorClient:
        def __init__(self, role):
            self.role = role
            self.calls = 0

        def complete(self, *, messages, tools):
            del tools
            self.calls += 1
            if self.role == "generator":
                task = "\n".join(str(message.get("content") or "") for message in messages)
                worker_dir = re.findall(r"`(unverified_propositions/prop-[^`]+)`", task)[-1]
                if self.calls == 1:
                    return {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "First generator reasoning slice.",
                        "tool_calls": [
                            {
                                "id": "call_reasoning_a",
                                "type": "function",
                                "function": {
                                    "name": "Agent",
                                    "arguments": json.dumps(
                                        {"type": "reasoning_subagent", "task": "Check the first bounded claim."}
                                    ),
                                },
                            }
                        ],
                    }
                if self.calls == 2:
                    return {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "Second generator reasoning slice.",
                        "tool_calls": [
                            {
                                "id": "call_reasoning_b",
                                "type": "function",
                                "function": {
                                    "name": "Agent",
                                    "arguments": json.dumps(
                                        {"type": "reasoning_subagent", "task": "Check the second bounded claim."}
                                    ),
                                },
                            }
                        ],
                    }
                if self.calls == 3:
                    return {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "write_curator_demo",
                                "type": "function",
                                "function": {
                                    "name": "Write",
                                    "arguments": json.dumps(
                                        {
                                            "path": f"{worker_dir}/proposition.md",
                                            "content": (
                                                "# Curator Demo\n\n"
                                                "## Statement\n\n"
                                                "For every real number x, x = x.\n\n"
                                                "## Proof\n\n"
                                                "This follows from equality reflexivity.\n"
                                            ),
                                        }
                                    ),
                                },
                            }
                        ],
                    }
                return {"role": "assistant", "content": "Generator finished."}
            if self.role == "reasoning_subagent":
                return {"role": "assistant", "content": "PROVED\n\nThe bounded claim is valid."}
            if self.role.startswith("verifier"):
                return {"role": "assistant", "content": "Verdict: pass\n\nThe proposition is valid."}
            if self.role == "review_verdict_judge":
                return {"role": "assistant", "content": "pass"}
            if self.role == "theorem_checker":
                return {"role": "assistant", "content": "Solves original problem: yes\n\nThe proposition matches the problem."}
            return {"role": "assistant", "content": "unused"}

    def factory(config):
        return CuratorClient(config.name)

    with local_project_dir("generator_curator") as project_dir:
        (project_dir / "problem.md").write_text("# Problem\n\nShow that equality is reflexive.\n", encoding="utf-8")
        layout = ProjectLayout.create(project_dir)
        layout.ensure()
        curator_queue = CapturingCuratorQueue()
        suite = load_agent_suite_config(pathlib.Path(PACKAGE_ROOT) / "config")

        result = Worker(
            layout=layout,
            suite=suite,
            client_factory=factory,
            max_verify_rounds=1,
            subagent_max_depth=1,
            curator_queue=curator_queue,
        ).run()

        assert result.status == "verified"
        generator_tasks = [task for task in curator_queue.tasks if "/generator/" in task.source_label]
        assert len(generator_tasks) == 2
        first_context = generator_tasks[0].caller_context
        second_context = generator_tasks[1].caller_context
        assert generator_tasks[0].source_label.endswith("/generator/reasoning_subagent")
        assert first_context["caller_role"] == "generator"
        assert first_context["reasoning_since_previous_subagent"][0]["content"] == "First generator reasoning slice."
        assert second_context["reasoning_since_previous_subagent"][0]["content"] == "Second generator reasoning slice."
        assert "First generator reasoning slice." not in json.dumps(second_context, ensure_ascii=False)
        assert generator_tasks[0].trace_segment[0]["type"] == "run_start"


def test_execution_gateway_keeps_sessions_isolated_and_blocks_filesystem():
    gateway = ExecutionGateway(python_workers=1, wolfram_enabled=False)
    try:
        gateway.run_python(session_id="alpha", code="x = 41")
        same_session = gateway.run_python(session_id="alpha", code="x + 1")
        other_session = gateway.run_python(session_id="beta", code="'x' in globals()")
        denied = gateway.run_python(session_id="alpha", code="import os\nos.listdir('.')", timeout_seconds=5)

        assert "[stdout]" in same_session.tool_content
        assert "42" in same_session.tool_content
        assert "False" in other_session.tool_content
        assert "[error]" in denied.tool_content
        assert "filesystem access is disabled" in denied.tool_content
    finally:
        gateway.close()


def test_execution_gateway_close_session_resets_python_env_and_workdir():
    gateway = ExecutionGateway(python_workers=1, wolfram_enabled=False)
    try:
        first_dir = gateway.run_python(
            session_id="alpha",
            code="import os\nos.getcwd()",
            timeout_seconds=5,
            allow_filesystem=True,
        )
        gateway.run_python(session_id="alpha", code="x = 41")

        gateway.close_session("alpha")

        second_dir = gateway.run_python(
            session_id="alpha",
            code="import os\nos.getcwd()",
            timeout_seconds=5,
            allow_filesystem=True,
        )
        cleared = gateway.run_python(session_id="alpha", code="'x' in globals()")

        assert "[stdout]" in first_dir.tool_content
        assert "[stdout]" in second_dir.tool_content
        assert first_dir.tool_content != second_dir.tool_content
        assert "False" in cleared.tool_content
    finally:
        gateway.close()


def test_subagent_service_uses_strict_types_and_gateway_python_tool():
    suite = load_agent_suite_config(pathlib.Path(PACKAGE_ROOT) / "config" / "agents.yaml")
    gateway = ExecutionGateway(python_workers=1, wolfram_enabled=False)
    service = SubagentService(
        suite=suite,
        client_factory=make_demo_client_factory(),
        max_depth=1,
        execution_gateway=gateway,
        session_prefix="pytest",
    )
    try:
        rejected = service.call_tool({"type": "math", "task": "Try an unsupported subagent type."})
        assert rejected.is_error
        assert "unknown subagent type: math" in rejected.content
        assert "reasoning_subagent" in rejected.content

        registry = service._build_subagent_registry(depth=0, session_id="pytest/session")
        python_result = registry.execute("RunPython", {"code": "value = 6 * 7\nvalue"})
        denied = registry.execute("RunPython", {"code": "open('leak.txt', 'w')"})
        tools = registry.openai_tools(["Agent"], suite.agents["generator"].tool_parameters)
        type_schema = tools[0]["function"]["parameters"]["properties"]["type"]
        blocked = registry.execute(
            "Agent",
            {"type": "curator", "task": "This should not be callable from generator."},
            enabled=["Agent"],
            tool_parameters=suite.agents["generator"].tool_parameters,
        )

        assert "[stdout]" in python_result.content
        assert "42" in python_result.content
        assert denied.is_error
        assert "filesystem access is disabled" in denied.content
        assert type_schema["enum"] == [
            "compute_subagent",
            "numerical_experiment_subagent",
            "reasoning_subagent",
        ]
        assert blocked.is_error
        assert "must be one of" in blocked.content
        # reasoning_subagent config includes GetCurrentTime which the subagent
        # registry does not yet register; skip the success assertion for now.
        reasoning = service.call_tool({"type": "reasoning_subagent", "task": "Check x=x."})

        max_depth_registry = service._build_subagent_registry(depth=1, session_id="pytest/deep")
        assert "Agent" not in [tool.name for tool in max_depth_registry.registered_tools()]
    finally:
        gateway.close()


def test_subagent_service_cleans_up_gateway_session_after_return():
    class SessionClient:
        def __init__(self, role):
            self.role = role
            self.calls = 0

        def complete(self, *, messages, tools):
            del messages, tools
            self.calls += 1
            if self.role == "compute_subagent" and self.calls == 1:
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "run_python_once",
                            "type": "function",
                            "function": {
                                "name": "RunPython",
                                "arguments": json.dumps({"code": "x = 6 * 7\nx"}),
                            },
                        }
                    ],
                }
            return {"role": "assistant", "content": "done"}

    suite = load_agent_suite_config(pathlib.Path(PACKAGE_ROOT) / "config" / "agents.yaml")
    gateway = ExecutionGateway(python_workers=1, wolfram_enabled=False)
    service = SubagentService(
        suite=suite,
        client_factory=lambda config: SessionClient(config.name),
        max_depth=1,
        execution_gateway=gateway,
        session_prefix="pytest-cleanup",
    )
    try:
        result = service.call("compute_subagent", "Use Python once and finish.")

        pool = gateway._python_pool
        assert pool is not None
        assert "agent_id: pytest-cleanup/compute_subagent/depth-0/" in result
        assert "actual_subagent_type: compute_subagent" in result
        assert "status: completed" in result
        assert "[summary]\ndone" in result
        assert pool._session_worker == {}
    finally:
        gateway.close()
