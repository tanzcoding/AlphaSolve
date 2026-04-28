from __future__ import annotations

import multiprocessing as mp
import os
import queue
import shutil
import tempfile
import threading
import uuid
from dataclasses import dataclass

from alphasolve.execution.runners import run_python, run_wolfram
from alphasolve.utils.logger import Logger
from wolframclient.evaluation import WolframLanguageSession


@dataclass(frozen=True)
class ExecutionOutput:
    tool_content: str
    log_parts: list[str]


class ExecutionGateway:
    """Small routing layer for code execution tools.

    Python code runs in a small pool of worker processes. Each tool conversation
    gets a stable session_id, and all requests for that session go to the same
    worker, whose in-process env dictionary persists between calls.

    Wolfram sessions are kept per session_id as well. The Wolfram kernel itself
    is already a separate process, so keeping the lightweight session handles in
    the main process is enough for now.
    """

    def __init__(
        self,
        *,
        python_workers: int = 2,
        wolfram_enabled: bool = True,
        logger: Logger | None = None,
    ) -> None:
        self.python_workers = max(1, int(python_workers))
        self.wolfram_enabled = wolfram_enabled
        self.logger = logger
        self._sandbox_root = tempfile.mkdtemp(prefix="alphasolve-exec-")

        self._python_pool: _PythonWorkerPool | None = None
        self._wolfram = _WolframSessionRegistry(logger=logger)

    def run_python(
        self,
        *,
        session_id: str,
        code: str,
        timeout_seconds: int = 300,
        allow_filesystem: bool = False,
    ) -> ExecutionOutput:
        if self._python_pool is None:
            self._python_pool = _PythonWorkerPool(
                self.python_workers,
                sandbox_root=self._sandbox_root,
                logger=self.logger,
            )
        return self._python_pool.execute(session_id, code, timeout_seconds, allow_filesystem=allow_filesystem)

    def run_wolfram(
        self,
        *,
        session_id: str,
        code: str,
        timeout_seconds: int = 300,
    ) -> ExecutionOutput:
        if not self.wolfram_enabled:
            return ExecutionOutput("[error]\nWolfram kernel is not available in this run", [])
        return self._wolfram.execute(session_id, code, timeout_seconds)

    def close(self) -> None:
        if self._python_pool is not None:
            self._python_pool.close()
            self._python_pool = None
        self._wolfram.close()
        shutil.rmtree(self._sandbox_root, ignore_errors=True)


class _PythonWorkerPool:
    def __init__(self, workers: int, *, sandbox_root: str, logger: Logger | None = None) -> None:
        self._logger = logger
        self._sandbox_root = sandbox_root
        self._ctx = mp.get_context("spawn")
        self._out_queue = self._ctx.Queue()
        self._in_queues = [self._ctx.Queue() for _ in range(workers)]
        self._processes = [
            self._ctx.Process(
                target=_python_worker_loop,
                args=(idx, self._in_queues[idx], self._out_queue, self._sandbox_root),
                daemon=True,
            )
            for idx in range(workers)
        ]
        for process in self._processes:
            process.start()

        self._lock = threading.Lock()
        self._pending: dict[str, "queue.Queue[dict]"] = {}
        self._session_worker: dict[str, int] = {}
        self._next_worker = 0
        self._closed = False
        self._dispatcher = threading.Thread(target=self._dispatch_results, daemon=True)
        self._dispatcher.start()

    def execute(
        self,
        session_id: str,
        code: str,
        timeout_seconds: int,
        *,
        allow_filesystem: bool,
    ) -> ExecutionOutput:
        request_id = uuid.uuid4().hex
        result_box: "queue.Queue[dict]" = queue.Queue(maxsize=1)
        worker_idx = self._worker_for_session(session_id)

        with self._lock:
            if self._closed:
                return ExecutionOutput("[error]\nPython execution pool is closed", [])
            self._pending[request_id] = result_box

        self._in_queues[worker_idx].put(
            {
                "id": request_id,
                "session_id": session_id,
                "code": code,
                "timeout_seconds": timeout_seconds,
                "allow_filesystem": allow_filesystem,
            }
        )

        try:
            data = result_box.get(timeout=timeout_seconds + 10)
        except queue.Empty:
            with self._lock:
                self._pending.pop(request_id, None)
            return ExecutionOutput("[error]\ntimeout", ["[error]\ntimeout"])

        return ExecutionOutput(
            data.get("tool_content", ""),
            data.get("log_parts", []),
        )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for pending in self._pending.values():
                pending.put({"tool_content": "[error]\nPython execution pool closed", "log_parts": []})
            self._pending.clear()

        for in_queue in self._in_queues:
            in_queue.put(None)
        for process in self._processes:
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()

        self._out_queue.put(None)
        self._dispatcher.join(timeout=2)

    def _worker_for_session(self, session_id: str) -> int:
        with self._lock:
            if session_id not in self._session_worker:
                self._session_worker[session_id] = self._next_worker
                self._next_worker = (self._next_worker + 1) % len(self._in_queues)
            return self._session_worker[session_id]

    def _dispatch_results(self) -> None:
        while True:
            try:
                data = self._out_queue.get(timeout=0.5)
            except Exception:
                with self._lock:
                    if self._closed:
                        return
                continue
            if data is None:
                return
            request_id = data.get("id")
            with self._lock:
                pending = self._pending.pop(request_id, None)
            if pending is not None:
                pending.put(data)


