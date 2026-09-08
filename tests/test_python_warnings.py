from __future__ import annotations

import warnings
import sys

import pytest

from alphasolve.agent.tools import ToolRegistry, ToolResult
from alphasolve.solver.execution.runners import (
    MAX_PYTHON_ERROR_CHARS,
    MAX_PYTHON_OUTPUT_CHARS,
    evaluate_python,
)
from alphasolve.solver.tool_runtime import register_execution_tools


def test_evaluate_python_returns_invalid_escape_syntax_warning_as_error_without_execution():
    env: dict[str, object] = {}
    code = r'pattern = "\{"' + '\nmarker = "must not persist"'

    with warnings.catch_warnings():
        warnings.simplefilter("error", SyntaxWarning)
        stdout, error = evaluate_python(code, env, allow_filesystem=False)

    assert stdout == ""
    assert error is not None
    assert "SyntaxWarning" in error
    assert "invalid escape sequence" in error
    assert 'r"\\{' in error
    assert "marker" not in env


def test_evaluate_python_accepts_raw_string_with_literal_open_brace():
    env: dict[str, object] = {}
    code = r'pattern = r"\{"' + "\npattern"

    stdout, error = evaluate_python(code, env, allow_filesystem=False)

    assert error is None
    assert env["pattern"] == r"\{"
    assert "\\\\{" in stdout


@pytest.mark.parametrize("failure", [False, True])
def test_evaluate_python_captures_stdout_and_stderr_in_order_and_restores_streams(capsys, failure):
    original_stdout, original_stderr = sys.stdout, sys.stderr
    code = (
        "import sys\n"
        "print('before')\n"
        "print('diagnostic', file=sys.stderr)\n"
        "print('after')\n"
    )
    if failure:
        code += "raise RuntimeError('intentional failure')\n"

    output, error = evaluate_python(code, {}, allow_filesystem=False)

    assert output == "before\ndiagnostic\nafter\n"
    assert sys.stdout is original_stdout
    assert sys.stderr is original_stderr
    assert capsys.readouterr() == ("", "")
    if failure:
        assert "RuntimeError: intentional failure" in error
    else:
        assert error is None


def test_evaluate_python_truncates_combined_output_without_interrupting_computation(capsys):
    env: dict[str, object] = {}
    code = (
        "import sys\n"
        "for _ in range(4):\n"
        "    sys.stdout.write('o' * 400000)\n"
        "    sys.stderr.write('e' * 400000)\n"
        "completed = True\n"
    )

    output, error = evaluate_python(code, env, allow_filesystem=False)

    assert error is None
    assert env["completed"] is True
    assert len(output) <= MAX_PYTHON_OUTPUT_CHARS
    assert output.startswith("o" * 400000 + "e" * 400000)
    assert output.endswith(f"[output truncated: exceeded {MAX_PYTHON_OUTPUT_CHARS} characters]\n")
    assert capsys.readouterr() == ("", "")


def test_evaluate_python_bounds_exception_text_and_keeps_prior_output(capsys):
    original_stdout, original_stderr = sys.stdout, sys.stderr
    output, error = evaluate_python(
        f"print('before failure')\nraise ValueError('x' * {MAX_PYTHON_ERROR_CHARS * 2})",
        {},
        allow_filesystem=False,
    )

    assert output == "before failure\n"
    assert error is not None
    assert len(error) <= MAX_PYTHON_ERROR_CHARS
    assert "ValueError:" in error
    assert error.endswith(f"[error truncated: exceeded {MAX_PYTHON_ERROR_CHARS} characters]")
    assert sys.stdout is original_stdout
    assert sys.stderr is original_stderr
    assert capsys.readouterr() == ("", "")


def test_run_python_description_explains_execution_contract_and_backslash_escaping():
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
    assert "300-second total time budget" in description
    assert "queueing and interpreter startup" in description
    assert "previous variables are gone" in description
    assert "Standard output and standard error are combined" in description
    assert "1,048,576 characters" in description
