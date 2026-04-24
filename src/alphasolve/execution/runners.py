from __future__ import annotations

import ast
import builtins
import importlib
import io
import queue
import sys
import threading
import time
import traceback
import types

BANNED_IMPORT_ROOTS = {"matplotlib", "pylab"}
FILESYSTEM_IMPORT_ROOTS = {"os", "pathlib", "shutil", "subprocess", "glob", "tempfile", "socket", "importlib"}
FILESYSTEM_CALL_NAMES = {"open", "__import__"}
FILESYSTEM_ATTR_NAMES = {
    "open", "read_text", "write_text", "read_bytes", "write_bytes",
    "mkdir", "unlink", "rmdir", "iterdir", "listdir", "walk",
    "scandir", "remove", "rmtree", "copy", "copy2",
}


def _is_banned(name: str) -> bool:
    return bool(name) and name.split(".", 1)[0] in BANNED_IMPORT_ROOTS


def _purge_banned() -> None:
    for name in list(sys.modules.keys()):
        if _is_banned(name):
            sys.modules.pop(name, None)


def _check_code(code: str, *, allow_filesystem: bool) -> tuple[ast.Module | None, str | None]:
    try:
        parsed = ast.parse(code, mode="exec")
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


def run_python(
    code: str,
    env: dict | None = None,
    timeout_seconds: int = 300,
    *,
    allow_filesystem: bool = True,
) -> tuple[str, str | None]:
    buf = io.StringIO()
    old_out = sys.stdout
    err = None
    if env is None:
        env = {}

    parsed_ast, static_error = _check_code(code, allow_filesystem=allow_filesystem)
    if static_error:
        return "", static_error

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

    env_snapshot = dict(env)
    env_keys_snapshot = set(env_snapshot.keys())
    start_t = time.monotonic()
    old_trace = sys.gettrace()

    def _trace(frame, event, arg):
        if timeout_seconds and (time.monotonic() - start_t) > timeout_seconds:
            raise TimeoutError("timeout")
        return _trace

    try:
        sys.stdout = buf
        sys.settrace(_trace)
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
    except SyntaxError:
        try:
            exec(code, env, env)
        except Exception:
            err = traceback.format_exc().strip()
    except TimeoutError:
        err = "timeout"
        for k in list(env.keys()):
            if k not in env_keys_snapshot:
                env.pop(k, None)
        for k, v in env_snapshot.items():
            env[k] = v
    except Exception:
        err = traceback.format_exc().strip()
    finally:
        sys.stdout = old_out
        sys.settrace(old_trace)
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