class _WolframSessionRegistry:
    def __init__(self, *, logger: Logger | None = None) -> None:
        self._logger = logger
        self._lock = threading.Lock()
        self._sessions: dict[str, object] = {}
        self._session_locks: dict[str, threading.Lock] = {}

    def execute(self, session_id: str, code: str, timeout_seconds: int) -> ExecutionOutput:
        session = self._get_session(session_id)
        if session is None:
            return ExecutionOutput("[error]\nWolfram session not available", ["[error]\nWolfram session not available"])

        lock = self._get_session_lock(session_id)
        with lock:
            output, error = run_wolfram(code, session, timeout_seconds=timeout_seconds)
            if isinstance(error, str) and error.startswith("timeout"):
                self._restart_session(session_id)

        tool_content, log_parts = _format_output(output=output, error=error, output_label="output")
        return ExecutionOutput(tool_content, log_parts)

    def close(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._session_locks.clear()
        for session in sessions:
            try:
                session.terminate()
            except Exception:
                pass

    def _get_session(self, session_id: str):
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = _start_wolfram_session()
                if session is not None:
                    self._sessions[session_id] = session
            return session

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        with self._lock:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_id] = lock
            return lock

    def _restart_session(self, session_id: str) -> None:
        with self._lock:
            old = self._sessions.pop(session_id, None)
            new = _start_wolfram_session()
            if new is not None:
                self._sessions[session_id] = new
        if old is not None:
            try:
                old.terminate()
            except Exception:
                pass


def _python_worker_loop(worker_idx: int, in_queue, out_queue, sandbox_root: str) -> None:
    envs: dict[str, dict] = {}
    session_dirs: dict[str, str] = {}
    while True:
        request = in_queue.get()
        if request is None:
            return

        request_id = request["id"]
        session_id = request["session_id"]
        code = request.get("code", "")
        timeout_seconds = int(request.get("timeout_seconds", 300))
        allow_filesystem = bool(request.get("allow_filesystem", False))
        env = envs.setdefault(session_id, {})
        session_dir = session_dirs.get(session_id)
        if session_dir is None:
            session_dir = tempfile.mkdtemp(prefix=f"py-{worker_idx}-", dir=sandbox_root)
            session_dirs[session_id] = session_dir

        old_cwd = os.getcwd()
        try:
            os.chdir(session_dir)
            stdout, error = run_python(
                code,
                env,
                timeout_seconds=timeout_seconds,
                allow_filesystem=allow_filesystem,
            )
        finally:
            os.chdir(old_cwd)
        tool_content, log_parts = _format_output(output=stdout, error=error, output_label="stdout")
        out_queue.put(
            {
                "id": request_id,
                "worker_idx": worker_idx,
                "tool_content": tool_content,
                "log_parts": log_parts,
            }
        )


def _format_output(*, output: str, error: str | None, output_label: str) -> tuple[str, list[str]]:
    tool_content = ""
    log_parts: list[str] = []
    if output:
        text = f"[{output_label}]\n{output}"
        tool_content += text
        log_parts.append(text)
    if error:
        text = f"[error]\n{error}"
        tool_content += text
        log_parts.append(text)
    return tool_content, log_parts


def _start_wolfram_session():
    try:
        return WolframLanguageSession()
    except Exception:
        kernel_path = os.environ.get("WOLFRAM_KERNEL")
        if kernel_path:
            try:
                return WolframLanguageSession(kernel_path)
            except Exception:
                return None
        return None
