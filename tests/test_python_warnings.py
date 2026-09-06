from __future__ import annotations

import warnings

from alphasolve.agent.tools import ToolRegistry, ToolResult
from alphasolve.solver.execution.runners import run_python
from alphasolve.solver.tool_runtime import register_execution_tools


def test_run_python_returns_invalid_escape_syntax_warning_as_error_without_execution():
    env: dict[str, object] = {}
    code = r'pattern = "\{"' + '\nmarker = "must not persist"'

    with warnings.catch_warnings():
        warnings.simplefilter("error", SyntaxWarning)
        stdout, error = run_python(code, env, allow_filesystem=False)

    assert stdout == ""
    assert error is not None
    assert "SyntaxWarning" in error
    assert "invalid escape sequence" in error
    assert 'r"\\{' in error
    assert "marker" not in env


def test_run_python_accepts_raw_string_with_literal_open_brace():
    env: dict[str, object] = {}
    code = r'pattern = r"\{"' + "\npattern"

    stdout, error = run_python(code, env, allow_filesystem=False)

    assert error is None
    assert env["pattern"] == r"\{"
    assert "\\\\{" in stdout


def test_run_python_description_explains_backslash_escaping():
    registry = ToolRegistry()
    register_execution_tools(
        registry,
        run_python_handler=lambda _args: ToolResult("ok"),
        run_wolfram_handler=lambda _args: ToolResult("ok"),
    )

    description = registry.tool_defs(["RunPython"])[0].description

    assert "raw strings" in description
    assert "double escaping" in description
    assert 'r"\\{' in description
