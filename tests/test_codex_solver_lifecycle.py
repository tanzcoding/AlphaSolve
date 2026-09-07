"""Codex 迁移后关键角色的失败传播及会话收尾。"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from alphasolve.agent import AgentConfig, AgentRunError, AgentRunResult
from alphasolve.solver import orchestrator as orchestrator_module
from alphasolve.solver import app as app_module
from alphasolve.solver.app import AlphaSolve
from alphasolve.solver.curator import CuratorQueue, CuratorTask
from alphasolve.solver.orchestrator import Orchestrator, OrchestratorRunResult
from alphasolve.solver.project import ProjectLayout
from alphasolve.solver.role import Role
from alphasolve.solver.subagent_service import SubagentService


@pytest.mark.parametrize("failure_kind", ["quota", "auth", "configuration"])
def test_curator_fatal_error_stops_run_without_retry(tmp_path, failure_kind):
    stop_event = threading.Event()
    queue = CuratorQueue(
        knowledge_dir=tmp_path / "knowledge",
        workspace_dir=tmp_path,
        suite=SimpleNamespace(settings={}),
        client_factory=lambda _config: None,
        stop_event=stop_event,
    )
    queue._digest_batch_window = 0
    calls = []
    failure = AgentRunError("cannot continue", trace=[], failure_kind=failure_kind)

    def fail(task):
        calls.append(task.source_label)
        raise failure

    queue._run_curator = fail
    queue._queue.put(CuratorTask([], "first"))
    queue._queue.put(CuratorTask([], "next"))
    queue._worker()

    assert stop_event.is_set()
    assert queue.fatal_error is failure
    assert calls == ["first"]
    assert not queue.has_active_task()
    record = json.loads((tmp_path / "curation_records" / "curator_failures.jsonl").read_text(encoding="utf-8"))
    assert record["failure_kind"] == failure_kind


def test_curator_runtime_failure_keeps_existing_retry(tmp_path):
    stop_event = threading.Event()
    queue = CuratorQueue(
        knowledge_dir=tmp_path / "knowledge",
        workspace_dir=tmp_path,
        suite=SimpleNamespace(settings={}),
        client_factory=lambda _config: None,
        stop_event=stop_event,
    )
    queue._digest_batch_window = 0
    attempts = []

    def run(task):
        attempts.append(task.attempts)
        if len(attempts) == 1:
            raise RuntimeError("temporary tool failure")
        queue._queue.put(None)

    queue._run_curator = run
    queue._queue.put(CuratorTask([], "digest"))
    queue._worker()

    assert attempts == [0, 1]
    assert not stop_event.is_set()
    assert queue.fatal_error is None


def test_subagent_quota_failure_remains_a_tool_error():
    """普通委派失败沿用工具错误路径，不扩大为全局停止。"""
    stop_event = threading.Event()
    service = SubagentService(
        suite=SimpleNamespace(subagents={}), client_factory=lambda _config: None,
        stop_event=stop_event,
    )

    def fail(*_args, **_kwargs):
        raise AgentRunError("subscription quota exhausted", trace=[], failure_kind="quota")

    service._run = fail
    result = service.call_tool({"type": "reasoning_subagent", "prompt": "Check this claim."})

    assert result.is_error
    assert "quota exhausted" in result.content
    assert not stop_event.is_set()


def test_orchestrator_quota_error_stops_workers_and_closes_session(tmp_path, monkeypatch):
    captured = {}

    class Manager:
        def __init__(self, *, stop_event, **_kwargs):
            captured["worker_stop"] = stop_event
            self.results = []
            self.solution_path = None
            self.solved_result = None

        def close(self, *, graceful=False):
            captured["graceful"] = graceful
            captured["stopped_at_close"] = captured["worker_stop"].is_set()

    class QuotaAgent:
        def run(self, _task):
            raise AgentRunError(
                "subscription quota exhausted", failure_kind="quota",
                trace=[{"type": "run_error", "failure_kind": "quota"}],
            )

        def close(self):
            captured["agent_closed"] = True

    monkeypatch.setattr(orchestrator_module, "WorkerManager", Manager)
    monkeypatch.setattr(Orchestrator, "build_agent", lambda *_args, **_kwargs: QuotaAgent())
    (tmp_path / "problem.md").write_text("Prove the statement.", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    stop_event = threading.Event()
    suite = SimpleNamespace(
        agents={"orchestrator": AgentConfig(name="orchestrator", system_prompt="Plan the proof.", tools=())},
        subagents={}, settings={},
    )
    orchestrator = Orchestrator(
        layout=layout, suite=suite, client_factory=lambda _config: None,
        stop_event=stop_event,
        cold_start_runtime=SimpleNamespace(
            prepare=lambda _manager: [], context_for_orchestrator=lambda: "",
        ),
    )
    result = orchestrator.run()

    assert stop_event.is_set()
    assert captured["stopped_at_close"]
    assert captured["graceful"] is False
    assert captured["agent_closed"]
    assert result.final_answer == "subscription quota exhausted"
    assert result.trace[0]["failure_kind"] == "quota"


@pytest.mark.parametrize("fails", [False, True])
def test_worker_role_always_closes_its_codex_session(fails):
    captured = []

    class Session:
        def run(self, *_args, **_kwargs):
            if fails:
                raise AgentRunError("failed", trace=[])
            return AgentRunResult(final_answer="saved", messages=[], trace=[], turns=1)

        def close(self):
            captured.append("closed")

    role = Role(name="generator", agent=Session(), trace_sink=[])
    if fails:
        with pytest.raises(AgentRunError):
            role.run("task")
    else:
        assert role.run("task").final_answer == "saved"
    assert captured == ["closed"]


def test_curator_failure_preserves_completed_results_and_prevents_restart(tmp_path, monkeypatch):
    captured = {"runs": 0}
    failure = AgentRunError("subscription quota exhausted", trace=[], failure_kind="quota")

    class Queue:
        fatal_error = None

        def __init__(self, *, stop_event, **_kwargs):
            captured["queue"] = self
            self.stop_event = stop_event

        def start(self):
            pass

        def stop(self):
            captured["queue_closed"] = True

        def submit(self, _task):
            pass

    completed = SimpleNamespace(
        worker_id="finished-worker", worker_dir=tmp_path / "finished-worker",
        status="verified", summary="saved result", proposition_file=None,
        verified_file=None, review_file=None, theorem_check_file=None,
        difficulty_id=None, solved_problem=False,
    )

    class Runner:
        def __init__(self, **_kwargs):
            pass

        def run(self):
            captured["runs"] += 1
            captured["queue"].fatal_error = failure
            captured["queue"].stop_event.set()
            return OrchestratorRunResult(
                final_answer="interrupted", trace=[], worker_results=[completed], solution_path=None,
            )

    monkeypatch.setattr(app_module, "CuratorQueue", Queue)
    monkeypatch.setattr(app_module, "Orchestrator", Runner)
    monkeypatch.setattr(
        app_module, "load_agent_suite",
        lambda _path: SimpleNamespace(settings={}, agents={}, subagents={"curator": object()}),
    )
    (tmp_path / "problem.md").write_text("Prove the statement.", encoding="utf-8")
    app = AlphaSolve(
        project_dir=tmp_path, client_factory=lambda _config: None,
        prime_wolfram=False, print_to_console=False,
        execution_gateway=SimpleNamespace(),
    )
    result = app.run()

    assert captured["runs"] == 1
    assert captured["queue_closed"]
    assert result.worker_results == [completed]
    assert "curator stopped" in result.final_answer
    persisted = json.loads((app.layout.logs_dir / "worker_results.json").read_text(encoding="utf-8"))
    assert persisted[0]["worker_id"] == "finished-worker"
    trace = json.loads((app.layout.logs_dir / "orchestrator_trace.json").read_text(encoding="utf-8"))
    assert trace[-1]["failure_kind"] == "quota"
