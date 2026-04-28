import threading
import time
from types import SimpleNamespace

from alphasolve.agents.general import GeneralAgentConfig
from alphasolve.agents.team import orchestrator as orchestrator_module
from alphasolve.agents.team.orchestrator import Orchestrator, WorkerManager
from alphasolve.agents.team.project import ProjectLayout
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
