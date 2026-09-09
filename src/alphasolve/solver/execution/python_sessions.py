"""由父进程监督的 Python 会话：独立解释器、并发限额与统一截止时间。"""
from __future__ import annotations

import json
import math
import multiprocessing as mp
import shutil
import signal
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .runners import evaluate_python


_POLL_SECONDS = 0.02
_RESET_MESSAGE = "Session reset; previous variables are lost. Recreate the required setup before retrying."


@dataclass(frozen=True)
class PythonResult:
    output: str = ""
    error: str | None = None
    queue_seconds: float = 0.0
    execution_seconds: float = 0.0


class PythonSessionExecutor:
    """每个会话持有解释器，限额约束正在执行的请求，不让闲置会话占用算力槽。"""

    def __init__(self, concurrency: int) -> None:
        self._context = mp.get_context("spawn")
        self._slots = threading.BoundedSemaphore(concurrency)
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._finished_closing = threading.Event()
        self._sessions: dict[str, _PythonSession] = {}
        self._storage = tempfile.TemporaryDirectory(prefix="alphasolve-exec-")
        self._root = Path(self._storage.name).resolve()

    def execute(
        self, session_id: str, code: str, timeout_seconds: float, *,
        allow_filesystem: bool, stop_event: threading.Event | None = None,
    ) -> PythonResult:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            return PythonResult(error="Python timeout_seconds must be finite and positive")
        started = time.monotonic()
        deadline = started + timeout_seconds
        with self._lock:
            if self._closed.is_set():
                return PythonResult(error="Python executor is closed")
            session = self._sessions.get(session_id)
            if session is None:
                session = _PythonSession(self._context, self._root, self._closed)
                self._sessions[session_id] = session

        # 先串行化会话，再申请全局槽位，等待同一会话的请求不会耗尽全局并发额度。
        if not session.acquire(session.lock, deadline, stop_event):
            return PythonResult(error=session.wait_error(stop_event), queue_seconds=time.monotonic() - started)
        admitted = False
        try:
            admitted = session.acquire(self._slots, deadline, stop_event)
            if not admitted:
                return PythonResult(error=session.wait_error(stop_event), queue_seconds=time.monotonic() - started)
            execution_started = time.monotonic()
            output, error = session.execute(code, allow_filesystem, deadline, stop_event)
            return PythonResult(
                output=output, error=error,
                queue_seconds=execution_started - started,
                execution_seconds=time.monotonic() - execution_started,
            )
        finally:
            if admitted:
                self._slots.release()
            session.lock.release()

    def close_session(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is not None:
                # 先废止这一代会话，已排队的请求只能取消，不能在关闭后重建旧解释器。
                session.closed.set()
                # 回收仍属于注册表生命周期，防止全局关闭先删除还在使用的根目录。
                session.close()

    def close(self) -> None:
        with self._lock:
            owner = not self._closed.is_set()
            if owner:
                self._closed.set()
                sessions = list(self._sessions.values())
                self._sessions.clear()
        if not owner:
            self._finished_closing.wait()
            return
        try:
            for session in sessions:
                session.close()
            self._storage.cleanup()
        finally:
            self._finished_closing.set()


class _PythonSession:
    """同一时间只允许一个请求使用会话，关闭操作可越过请求锁终止执行。"""

    def __init__(self, context, root: Path, shutdown: threading.Event) -> None:
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self._shutdown = shutdown
        self._context = context
        self._root = root
        self._resources = threading.Lock()
        self._process = None
        self._directory: Path | None = None
        self._request_ready = None
        self._result_ready = None

    def cancelled(self, stop_event: threading.Event | None) -> bool:
        return self.closed.is_set() or self._shutdown.is_set() or (stop_event is not None and stop_event.is_set())

    def acquire(self, lock, deadline: float, stop_event: threading.Event | None) -> bool:
        while not self.cancelled(stop_event):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if lock.acquire(timeout=min(_POLL_SECONDS, remaining)):
                if not self.cancelled(stop_event) and time.monotonic() < deadline:
                    return True
                lock.release()
                return False
        return False

    def wait_error(self, stop_event: threading.Event | None) -> str:
        if self.cancelled(stop_event):
            return "Python request cancelled or session closed before execution; code was not run"
        return "Python timeout before execution; code was not run; session state preserved"

    def _start(self) -> None:
        with self._resources:
            if self.closed.is_set() or self._shutdown.is_set():
                return
            if self._process is not None:
                return
            self._directory = Path(tempfile.mkdtemp(prefix="session-", dir=self._root))
            self._request_ready = self._context.Event()
            self._result_ready = self._context.Event()
            self._process = self._context.Process(
                target=_serve_python, args=(str(self._directory), self._request_ready, self._result_ready),
                daemon=True,
            )
            self._process.start()

    def execute(self, code: str, allow_filesystem: bool, deadline: float, stop_event) -> tuple[str, str | None]:
        if self.cancelled(stop_event) or time.monotonic() >= deadline:
            return "", self.wait_error(stop_event)
        try:
            self._start()
            with self._resources:
                if self.cancelled(stop_event) or time.monotonic() >= deadline:
                    return "", self.wait_error(stop_event)
                process = self._process
                directory = self._directory
                request_ready, result_ready = self._request_ready, self._result_ready
                # 上次完成后若解释器意外退出，本次明确报告状态丢失，不偷偷执行依赖旧变量的代码。
                if process is None or not process.is_alive():
                    raise RuntimeError(f"Python interpreter exited unexpectedly (exit code {process.exitcode if process else 'unknown'})")
                result_ready.clear()
                (directory / "request.json").write_text(
                    json.dumps({"code": code, "allow_filesystem": allow_filesystem}), encoding="utf-8",
                )
                # 启动和写入也消耗预算；派发前再检查，过期请求绝不能延迟执行。
                if self.cancelled(stop_event) or time.monotonic() >= deadline:
                    return "", self.wait_error(stop_event)
                request_ready.set()
            while True:
                if self.cancelled(stop_event):
                    self._discard()
                    return "", f"Python execution cancelled or session closed. {_RESET_MESSAGE}"
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._discard()
                    return "", f"Python execution timeout. {_RESET_MESSAGE}"
                if result_ready.wait(min(_POLL_SECONDS, remaining)):
                    if self.cancelled(stop_event) or time.monotonic() >= deadline:
                        continue
                    result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
                    if self.cancelled(stop_event) or time.monotonic() >= deadline:
                        continue
                    return result["output"], result["error"]
                # 显式检查退出；不能把已死亡的进程误报为又一次长时间运算。
                with self._resources:
                    if self._process is not process or not process.is_alive():
                        raise RuntimeError(f"Python interpreter exited unexpectedly (exit code {process.exitcode})")
        except Exception as exc:
            self._discard()
            if self.cancelled(stop_event):
                return "", f"Python execution cancelled or session closed. {_RESET_MESSAGE}"
            return "", f"{type(exc).__name__}: {exc}. {_RESET_MESSAGE}"
        except BaseException:
            # 宿主收到 Ctrl+C 时先回收自身子进程，再把信号交还现有的两阶段停止流程。
            self._discard()
            raise

    def _discard(self) -> None:
        with self._resources:
            process, directory = self._process, self._directory
            self._process = self._directory = None
            self._request_ready = self._result_ready = None
            if process is not None:
                if process.pid is not None:
                    if process.is_alive():
                        process.terminate()
                    process.join(timeout=0.5)
                    if process.is_alive():
                        process.kill()
                        process.join(timeout=1.0)
                    process.close()
            if directory is not None:
                resolved = directory.resolve()
                if resolved != self._root and resolved.is_relative_to(self._root):
                    shutil.rmtree(resolved)

    def close(self) -> None:
        self.closed.set()
        self._discard()


def _serve_python(directory: str, request_ready, result_ready) -> None:
    """专属解释器只做求值；文件传递正文，事件通知完成，避免管道大消息阻塞监督线程。"""
    import os

    # 终端信号只交给宿主决定取消范围，避免第一次 Ctrl+C 连带退出所有计算会话。
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    root = Path(directory)
    workspace = root / "workspace"
    workspace.mkdir()
    os.chdir(workspace)
    env: dict = {}
    while True:
        request_ready.wait()
        request_ready.clear()
        request = json.loads((root / "request.json").read_text(encoding="utf-8"))
        output, error = evaluate_python(request["code"], env, allow_filesystem=request["allow_filesystem"])
        result_path = root / "result.tmp"
        result_path.write_text(json.dumps({"output": output, "error": error}), encoding="utf-8")
        result_path.replace(root / "result.json")
        result_ready.set()
