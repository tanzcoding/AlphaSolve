"""使用真实子进程验证 Python 会话的隔离、截止时间和关闭行为。"""

from __future__ import annotations

from concurrent.futures import Future
import json
import multiprocessing as mp
from pathlib import Path
import threading
import time

import pytest

from alphasolve.solver.execution.gateway import ExecutionGateway, ExecutionOutput


class _ObservedStopEvent(threading.Event):
    """确认请求已经检查停止信号，避免靠固定睡眠猜测排队时机。"""

    def __init__(self) -> None:
        super().__init__()
        self.checked = threading.Event()

    def is_set(self) -> bool:
        self.checked.set()
        return super().is_set()


@pytest.fixture
def gateway_factory():
    gateways: list[ExecutionGateway] = []

    def create(*, workers: int = 2) -> ExecutionGateway:
        gateway = ExecutionGateway(python_workers=workers, wolfram_enabled=False)
        gateways.append(gateway)
        return gateway

    yield create

    for gateway in gateways:
        gateway.close()


def _async_call(function, *args, **kwargs) -> Future:
    result: Future = Future()

    def invoke() -> None:
        result.set_running_or_notify_cancel()
        try:
            result.set_result(function(*args, **kwargs))
        except BaseException as exc:
            result.set_exception(exc)

    threading.Thread(target=invoke, daemon=True).start()
    return result


