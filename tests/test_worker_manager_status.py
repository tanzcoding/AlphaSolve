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
        assert "current agent:" in blocked["active_workers"][0]["progress"]
        assert "first branch" not in blocked["active_workers"][0]["progress"]
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
        def __init__(self, *, stop_event, **_kwargs):
            captured["stop_event"] = stop_event

        def run(self):
            started.set()
            deadline = time.time() + 2
            while time.time() < deadline:
                if captured["stop_event"].is_set():
                    break
                time.sleep(0.01)
            captured["stop_seen"] = captured["stop_event"].is_set()
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
            if captured.get("stop_seen"):
                break
            time.sleep(0.01)
        assert captured["stop_event"].is_set()
        assert captured["stop_seen"] is True
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
        assert "current agent:" in payload["active_workers"][0]["progress"]
        assert "second branch" not in payload["active_workers"][0]["progress"]
    finally:
        manager.close(timeout=0)


def test_task_output_syncs_changed_root_hint_and_reports_update(tmp_path, monkeypatch):
    (tmp_path / "hint.md").write_text("initial hint\n", encoding="utf-8")
    manager = _manager(tmp_path, monkeypatch, max_workers=1)
    try:
        (tmp_path / "hint.md").write_text("expert hint\n", encoding="utf-8")

        payload = manager.wait(timeout_seconds=0)

        assert (manager.layout.workspace_dir / "hint.md").read_text(encoding="utf-8") == "expert hint\n"
        updates = payload["human_expert_updates"]
        assert updates["hint_md_updated"] is True
        assert updates["new_reference_files"] == []
        assert "Read hint.md" in updates["message"]
    finally:
        manager.close(timeout=0)


def test_task_output_reports_new_reference_files_once(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch, max_workers=1)
    try:
        references_dir = manager.layout.knowledge_dir / "references"
        references_dir.mkdir(parents=True)
        (references_dir / "expert-note.md").write_text("# Expert Note\n", encoding="utf-8")

        first = manager.wait(timeout_seconds=0)
        second = manager.wait(timeout_seconds=0)

        assert first["human_expert_updates"]["new_reference_files"] == [
            "knowledge/references/expert-note.md"
        ]
        assert "human_expert_updates" not in second
    finally:
        manager.close(timeout=0)


def test_task_output_omits_verified_organization_prompt_at_directory_threshold(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch, max_workers=1)
    try:
        for index in range(WorkerManager.VERIFIED_PROPOSITIONS_ORGANIZATION_THRESHOLD):
            (manager.layout.verified_dir / f"lemma-{index:02d}.md").write_text(
                f"# Lemma {index}\n",
                encoding="utf-8",
            )

        payload = manager.wait(timeout_seconds=0)

        assert "verified_propositions_organization" not in payload
    finally:
        manager.close(timeout=0)


def test_task_output_omits_verified_organization_prompt_when_recursive_total_is_high(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch, max_workers=1)
    try:
        first_dir = manager.layout.verified_dir / "first"
        second_dir = manager.layout.verified_dir / "second"
        first_dir.mkdir()
        second_dir.mkdir()
        for index in range(15):
            (first_dir / f"lemma-{index:02d}.md").write_text(
                f"# Lemma {index}\n",
                encoding="utf-8",
            )
            (second_dir / f"lemma-{index:02d}.md").write_text(
                f"# Lemma {index}\n",
                encoding="utf-8",
            )

        payload = manager.wait(timeout_seconds=0)

        assert "verified_propositions_organization" not in payload
    finally:
        manager.close(timeout=0)


def test_task_output_prompts_verified_organization_for_overloaded_direct_directory(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch, max_workers=1)
    try:
        topic_dir = manager.layout.verified_dir / "topic"
        topic_dir.mkdir()
        for index in range(WorkerManager.VERIFIED_PROPOSITIONS_ORGANIZATION_THRESHOLD + 1):
            (topic_dir / f"lemma-{index:02d}.md").write_text(
                f"# Lemma {index}\n",
                encoding="utf-8",
            )
        for index in range(10):
            (manager.layout.verified_dir / f"root-lemma-{index:02d}.md").write_text(
                f"# Root Lemma {index}\n",
                encoding="utf-8",
            )

        payload = manager.wait(timeout_seconds=0)

        organization = payload["verified_propositions_organization"]
        assert organization["threshold"] == WorkerManager.VERIFIED_PROPOSITIONS_ORGANIZATION_THRESHOLD
        assert organization["directories"] == [
            {
                "path": "verified_propositions/topic",
                "markdown_file_count": WorkerManager.VERIFIED_PROPOSITIONS_ORGANIZATION_THRESHOLD + 1,
            }
        ]
        assert organization["message"] == (
            "Organize these verified_propositions directories before spawning more work: "
            "verified_propositions/topic"
        )
    finally:
        manager.close(timeout=0)


def test_task_output_ignores_curator_touched_reference_files(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch, max_workers=1)
    try:
        references_dir = manager.layout.knowledge_dir / "references"
        references_dir.mkdir(parents=True)
        curator_file = references_dir / "renamed-by-curator.md"
        curator_file.write_text("# Curator renamed note\n", encoding="utf-8")

        class FakeCuratorQueue:
            def touched_paths(self):
                return (curator_file,)

        manager.injection_monitor.curator_queue = FakeCuratorQueue()

        payload = manager.wait(timeout_seconds=0)

        assert "human_expert_updates" not in payload
    finally:
        manager.close(timeout=0)


def test_task_output_defers_reference_scan_while_curator_is_active(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch, max_workers=1)
    try:
        references_dir = manager.layout.knowledge_dir / "references"
        references_dir.mkdir(parents=True)
        expert_file = references_dir / "expert-note.md"
        expert_file.write_text("# Expert Note\n", encoding="utf-8")

        class ActiveCuratorQueue:
            active = True

            def has_active_task(self):
                return self.active

            def touched_paths(self):
                return ()

        active_queue = ActiveCuratorQueue()
        manager.injection_monitor.curator_queue = active_queue

        during_curator = manager.wait(timeout_seconds=0)
        active_queue.active = False
        after_curator = manager.wait(timeout_seconds=0)

        assert "human_expert_updates" not in during_curator
        assert after_curator["human_expert_updates"]["new_reference_files"] == [
            "knowledge/references/expert-note.md"
        ]
    finally:
        manager.close(timeout=0)


def test_active_worker_progress_reports_current_agent_and_round(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch, max_workers=1)
    try:
        manager.spawn("do not leak this hint")

        manager._update_worker_progress("w1", "verifier_attempt w2.3", "thinking")
        payload = manager._pool_status()

        progress = payload["active_workers"][0]["progress"]
        assert "current agent: verifier, round 2, attempt 3" in progress
        assert "do not leak this hint" not in progress

        manager._update_worker_progress("w1", "reviser w2", "thinking")
        progress = manager._pool_status()["active_workers"][0]["progress"]
        assert "current agent: reviser, round 2" in progress
    finally:
        manager.close(timeout=0)
