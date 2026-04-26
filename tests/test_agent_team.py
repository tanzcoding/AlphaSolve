import json
import os
import pathlib
import re
import shutil
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alphasolve.agents.general import GeneralAgentConfig, Workspace, load_agent_suite_config  # noqa: E402
from alphasolve.agents.team import AlphaSolve  # noqa: E402
from alphasolve.agents.team.demo import make_demo_client_factory  # noqa: E402
from alphasolve.agents.team.knowledge_digest import DigestTask, KnowledgeDigestQueue  # noqa: E402
from alphasolve.agents.team.worker import Worker  # noqa: E402
from alphasolve.agents.team.project import ProjectLayout  # noqa: E402
from alphasolve.agents.team.solution import write_solution  # noqa: E402
from alphasolve.agents.team.tools import RoleWorkspaceAccess, SubagentService  # noqa: E402
from alphasolve.config.agent_config import AlphaSolveConfig  # noqa: E402
from alphasolve.config.agent_config import PACKAGE_ROOT  # noqa: E402
from alphasolve.execution import ExecutionGateway  # noqa: E402


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
    assert "spawn_worker" in suite.agents["orchestrator"].tools
    assert "agent" in suite.agents["generator"].tools
    assert suite.agents["generator"].tool_parameters["agent"]["type"]["enum"] == [
        "compute_subagent",
        "numerical_experiment_subagent",
        "reasoning_subagent",
    ]
    assert suite.subagents["reasoning_subagent"].tool_parameters["agent"]["type"]["enum"] == ["reasoning_subagent"]
    assert suite.settings["max_verify_rounds"] == 6
    assert suite.settings["verifier_scaling_factor"] == 5
    assert suite.settings["verifier_agents"] == [
        "verifier_failure_modes",
        "verifier_stepwise",
    ]
    assert suite.settings["max_orchestrator_restarts"] == 50
    assert suite.subagents["reasoning_subagent"].when_to_use
    assert "get_child_item" in suite.subagents["reasoning_subagent"].tools
    digest = suite.subagents["knowledge_digest"]
    assert digest.tool_parameters["get_child_item"]["path"] == {"default": "knowledge", "enum": ["knowledge", "knowledge/"]}
    assert digest.tool_parameters["get_child_item"]["recursive"]["const"] is False
    assert digest.tool_parameters["write_file"]["path"]["pattern"].endswith("\\.md$")
    assert "not a transcript archive" in digest.system_prompt
    assert "<source_label>" not in digest.system_prompt
    assert suite_from_dir.agents["generator"].tools == suite.agents["generator"].tools


