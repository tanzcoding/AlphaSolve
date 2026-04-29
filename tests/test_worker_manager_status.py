import threading
import time
from types import SimpleNamespace

from alphasolve.agents.general import GeneralAgentConfig
from alphasolve.agents.team import orchestrator as orchestrator_module
from alphasolve.agents.team import workflow as workflow_module
from alphasolve.agents.team import AlphaSolve
from alphasolve.agents.team.orchestrator import Orchestrator, WorkerManager
from alphasolve.agents.team.project import ProjectLayout
from alphasolve.agents.team.orchestrator import OrchestratorRunResult
from alphasolve.agents.team.worker import WorkerRunResult


class _DummyWorker:
    created = []
    release_events = {}

    def __init__(self, *, layout, worker_hint=None, **_kwargs):
        self.worker_id = f"w{len(self.created) + 1}"
        self.worker_hint = worker_hint
        self.worker_dir = layout.unverified_dir / f"prop-{self.worker_id}"
        self.stop_event = _kwargs.get("stop_event")
        self.created.append(self)
        self.release_events[self.worker_id] = threading.Event()

    def run(self):
        self.release_events[self.worker_id].wait(timeout=5)
        return WorkerRunResult(
            worker_id=self.worker_id,
            worker_dir=self.worker_dir,
            status="rejected",
            summary=f"finished {self.worker_id}",
        )


def _manager(tmp_path, monkeypatch, *, max_workers=2):
    _DummyWorker.created = []
    _DummyWorker.release_events = {}
    monkeypatch.setattr(orchestrator_module, "Worker", _DummyWorker)
    (tmp_path / "problem.md").write_text("# Problem\n\nTest.\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    return WorkerManager(
        layout=layout,
        suite=object(),
        client_factory=lambda _config: None,
        max_workers=max_workers,
        max_verify_rounds=1,
        verifier_scaling_factor=1,
        subagent_max_depth=0,
    )


def _drain_manager(manager, worker_id, *, timeout=1.0):
    _DummyWorker.release_events[worker_id].set()
    deadline = time.time() + timeout
    while time.time() < deadline:
        manager._collect_done()
        if not manager.active:
            return
        time.sleep(0.01)
    raise AssertionError("worker did not finish before cleanup deadline")


def test_spawn_reports_active_workers_and_enforces_limit(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch, max_workers=2)
    try:
        first = manager.spawn("first branch")
        second = manager.spawn("second branch")
        blocked = manager.spawn("third branch")

        assert first["spawned"] is True
        assert second["spawned"] is True
        assert blocked["spawned"] is False
        assert blocked["reason"] == "parallelism_limit_reached"
        assert blocked["active_count"] == 2
        assert blocked["active_worker_ids"] == ["w1", "w2"]
        assert blocked["available_worker_slots"] == 0
        assert [item["worker_id"] for item in blocked["active_workers"]] == ["w1", "w2"]
        assert "first branch" in blocked["active_workers"][0]["progress"]
    finally:
        manager.close(timeout=0)


def test_graceful_close_waits_for_workers_without_setting_stop_event(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch, max_workers=1)
    try:
        manager.spawn("finish cleanly")
        worker = _DummyWorker.created[0]
        releaser = threading.Timer(0.05, _DummyWorker.release_events["w1"].set)
        releaser.start()

        manager.close(graceful=True)
        releaser.join(timeout=1)

        assert worker.stop_event is not None
        assert not worker.stop_event.is_set()
        assert manager.active == {}
        assert [item.worker_id for item in manager.results] == ["w1"]
    finally:
        manager.close(timeout=0)


def test_graceful_close_timeout_escalates_to_worker_stop_without_blocking_forever(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch, max_workers=1)
    try:
        manager.spawn("hang briefly")
        worker = _DummyWorker.created[0]

        started = time.perf_counter()
        manager.close(timeout=0.05, graceful=True)
        elapsed = time.perf_counter() - started

        assert worker.stop_event is not None
        assert worker.stop_event.is_set()
        assert elapsed < 0.5
        assert list(manager.active.values()) == ["w1"]
        assert manager.results == []
    finally:
        if "w1" in _DummyWorker.release_events:
            _drain_manager(manager, "w1")
        manager.close(timeout=0)