def _wait_for_file(path: Path, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    pause = threading.Event()
    while not path.exists():
        assert time.monotonic() < deadline, f"子进程没有写入同步标记：{path}"
        pause.wait(0.01)


def _identity(gateway: ExecutionGateway, session_id: str) -> dict:
    result = gateway.run_python(
        session_id=session_id,
        code="import json, os\nprint(json.dumps({'pid': os.getpid(), 'cwd': os.getcwd()}))",
        allow_filesystem=True,
        timeout_seconds=10,
    )
    assert "[error]" not in result.tool_content, result.tool_content
    return json.loads(result.tool_content.removeprefix("[output]\n").strip())


def _assert_error(result: ExecutionOutput, *words: str) -> None:
    assert "[error]" in result.tool_content, result.tool_content
    for word in words:
        assert word.lower() in result.tool_content.lower(), result.tool_content


def _assert_value(result: ExecutionOutput, value: str) -> None:
    assert "[error]" not in result.tool_content, result.tool_content
    assert result.tool_content.removeprefix("[output]\n").strip() == value


def _hold_code(started: Path, released: Path, *, before: str = "", after: str = "") -> str:
    return (
        "from pathlib import Path\nimport time\n"
        f"{before}\n"
        f"Path({str(started)!r}).write_text('started')\n"
        f"while not Path({str(released)!r}).exists():\n    time.sleep(0.01)\n"
        f"{after}\n"
    )


def _alive_child_pids() -> set[int]:
    return {process.pid for process in mp.active_children() if process.is_alive()}


def test_python_sessions_have_distinct_processes_directories_and_persistent_state(gateway_factory):
    gateway = gateway_factory(workers=1)
    first = _identity(gateway, "first")
    second = _identity(gateway, "second")

    assert first["pid"] != second["pid"]
    assert first["cwd"] != second["cwd"]
    _assert_value(gateway.run_python(session_id="first", code="values = [1]\nvalues"), "[1]")
    _assert_value(gateway.run_python(session_id="second", code="'values' in globals()"), "False")
    _assert_value(gateway.run_python(session_id="first", code="values.append(2)\nvalues"), "[1, 2]")
    assert _identity(gateway, "first") == first


def test_python_exceptions_and_system_exit_preserve_a_usable_session(gateway_factory):
    gateway = gateway_factory()
    identity = _identity(gateway, "exceptions")

    for exception in ("ValueError", "SystemExit", "KeyboardInterrupt"):
        result = gateway.run_python(
            session_id="exceptions", code=f"saved = {exception!r}\nraise {exception}('intentional')"
        )
        _assert_error(result, exception)
        _assert_value(gateway.run_python(session_id="exceptions", code="print(saved)"), exception)
        assert _identity(gateway, "exceptions") == identity


def test_python_hard_exit_is_detected_promptly_and_only_resets_its_session(gateway_factory):
    gateway = gateway_factory(workers=1)
    original = _identity(gateway, "crash")
    survivor = _identity(gateway, "survivor")
    gateway.run_python(session_id="crash", code="saved = 99")
    gateway.run_python(session_id="survivor", code="saved = 42")

    started = time.monotonic()
    result = gateway.run_python(
        session_id="crash", code="import os\nos._exit(7)", allow_filesystem=True, timeout_seconds=15
    )

    _assert_error(result, "reset")
    assert time.monotonic() - started < 5
    assert original["pid"] not in _alive_child_pids()
    assert not Path(original["cwd"]).exists()
    _assert_value(gateway.run_python(session_id="crash", code="'saved' in globals()"), "False")
    _assert_value(gateway.run_python(session_id="crash", code="2 + 2"), "4")
    _assert_value(gateway.run_python(session_id="survivor", code="saved"), "42")
    assert _identity(gateway, "survivor") == survivor


@pytest.mark.parametrize("code", ["while True:\n    pass", "import time\ntime.sleep(60)"])
def test_python_deadline_terminates_python_and_native_waits_and_resets_state(gateway_factory, code):
    gateway = gateway_factory()
    original = _identity(gateway, "timeout")
    gateway.run_python(session_id="timeout", code="saved = 99")

    started = time.monotonic()
    result = gateway.run_python(session_id="timeout", code=code, timeout_seconds=1)

    _assert_error(result, "timeout", "reset")
    assert time.monotonic() - started < 4
    assert original["pid"] not in _alive_child_pids()
    assert not Path(original["cwd"]).exists()
    _assert_value(gateway.run_python(session_id="timeout", code="'saved' in globals()"), "False")
    _assert_value(gateway.run_python(session_id="timeout", code="2 + 2"), "4")


def test_python_execution_does_not_install_a_line_trace(gateway_factory):
    gateway = gateway_factory()
    _assert_value(
        gateway.run_python(session_id="trace", code="import sys\nprint(sys.gettrace())"), "None"
    )


def test_capacity_limits_parallel_execution_and_queue_wait_counts_towards_deadline(
    gateway_factory, tmp_path
):
    gateway = gateway_factory(workers=2)
    for session_id in ("first", "second", "queued"):
        _identity(gateway, session_id)
    release = tmp_path / "release"
    calls = [
        _async_call(
            gateway.run_python,
            session_id=session_id,
            code=_hold_code(tmp_path / session_id, release),
            allow_filesystem=True,
            timeout_seconds=10,
        )
        for session_id in ("first", "second")
    ]
    for session_id in ("first", "second"):
        _wait_for_file(tmp_path / session_id)

    queued_marker = tmp_path / "queued"
    started = time.monotonic()
    result = gateway.run_python(
        session_id="queued",
        code=f"from pathlib import Path\nPath({str(queued_marker)!r}).write_text('unexpected')",
        allow_filesystem=True,
        timeout_seconds=1,
    )

    _assert_error(result, "timeout")
    assert time.monotonic() - started < 4
    assert not queued_marker.exists()
    assert all(not call.done() for call in calls)
    release.touch()
    for call in calls:
        assert "[error]" not in call.result(timeout=5).tool_content
    _assert_value(gateway.run_python(session_id="queued", code="2 + 2"), "4")
    assert not queued_marker.exists()


def test_requests_for_one_session_are_serialized_without_lost_state(gateway_factory, tmp_path):
    gateway = gateway_factory(workers=2)
    _identity(gateway, "serial")
    started = tmp_path / "started"
    release = tmp_path / "release"
    first = _async_call(
        gateway.run_python,
        session_id="serial",
        code=_hold_code(started, release, before="values = ['first']", after="values.append('done')"),
        allow_filesystem=True,
        timeout_seconds=10,
    )
    _wait_for_file(started)
    stop = _ObservedStopEvent()
    second = _async_call(
        gateway.run_python,
        session_id="serial",
        code="values.append('second')\nvalues",
        stop_event=stop,
        timeout_seconds=10,
    )
    assert stop.checked.wait(3)
    assert not second.done()

    release.touch()
    assert "[error]" not in first.result(timeout=5).tool_content
    _assert_value(second.result(timeout=5), "['first', 'done', 'second']")


@pytest.mark.parametrize("queued_session", ["busy", "another"])
def test_stop_event_cancels_requests_waiting_for_session_or_capacity(
    gateway_factory, tmp_path, queued_session
):
    gateway = gateway_factory(workers=1)
    identity = _identity(gateway, "busy")
    started = tmp_path / "started"
    release = tmp_path / "release"
    running = _async_call(
        gateway.run_python,
        session_id="busy",
        code=_hold_code(started, release, before="saved = 42"),
        allow_filesystem=True,
        timeout_seconds=10,
    )
    _wait_for_file(started)
    stop = _ObservedStopEvent()
    queued_marker = tmp_path / "queued"
    queued = _async_call(
        gateway.run_python,
        session_id=queued_session,
        code=f"from pathlib import Path\nPath({str(queued_marker)!r}).write_text('unexpected')",
        allow_filesystem=True,
        stop_event=stop,
        timeout_seconds=10,
    )
    assert stop.checked.wait(3)
    stop.set()

    _assert_error(queued.result(timeout=3), "cancel")
    assert not queued_marker.exists()
    assert not running.done()
    release.touch()
    assert "[error]" not in running.result(timeout=5).tool_content
    _assert_value(gateway.run_python(session_id="busy", code="saved"), "42")
    assert _identity(gateway, "busy") == identity


def test_stop_event_terminates_running_native_wait_and_releases_capacity(gateway_factory, tmp_path):
    gateway = gateway_factory(workers=1)
    original = _identity(gateway, "cancel")
    marker = tmp_path / "started"
    stop = threading.Event()
    running = _async_call(
        gateway.run_python,
        session_id="cancel",
        code=f"from pathlib import Path\nimport time\nPath({str(marker)!r}).touch()\ntime.sleep(60)",
        allow_filesystem=True,
        stop_event=stop,
        timeout_seconds=15,
    )
    _wait_for_file(marker)
    stop.set()

    _assert_error(running.result(timeout=3), "cancel", "reset")
    assert original["pid"] not in _alive_child_pids()
    _assert_value(gateway.run_python(session_id="cancel", code="2 + 2"), "4")
    _assert_value(gateway.run_python(session_id="other", code="3 + 3"), "6")


def test_close_session_cancels_running_and_queued_requests_and_cleans_state(gateway_factory, tmp_path):
    gateway = gateway_factory(workers=1)
    original = _identity(gateway, "closed-session")
    marker = tmp_path / "started"
    running = _async_call(
        gateway.run_python,
        session_id="closed-session",
        code=_hold_code(marker, tmp_path / "never", before="saved = 99"),
        allow_filesystem=True,
        timeout_seconds=15,
    )
    _wait_for_file(marker)
    stop = _ObservedStopEvent()
    queued = _async_call(
        gateway.run_python,
        session_id="closed-session",
        code="saved = 'unexpected'",
        stop_event=stop,
        timeout_seconds=15,
    )
    assert stop.checked.wait(3)

    _async_call(gateway.close_session, "closed-session").result(timeout=3)

    _assert_error(running.result(timeout=3))
    _assert_error(queued.result(timeout=3))
    assert original["pid"] not in _alive_child_pids()
    assert not Path(original["cwd"]).exists()
    _assert_value(gateway.run_python(session_id="closed-session", code="'saved' in globals()"), "False")
    fresh = _identity(gateway, "closed-session")
    assert fresh["cwd"] != original["cwd"]


def test_close_joins_its_processes_unblocks_requests_and_cannot_resurrect(gateway_factory, tmp_path):
    gateway = gateway_factory(workers=1)
    original = _identity(gateway, "running")
    idle = _identity(gateway, "idle")
    marker = tmp_path / "started"
    running = _async_call(
        gateway.run_python,
        session_id="running",
        code=_hold_code(marker, tmp_path / "never"),
        allow_filesystem=True,
        timeout_seconds=15,
    )
    _wait_for_file(marker)
    stop = _ObservedStopEvent()
    queued = _async_call(
        gateway.run_python,
        session_id="queued",
        code="42",
        stop_event=stop,
        timeout_seconds=15,
    )
    assert stop.checked.wait(3)

    closes = [_async_call(gateway.close) for _ in range(2)]
    for close in closes:
        close.result(timeout=3)

    _assert_error(running.result(timeout=3))
    _assert_error(queued.result(timeout=3))
    assert not {original["pid"], idle["pid"]} & _alive_child_pids()
    assert not Path(original["cwd"]).exists()
    assert not Path(idle["cwd"]).exists()
    _assert_error(gateway.run_python(session_id="running", code="42"), "closed")
    _assert_error(gateway.run_python(session_id="new", code="42"), "closed")
    gateway.close_session("running")
    gateway.close()


def test_close_before_first_request_does_not_start_a_process(gateway_factory):
    gateway = gateway_factory()
    gateway.close()

    _assert_error(gateway.run_python(session_id="never-started", code="42"), "closed")


@pytest.mark.parametrize("phase", ["startup", "request_write"])
@pytest.mark.parametrize("cause", ["cancel", "timeout"])
def test_expiration_before_dispatch_never_sends_user_code(gateway_factory, monkeypatch, phase, cause):
    from types import SimpleNamespace

    from alphasolve.solver.execution import python_sessions

    gateway = gateway_factory()
    stop = threading.Event()
    expired = threading.Event()
    clock_offset = [0.0]
    sent_requests: list[bool] = []
    original_monotonic = time.monotonic
    original_start = python_sessions._PythonSession._start
    original_write_text = Path.write_text

    def expire() -> None:
        if cause == "cancel":
            stop.set()
        else:
            clock_offset[0] += 60
        expired.set()

    def start_and_observe(session) -> None:
        original_start(session)
        original_set = session._request_ready.set

        def observe_dispatch() -> None:
            sent_requests.append(True)
            original_set()

        monkeypatch.setattr(session._request_ready, "set", observe_dispatch)
        if phase == "startup":
            expire()

    def write_and_expire(path, *args, **kwargs):
        result = original_write_text(path, *args, **kwargs)
        if phase == "request_write" and path.name == "request.json":
            expire()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(
            python_sessions, "time",
            SimpleNamespace(monotonic=lambda: original_monotonic() + clock_offset[0]),
        )
        patch.setattr(python_sessions._PythonSession, "_start", start_and_observe)
        patch.setattr(Path, "write_text", write_and_expire)
        result = gateway.run_python(
            session_id="before-dispatch", code="saved = 'must not execute'",
            timeout_seconds=10, stop_event=stop,
        )

    assert expired.is_set()
    _assert_error(result, cause, "code was not run")
    assert sent_requests == []
    _assert_value(gateway.run_python(session_id="before-dispatch", code="'saved' in globals()"), "False")


@pytest.mark.parametrize("cause", ["cancel", "timeout"])
def test_expiration_during_result_read_resets_session_instead_of_returning_success(
    gateway_factory, monkeypatch, cause
):
    from types import SimpleNamespace

    from alphasolve.solver.execution import python_sessions

    gateway = gateway_factory()
    identity = _identity(gateway, "during-read")
    gateway.run_python(session_id="during-read", code="saved = 99")
    stop = threading.Event()
    expired = threading.Event()
    clock_offset = [0.0]
    original_monotonic = time.monotonic
    original_read_text = Path.read_text

    def read_and_expire(path, *args, **kwargs):
        result = original_read_text(path, *args, **kwargs)
        if path.name == "result.json":
            if cause == "cancel":
                stop.set()
            else:
                clock_offset[0] += 60
            expired.set()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(
            python_sessions, "time",
            SimpleNamespace(monotonic=lambda: original_monotonic() + clock_offset[0]),
        )
        patch.setattr(Path, "read_text", read_and_expire)
        result = gateway.run_python(
            session_id="during-read", code="2 + 2", timeout_seconds=10, stop_event=stop,
        )

    assert expired.is_set()
    _assert_error(result, cause, "reset")
    assert identity["pid"] not in _alive_child_pids()
    assert not Path(identity["cwd"]).exists()
    _assert_value(gateway.run_python(session_id="during-read", code="'saved' in globals()"), "False")


def test_global_close_waits_for_session_resource_reclamation(gateway_factory, monkeypatch):
    gateway = gateway_factory()
    identity = _identity(gateway, "closing")
    executor = gateway._python
    session = executor._sessions["closing"]
    original_close = session.close
    original_lock = executor._lock
    cleanup_entered = threading.Event()
    allow_cleanup = threading.Event()
    global_lock_attempted = threading.Event()
    global_lock_blocked = threading.Event()
    global_thread_id: list[int] = []

    class ObservedRegistryLock:
        """观察全局关闭能否越过回收屏障，同时保留真实锁的阻塞行为。"""

        def __enter__(self):
            if global_thread_id and threading.get_ident() == global_thread_id[0]:
                acquired = original_lock.acquire(blocking=False)
                if not acquired:
                    global_lock_blocked.set()
                global_lock_attempted.set()
                if not acquired:
                    original_lock.acquire()
            else:
                original_lock.acquire()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            original_lock.release()

    def pause_session_cleanup() -> None:
        cleanup_entered.set()
        assert allow_cleanup.wait(5), "测试没有解除会话回收屏障"
        original_close()

    def close_globally() -> None:
        global_thread_id.append(threading.get_ident())
        gateway.close()

    monkeypatch.setattr(executor, "_lock", ObservedRegistryLock())
    monkeypatch.setattr(session, "close", pause_session_cleanup)
    close_session = _async_call(gateway.close_session, "closing")
    close_all = None
    try:
        assert cleanup_entered.wait(3)
        close_all = _async_call(close_globally)
        assert global_lock_attempted.wait(3)
        assert global_lock_blocked.is_set()
        assert not close_all.done()
        assert Path(identity["cwd"]).exists()
    finally:
        allow_cleanup.set()
        close_session.result(timeout=5)
        if close_all is not None:
            close_all.result(timeout=5)

    assert identity["pid"] not in _alive_child_pids()
    assert not Path(identity["cwd"]).exists()
    assert not executor._root.exists()