def test_knowledge_digest_task_prompt_hides_source_labels():
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
                "knowledge_digest": GeneralAgentConfig(
                    name="knowledge_digest",
                    system_prompt="Digest prompt",
                    tools=[],
                    max_turns=1,
                )
            }

    with local_project_dir("digest_sanitizes_source") as project_dir:
        workspace_dir = project_dir / "workspace"
        knowledge_dir = workspace_dir / "knowledge"
        knowledge_dir.mkdir(parents=True)
        client = CapturingClient()
        queue = KnowledgeDigestQueue(
            knowledge_dir=knowledge_dir,
            workspace_dir=workspace_dir,
            suite=Suite(),
            client_factory=lambda config: client,
        )

        queue._run_digest(
            DigestTask(
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
        assert (project_dir / "workspace" / "verified_propositions" / "demo-proposition.md").is_file()
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
                                    "name": "spawn_worker",
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
                                    "name": "wait",
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
                                "name": "write_file",
                                "arguments": json.dumps(
                                    {
                                        "path": f"{worker_dir}/near-miss.md",
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
                                "name": "write_file",
                                "arguments": json.dumps(
                                    {
                                        "path": f"{worker_dir}/scaling-candidate.md",
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
            worker_id=0,
            max_verify_rounds=1,
            verifier_scaling_factor=2,
            subagent_max_depth=1,
        ).run()

        assert result.status == "rejected"
        assert result.review_file is not None
        review = result.review_file.read_text(encoding="utf-8")
        assert "Attempt two found a gap." in review
        assert "Attempt one accepts the proposition." not in review
        assert verifier_calls == ["verifier_failure_modes", "verifier_stepwise"]
        verifier_traces = [item for item in result.trace if item["role"] == "verifier_attempt"]
        assert [item["config"] for item in verifier_traces] == ["verifier_failure_modes", "verifier_stepwise"]
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
                                "name": "write_file",
                                "arguments": json.dumps(
                                    {
                                        "path": f"{worker_dir}/markdown-verdict.md",
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
            worker_id=0,
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
                                "name": "write_file",
                                "arguments": json.dumps(
                                    {
                                        "path": f"{worker_dir}/multi-round.md",
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
            worker_id=0,
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
                                "name": "write_file",
                                "arguments": json.dumps(
                                    {
                                        "path": f"{worker_dir}/candidate.md",
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
                    proposition_rel = re.findall(r"unverified_propositions/prop-[^\s]+/candidate\.md", task)[-1]
                    worker_dir = pathlib.PurePosixPath(proposition_rel).parent.as_posix()
                    return {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "read_prior_review",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
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
            worker_id=0,
            max_verify_rounds=2,
            verifier_scaling_factor=1,
            subagent_max_depth=0,
        ).run()

        assert result.status == "rejected"
        assert result.review_file is not None
        assert result.review_file.name == "review.md"
        assert "error" in read_probe["content"]
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
            worker_id=0,
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
            worker_id=0,
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
            access.read_text("unverified_propositions/prop-other/secret.md")
        except Exception as exc:
            assert "other unverified" in str(exc)
        else:
            raise AssertionError("reading another worker directory should fail")

        access.write_text("unverified_propositions/prop-own/first.md", "ok")
        try:
            access.write_text("unverified_propositions/prop-own/second.md", "no")
        except Exception as exc:
            assert "only rewrite" in str(exc)
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

        assert access.read_text("unverified_propositions/prop-own/verifier_workspace/note.md") == "scratch"
        try:
            access.read_text("knowledge/global.md")
        except Exception as exc:
            assert "read path must stay under" in str(exc)
        else:
            raise AssertionError("subagent file reads should stay inside verifier_workspace")


def test_generator_digest_submits_reasoning_slice_with_each_subagent_trace():
    class CapturingDigestQueue:
        def __init__(self):
            self.tasks = []

        def submit(self, task):
            self.tasks.append(task)

    class DigestClient:
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
                                    "name": "agent",
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
                                    "name": "agent",
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
                                "id": "write_digest_demo",
                                "type": "function",
                                "function": {
                                    "name": "write_file",
                                    "arguments": json.dumps(
                                        {
                                            "path": f"{worker_dir}/digest-demo.md",
                                            "content": (
                                                "# Digest Demo\n\n"
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
        return DigestClient(config.name)

    with local_project_dir("generator_digest") as project_dir:
        (project_dir / "problem.md").write_text("# Problem\n\nShow that equality is reflexive.\n", encoding="utf-8")
        layout = ProjectLayout.create(project_dir)
        layout.ensure()
        digest_queue = CapturingDigestQueue()
        suite = load_agent_suite_config(pathlib.Path(PACKAGE_ROOT) / "config")

        result = Worker(
            layout=layout,
            suite=suite,
            client_factory=factory,
            worker_id=0,
            max_verify_rounds=1,
            subagent_max_depth=1,
            digest_queue=digest_queue,
        ).run()

        assert result.status == "verified"
        generator_tasks = [task for task in digest_queue.tasks if "/generator/" in task.source_label]
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
        python_result = registry.execute("run_python", {"code": "value = 6 * 7\nvalue"})
        denied = registry.execute("run_python", {"code": "open('leak.txt', 'w')"})
        tools = registry.openai_tools(["agent"], suite.agents["generator"].tool_parameters)
        type_schema = tools[0]["function"]["parameters"]["properties"]["type"]
        blocked = registry.execute(
            "agent",
            {"type": "knowledge_digest", "task": "This should not be callable from generator."},
            enabled=["agent"],
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
        reasoning = service.call_tool({"type": "reasoning_subagent", "task": "Check x=x."})
        assert not reasoning.is_error

        max_depth_registry = service._build_subagent_registry(depth=1, session_id="pytest/deep")
        assert "agent" not in [tool.name for tool in max_depth_registry.registered_tools()]
    finally:
        gateway.close()
