from __future__ import annotations

import ast
import builtins
import importlib
import io
import queue
import sys
import threading
import traceback
import types
import warnings

BANNED_IMPORT_ROOTS = {"matplotlib", "pylab"}
FILESYSTEM_IMPORT_ROOTS = {"os", "pathlib", "shutil", "subprocess", "glob", "tempfile", "socket", "importlib"}
FILESYSTEM_CALL_NAMES = {"open", "__import__"}
FILESYSTEM_ATTR_NAMES = {
    "open", "read_text", "write_text", "read_bytes", "write_bytes",
    "mkdir", "unlink", "rmdir", "iterdir", "listdir", "walk",
    "scandir", "remove", "rmtree", "copy", "copy2",
}
MAX_PYTHON_OUTPUT_CHARS = 1024 * 1024
MAX_PYTHON_ERROR_CHARS = 1024 * 1024


class _BoundedTextCapture(io.TextIOBase):
    """达到上限后继续接收写入，只保留有界文本，不中断用户计算。"""

    def __init__(self, *, limit: int, label: str) -> None:
        super().__init__()
        self._buffer = io.StringIO()
        self._limit = limit
        self._label = label
        self._size = 0
        self._lock = threading.Lock()
        self.truncated = False

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            raise TypeError("text output requires a string")
        if self.closed:
            raise ValueError("I/O operation on closed file")
        size = len(text)
        with self._lock:
            remaining = self._limit - self._size
            if remaining > 0:
                kept = text[:remaining]
                self._buffer.write(kept)
                self._size += len(kept)
            if size > remaining:
                self.truncated = True
        return size

    def getvalue(self) -> str:
        with self._lock:
            value = self._buffer.getvalue()
            if not self.truncated:
                return value
            notice = f"\n[{self._label} truncated: exceeded {self._limit} characters]\n"
            return value[:self._limit - len(notice)] + notice


def _is_banned(name: str) -> bool:
    return bool(name) and name.split(".", 1)[0] in BANNED_IMPORT_ROOTS


def _purge_banned() -> None:
    for name in list(sys.modules.keys()):
        if _is_banned(name):
            sys.modules.pop(name, None)


def _check_code(code: str, *, allow_filesystem: bool) -> tuple[ast.Module | None, str | None]:
    try:
        parsed = ast.parse(code, filename="<string>", mode="exec")
    except SyntaxError:
        return None, None
    for node in ast.walk(parsed):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if _is_banned(root):
                    return parsed, "ImportError: matplotlib/pylab is disabled"
                if not allow_filesystem and root in FILESYSTEM_IMPORT_ROOTS:
                    return parsed, f"filesystem access is disabled: importing {root!r} is not allowed"
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            if _is_banned(root):
                return parsed, "ImportError: matplotlib/pylab is disabled"
            if not allow_filesystem and root in FILESYSTEM_IMPORT_ROOTS:
                return parsed, f"filesystem access is disabled: importing {root!r} is not allowed"
        elif not allow_filesystem and isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in FILESYSTEM_CALL_NAMES:
                return parsed, f"filesystem access is disabled: calling {func.id!r} is not allowed"
            if isinstance(func, ast.Attribute) and func.attr in FILESYSTEM_ATTR_NAMES:
                return parsed, f"filesystem access is disabled: calling {func.attr!r} is not allowed"
    return parsed, None


def _syntax_warning_error(records: list[warnings.WarningMessage]) -> str | None:
    """把编译期警告转为有界的工具错误，并给出修正方法。"""
    syntax_warnings = [record for record in records if issubclass(record.category, SyntaxWarning)]
    if not syntax_warnings:
        return None
    capture = _BoundedTextCapture(limit=MAX_PYTHON_ERROR_CHARS, label="error")
    capture.write(
        "Python code was rejected because it contains a SyntaxWarning.\n"
        "Fix the code and run it again; do not rely on the warning being ignored.\n"
    )
    for record in syntax_warnings:
        location = f"{record.filename}:{record.lineno}" if record.lineno else str(record.filename)
        capture.write(f"{location}: {record.category.__name__}: {record.message}\n")
        if capture.truncated:
            break
    capture.write(
        'For regexes or strings with backslashes, prefer raw strings such as r"\\{" '
        'or double escaping such as "\\\\{".'
    )
    return capture.getvalue()


