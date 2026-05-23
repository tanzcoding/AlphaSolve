from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class WolframKernelCheck:
    available: bool
    reason: str
    kernel_path: Optional[str] = None
    output: str = ""


def check_wolfram_kernel(timeout_seconds: int = 30) -> WolframKernelCheck:
    """Return whether a Wolfram kernel can start and preserve session state."""

    kernel_path = os.environ.get("WOLFRAM_KERNEL")
    script = r'''
import os
import traceback
from wolframclient.evaluation import WolframLanguageSession
from wolframclient.language import wlexpr

kernel_path = os.environ.get("WOLFRAM_KERNEL")
session = None
try:
    session = WolframLanguageSession(kernel_path) if kernel_path else WolframLanguageSession()
    first = session.evaluate(wlexpr("alphaSolveProbeValue = 41"))
    second = session.evaluate(wlexpr("alphaSolveProbeValue + 1"))
    if str(first) != "41" or str(second) != "42":
        raise RuntimeError(f"unexpected probe results: first={first!r}, second={second!r}")
    print("ok")
except Exception:
    traceback.print_exc()
    raise
finally:
    if session is not None:
        try:
            session.terminate()
        except Exception:
            pass
'''

    try:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout_seconds)),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = _combine_output(exc.stdout, exc.stderr)
        return WolframKernelCheck(
            available=False,
            reason=f"wolfram_probe_timeout_after_{timeout_seconds}s",
            kernel_path=kernel_path,
            output=output,
        )
    except FileNotFoundError as exc:
        return WolframKernelCheck(
            available=False,
            reason=f"python_executable_not_found: {exc}",
            kernel_path=kernel_path,
        )
    except Exception as exc:
        return WolframKernelCheck(
            available=False,
            reason=f"wolfram_probe_failed: {exc}",
            kernel_path=kernel_path,
        )

    output = _combine_output(completed.stdout, completed.stderr)
    if completed.returncode != 0:
        reason = _short_reason(completed.stderr) or f"probe_exit_code_{completed.returncode}"
        return WolframKernelCheck(
            available=False,
            reason=reason,
            kernel_path=kernel_path,
            output=output,
        )

    return WolframKernelCheck(
        available=True,
        reason="wolfram_kernel_probe_ok",
        kernel_path=kernel_path,
        output=output,
    )


def _combine_output(stdout: object, stderr: object) -> str:
    parts = []
    if stdout:
        parts.append(str(stdout).strip())
    if stderr:
        parts.append(str(stderr).strip())
    return "\n".join(part for part in parts if part)


def _short_reason(stderr: str) -> str:
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    if not lines:
        return ""
    return lines[-1][:300]

