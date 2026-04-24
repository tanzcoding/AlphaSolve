import os
import pathlib
import shutil
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alphasolve.agents.general import Workspace, load_agent_suite_config  # noqa: E402
from alphasolve.agents.team import FilesystemAlphaSolve  # noqa: E402
from alphasolve.agents.team.demo import make_demo_client_factory  # noqa: E402
from alphasolve.agents.team.tools import RoleWorkspaceAccess  # noqa: E402
from alphasolve.config.agent_config import PACKAGE_ROOT  # noqa: E402


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

    assert {"orchestrator", "generator", "verifier", "reviser"} <= set(suite.agents)
    assert {"reasoning_subagent", "compute_subagent"} <= set(suite.subagents)
    assert "spawn_worker" in suite.agents["orchestrator"].tools
    assert "agent" in suite.agents["generator"].tools


def test_filesystem_workflow_demo_creates_workspace_and_verified_lemma():
    with local_project_dir("demo") as project_dir:
        (project_dir / "problem.md").write_text("# Problem\n\nShow that equality is reflexive.\n", encoding="utf-8")

        result = FilesystemAlphaSolve(
            project_dir=project_dir,
            max_workers=1,
            client_factory=make_demo_client_factory(),
            prime_wolfram=False,
            print_to_console=False,
        ).run()

        assert result.final_answer == "Demo run complete."
        assert (project_dir / "workspace" / "knowledge").is_dir()
        assert (project_dir / "workspace" / "unverified_lemmas").is_dir()
        assert (project_dir / "workspace" / "verified_lemmas" / "demo-lemma.md").is_file()
        assert (project_dir / "logs" / "orchestrator_trace.json").is_file()
        assert result.worker_results
        assert result.worker_results[0].status == "verified"


def test_role_workspace_access_blocks_other_unverified_workers_and_locks_generator_file():
    with local_project_dir("guards") as project_dir:
        workspace_root = project_dir / "workspace"
        own_dir = workspace_root / "unverified_lemmas" / "lemma-own"
        other_dir = workspace_root / "unverified_lemmas" / "lemma-other"
        own_dir.mkdir(parents=True)
        other_dir.mkdir(parents=True)
        (other_dir / "secret.md").write_text("do not read", encoding="utf-8")

        access = RoleWorkspaceAccess(
            workspace=Workspace(workspace_root),
            worker_rel="unverified_lemmas/lemma-own",
            deny_other_unverified=True,
            single_lemma_file=True,
        )

        try:
            access.read_text("unverified_lemmas/lemma-other/secret.md")
        except Exception as exc:
            assert "other unverified" in str(exc)
        else:
            raise AssertionError("reading another worker directory should fail")

        access.write_text("unverified_lemmas/lemma-own/first.md", "ok")
        try:
            access.write_text("unverified_lemmas/lemma-own/second.md", "no")
        except Exception as exc:
            assert "only rewrite" in str(exc)
        else:
            raise AssertionError("generator should be locked to its first lemma file")