def evaluate_python(
    code: str,
    env: dict | None = None,
    *,
    allow_filesystem: bool = True,
) -> tuple[str, str | None]:
    """在专属子进程内求值；截止时间和取消由父进程监督。"""
    buf = _BoundedTextCapture(limit=MAX_PYTHON_OUTPUT_CHARS, label="output")
    old_out = sys.stdout
    old_err = sys.stderr
    err = None
    if env is None:
        env = {}

    with warnings.catch_warnings(record=True) as captured_warnings:
        warnings.simplefilter("always", SyntaxWarning)
        parsed_ast, static_error = _check_code(code, allow_filesystem=allow_filesystem)
    syntax_warning_error = _syntax_warning_error(captured_warnings)
    if syntax_warning_error:
        return "", syntax_warning_error
    if static_error:
        capture = _BoundedTextCapture(limit=MAX_PYTHON_ERROR_CHARS, label="error")
        capture.write(static_error)
        return "", capture.getvalue()

    _purge_banned()
    for k in list(env.keys()):
        v = env.get(k)
        if isinstance(v, types.ModuleType) and _is_banned(getattr(v, "__name__", "")):
            env.pop(k, None)

    original_import = builtins.__import__
    original_open = builtins.open

    def _blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
        if _is_banned(str(name)):
            raise ImportError("matplotlib/pylab is disabled in this runtime")
        return original_import(name, globals, locals, fromlist, level)

    def _blocked_open(*args, **kwargs):
        raise PermissionError("filesystem access is disabled in this runtime")

    env_builtins = env.get("__builtins__")
    if isinstance(env_builtins, types.ModuleType):
        env_builtins = env_builtins.__dict__
    if env_builtins is None:
        env_builtins = builtins.__dict__
    env_builtins = dict(env_builtins)
    env_builtins["__import__"] = _blocked_import
    env_builtins["open"] = _blocked_open if not allow_filesystem else original_open
    env["__builtins__"] = env_builtins

    original_importlib_import = getattr(importlib, "import_module", None)

    def _blocked_import_module(name, package=None):
        if _is_banned(str(name)):
            raise ImportError("matplotlib/pylab is disabled in this runtime")
        if original_importlib_import is None:
            raise ImportError("importlib.import_module is unavailable")
        return original_importlib_import(name, package=package)

    try:
        sys.stdout = buf
        sys.stderr = buf
        builtins.__import__ = _blocked_import
        if not allow_filesystem:
            builtins.open = _blocked_open
        if original_importlib_import is not None:
            importlib.import_module = _blocked_import_module

        parsed = parsed_ast if parsed_ast is not None else ast.parse(code, mode="exec")
        if parsed.body and isinstance(parsed.body[-1], ast.Expr):
            *stmts, last_expr = parsed.body
            if stmts:
                exec(compile(ast.Module(body=stmts, type_ignores=[]), "<string>", "exec"), env, env)
            result = eval(compile(ast.Expression(body=last_expr.value), "<string>", "eval"), env, env)
            if result is not None:
                print(repr(result))
        else:
            exec(code, env, env)
    except BaseException:
        # 用户代码中的退出异常也是本次工具结果，不能结束承载会话的解释器。
        capture = _BoundedTextCapture(limit=MAX_PYTHON_ERROR_CHARS, label="error")
        traceback.print_exc(limit=100, file=capture)
        err = capture.getvalue().strip()
    finally:
        sys.stdout = old_out
        sys.stderr = old_err
        builtins.__import__ = original_import
        if not allow_filesystem:
            builtins.open = original_open
        if original_importlib_import is not None:
            importlib.import_module = original_importlib_import

    return buf.getvalue(), err


def run_wolfram(code: str, session=None, timeout_seconds: int = 300) -> tuple[str, str | None]:
    if session is None:
        raise ValueError("Wolfram session must be provided by the caller")

    from wolframclient.language import wlexpr
    result_queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=1)

    def _worker():
        try:
            result = session.evaluate(wlexpr(code))
            result_queue.put(("output", str(result)))
        except Exception:
            result_queue.put(("error", traceback.format_exc().strip()))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout_seconds)

    if t.is_alive():
        err = "timeout"
        try:
            session.terminate()
        except Exception as exc:
            err = f"timeout (failed to terminate: {exc})"
        return "", err

    try:
        kind, payload = result_queue.get_nowait()
    except queue.Empty:
        return "", "unknown_error: wolfram worker produced no result"

    return (payload, None) if kind == "output" else ("", payload)
