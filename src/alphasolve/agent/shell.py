"""Cross-platform shell discovery and execution.

Follows Claude Code's bash discovery chain:
  Windows: ALPHASOLVE_GIT_BASH_PATH env → git.exe → hardcoded Git paths → PATH
  Unix:    SHELL env → /bin/bash /usr/bin/bash → PATH
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def is_windows() -> bool:
    return sys.platform == "win32"


def find_bash_path() -> Path | None:
    """Find a bash executable.

    On Windows, looks for Git Bash following Claude Code's discovery chain.
    On Unix, checks SHELL then common paths.
    """
    # 1. Explicit override (works on all platforms)
    env_bash = os.environ.get("ALPHASOLVE_GIT_BASH_PATH")
    if env_bash:
        p = Path(env_bash)
        if p.is_file():
            return p

    if is_windows():
        return _find_bash_on_windows()

    # ── Unix ──────────────────────────────────────────────
    shell = os.environ.get("SHELL", "")
    if shell and ("bash" in shell or "zsh" in shell):
        p = Path(shell)
        if p.is_file():
            return p

    for candidate in ["/bin/bash", "/usr/bin/bash", "/usr/local/bin/bash", "/bin/sh"]:
        p = Path(candidate)
        if p.is_file():
            return p

    which = shutil.which("bash") or shutil.which("sh")
    if which:
        return Path(which)

    return None


def _find_bash_on_windows() -> Path | None:
    # 1. Derive from git.exe location (Claude Code approach)
    git_path = _find_git_on_windows()
    if git_path:
        # git.exe is at <Git>/cmd/git.exe, bash.exe at <Git>/bin/bash.exe
        bash_path = git_path.parent.parent / "bin" / "bash.exe"
        if bash_path.is_file():
            return bash_path

    # 2. Common hardcoded install paths
    for candidate in [
        "C:/Program Files/Git/bin/bash.exe",
        "C:/Program Files (x86)/Git/bin/bash.exe",
    ]:
        p = Path(candidate)
        if p.is_file():
            return p

    # 3. PATH search
    which = shutil.which("bash.exe") or shutil.which("bash")
    if which:
        return Path(which)

    return None


def _find_git_on_windows() -> Path | None:
    # Check hardcoded locations first (faster)
    for candidate in [
        "C:/Program Files/Git/cmd/git.exe",
        "C:/Program Files (x86)/Git/cmd/git.exe",
    ]:
        p = Path(candidate)
        if p.is_file():
            return p

    which = shutil.which("git.exe") or shutil.which("git")
    if which:
        return Path(which)

    return None


def find_powershell_path() -> Path | None:
    """Find PowerShell executable.  Only meaningful on Windows."""
    if not is_windows():
        return None

    for candidate in [
        "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
        "C:/Windows/System32/powershell.exe",
    ]:
        p = Path(candidate)
        if p.is_file():
            return p

    which = shutil.which("powershell.exe") or shutil.which("powershell")
    if which:
        return Path(which)

    return None


def run_bash_command(
    command: str,
    *,
    cwd: str | Path,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """Run a command through bash.

    Raises FileNotFoundError if bash cannot be located.
    """
    bash_path = find_bash_path()
    if bash_path is None:
        raise FileNotFoundError(
            "bash not found; install Git Bash (https://git-scm.com/downloads/win) "
            "or set ALPHASOLVE_GIT_BASH_PATH"
        )
    return subprocess.run(
        [str(bash_path), "-c", command],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def run_powershell_command(
    command: str,
    *,
    cwd: str | Path,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """Run a command through PowerShell.

    Raises FileNotFoundError if PowerShell cannot be located.
    """
    ps_path = find_powershell_path()
    if ps_path is None:
        raise FileNotFoundError("PowerShell not found")
    return subprocess.run(
        [str(ps_path), "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def has_bash() -> bool:
    """Check whether bash is available on this system."""
    return find_bash_path() is not None
