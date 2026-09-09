from __future__ import annotations

import os
import threading
from dataclasses import dataclass

from alphasolve.solver.execution.runners import run_wolfram
from alphasolve.solver.execution.python_sessions import PythonSessionExecutor
from alphasolve.solver.logging.logger import Logger
from wolframclient.evaluation import WolframLanguageSession


@dataclass(frozen=True)
class ExecutionOutput:
    tool_content: str
    log_parts: list[str]
    is_error: bool = False
    queue_seconds: float = 0.0
    execution_seconds: float = 0.0


class ExecutionGateway:
    """计算工具的资源所有者；Python 会话独立运行，宿主监督截止时间与取消。"""

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
        self._closed = threading.Event()
        self._python = PythonSessionExecutor(self.python_workers)
        self._wolfram = _WolframSessionRegistry(logger=logger)

    def run_python(
        self,
        *,
        session_id: str,
        code: str,
        timeout_seconds: float = 300,
        allow_filesystem: bool = False,
        stop_event: threading.Event | None = None,
    ) -> ExecutionOutput:
        result = self._python.execute(
            session_id, code, timeout_seconds,
            allow_filesystem=allow_filesystem, stop_event=stop_event,
        )
        content, parts = _format_output(output=result.output, error=result.error, output_label="output")
        return ExecutionOutput(
            content, parts, is_error=result.error is not None,
            queue_seconds=result.queue_seconds, execution_seconds=result.execution_seconds,
        )

    def run_wolfram(
        self,
        *,
        session_id: str,
        code: str,
        timeout_seconds: int = 300,
    ) -> ExecutionOutput:
        if self._closed.is_set():
            return ExecutionOutput("[error]\nExecution gateway is closed", [], is_error=True)
        if not self.wolfram_enabled:
            return ExecutionOutput("[error]\nWolfram kernel is not available in this run", [], is_error=True)
        return self._wolfram.execute(session_id, code, timeout_seconds)

    def close_session(self, session_id: str) -> None:
        self._python.close_session(session_id)
        self._wolfram.close_session(session_id)

    def close(self) -> None:
        self._closed.set()
        self._python.close()
        self._wolfram.close()


class _WolframSessionRegistry:
    def __init__(self, *, logger: Logger | None = None) -> None:
        self._logger = logger
        self._lock = threading.Lock()
        self._sessions: dict[str, object] = {}
        self._session_locks: dict[str, threading.Lock] = {}

    def execute(self, session_id: str, code: str, timeout_seconds: int) -> ExecutionOutput:
        session = self._get_session(session_id)
        if session is None:
            return ExecutionOutput("[error]\nWolfram session not available", ["[error]\nWolfram session not available"], is_error=True)

        lock = self._get_session_lock(session_id)
        with lock:
            output, error = run_wolfram(code, session, timeout_seconds=timeout_seconds)
            if isinstance(error, str) and error.startswith("timeout"):
                self._restart_session(session_id)

        tool_content, log_parts = _format_output(output=output, error=error, output_label="output")
        return ExecutionOutput(tool_content, log_parts, is_error=error is not None)

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

    def close_session(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
            self._session_locks.pop(session_id, None)
        if session is not None:
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