def test_orchestrator_interrupt_uses_separate_worker_stop_event(tmp_path, monkeypatch):
    captured = {}

    class CapturingManager:
        DEFAULT_WAIT_TIMEOUT_SECONDS = 3600.0

        def __init__(self, *, stop_event, **_kwargs):
            self.stop_event = stop_event
            self.results = []
            self.solution_path = None
            self.solved_result = None
            captured["worker_stop_event"] = stop_event

        def close(self, *, timeout=5.0, graceful=False):
            captured["close_graceful"] = graceful
            captured["worker_stop_set_during_close"] = self.stop_event.is_set()

    monkeypatch.setattr(orchestrator_module, "WorkerManager", CapturingManager)
    (tmp_path / "problem.md").write_text("# Problem\n\nTest.\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    user_stop_event = threading.Event()
    user_stop_event.set()
    suite = SimpleNamespace(
        agents={
            "orchestrator": GeneralAgentConfig(
                name="orchestrator",
                system_prompt="Stop immediately.",
                tools=[],
                max_turns=1,
            )
        },
        subagents={},
        models={},
    )
    orchestrator = Orchestrator(
        layout=layout,
        suite=suite,
        client_factory=lambda _config: None,
        max_workers=1,
        max_verify_rounds=1,
        verifier_scaling_factor=1,
        subagent_max_depth=0,
        stop_event=user_stop_event,
    )

    orchestrator.run()

    assert captured["worker_stop_event"] is not user_stop_event
    assert captured["close_graceful"] is True
    assert captured["worker_stop_set_during_close"] is False


def test_alphasolve_cancel_stops_orchestrator_and_current_workers(tmp_path, monkeypatch):
    captured = {}
    started = threading.Event()
    allow_return = threading.Event()

    class CapturingOrchestrator:
        def __init__(self, *, stop_event, worker_stop_event, **_kwargs):
            captured["stop_event"] = stop_event
            captured["worker_stop_event"] = worker_stop_event

        def run(self):
            started.set()
            deadline = time.time() + 2
            while time.time() < deadline:
                if captured["stop_event"].is_set() and captured["worker_stop_event"].is_set():
                    break
                time.sleep(0.01)
            captured["stop_seen"] = captured["stop_event"].is_set()
            captured["worker_stop_seen"] = captured["worker_stop_event"].is_set()
            allow_return.wait(timeout=1)
            return OrchestratorRunResult(final_answer="", trace=[], worker_results=[], solution_path=None)

    monkeypatch.setattr(workflow_module, "Orchestrator", CapturingOrchestrator)
    monkeypatch.setattr(
        workflow_module,
        "load_agent_suite_config",
        lambda _path: SimpleNamespace(settings={}, subagents={}, agents={}, models={}),
    )
    (tmp_path / "problem.md").write_text("# Problem\n\nTest.\n", encoding="utf-8")

    class DummyExecutionGateway:
        def close(self):
            return None

    app = AlphaSolve(
        project_dir=tmp_path,
        max_workers=1,
        prime_wolfram=False,
        print_to_console=False,
        client_factory=lambda _config: None,
        execution_gateway=DummyExecutionGateway(),
    )
    thread = threading.Thread(target=app.run)
    thread.start()
    try:
        assert started.wait(timeout=1)
        app.cancel()
        deadline = time.time() + 1
        while time.time() < deadline:
            if captured.get("stop_seen") and captured.get("worker_stop_seen"):
                break
            time.sleep(0.01)
        assert captured["stop_event"] is not captured["worker_stop_event"]
        assert captured["stop_event"].is_set()
        assert captured["worker_stop_event"].is_set()
        assert captured["stop_seen"] is True
        assert captured["worker_stop_seen"] is True
        # cancel() 的 worker 停止信号是退出专用的一次性开关，置位后保持为 True。
        assert app._worker_stop_event.is_set()
    finally:
        allow_return.set()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_task_output_returns_completed_result_and_remaining_active_snapshot(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch, max_workers=2)
    try:
        manager.spawn("first branch")
        manager.spawn("second branch")

        _DummyWorker.release_events["w1"].set()
        deadline = time.time() + 2
        while time.time() < deadline and manager.active:
            if any(future.done() for future in manager.active):
                break
            time.sleep(0.01)

        payload = manager.wait(timeout_seconds=0)

        assert [item["worker_id"] for item in payload["completed"]] == ["w1"]
        assert payload["active_count"] == 1
        assert payload["active_worker_ids"] == ["w2"]
        assert payload["available_worker_slots"] == 1
        assert "second branch" in payload["active_workers"][0]["progress"]
    finally:
        manager.close(timeout=0)
