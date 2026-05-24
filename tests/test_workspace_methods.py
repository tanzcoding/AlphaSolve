"""第二层 Workspace 新方法的单元测试。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alphasolve.agent import Workspace  # noqa: E402
from alphasolve.agent import workspace as workspace_module  # noqa: E402
from alphasolve.agent.workspace import WorkspaceError  # noqa: E402


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace(root=tmp_path)


def test_write_text_overwrite(ws: Workspace):
    ws.write_text("a.txt", "hello")
    ws.write_text("a.txt", "world", mode="overwrite")
    assert (ws.root / "a.txt").read_text(encoding="utf-8") == "world"


def test_write_text_append(ws: Workspace):
    ws.write_text("a.txt", "hello")
    ws.write_text("a.txt", " world", mode="append")
    assert (ws.root / "a.txt").read_text(encoding="utf-8") == "hello world"


def test_write_text_invalid_mode(ws: Workspace):
    with pytest.raises(WorkspaceError):
        ws.write_text("a.txt", "x", mode="wat")


def test_edit_success(ws: Workspace):
    ws.write_text("a.txt", "hello world")
    ws.edit("a.txt", "world", "moon")
    assert (ws.root / "a.txt").read_text(encoding="utf-8") == "hello moon"


def test_edit_old_str_not_found(ws: Workspace):
    ws.write_text("a.txt", "hello")
    with pytest.raises(WorkspaceError, match="not found"):
        ws.edit("a.txt", "world", "x")


def test_edit_old_str_ambiguous(ws: Workspace):
    ws.write_text("a.txt", "hi hi")
    with pytest.raises(WorkspaceError, match="multiple"):
        ws.edit("a.txt", "hi", "x")


def test_make_dir(ws: Workspace):
    ws.make_dir("sub/nested")
    assert (ws.root / "sub" / "nested").is_dir()


def test_rename_item(ws: Workspace):
    ws.write_text("a.txt", "x")
    result = ws.rename_item(".", "a.txt", "b.txt")
    assert result == {"old_path": "a.txt", "path": "b.txt"}
    assert not (ws.root / "a.txt").exists()
    assert (ws.root / "b.txt").read_text(encoding="utf-8") == "x"


def test_rename_item_rejects_paths(ws: Workspace):
    ws.write_text("a.txt", "x")
    with pytest.raises(WorkspaceError, match="plain names"):
        ws.rename_item(".", "a.txt", "sub/b.txt")


def test_move_file(ws: Workspace):
    ws.write_text("a.txt", "x")
    ws.make_dir("sub")
    result = ws.move_file("a.txt", "sub")
    assert result == {"old_path": "a.txt", "path": "sub/a.txt"}
    assert (ws.root / "sub" / "a.txt").read_text(encoding="utf-8") == "x"


def test_delete_file(ws: Workspace):
    ws.write_text("a.txt", "x")
    ws.delete_path("a.txt")
    assert not (ws.root / "a.txt").exists()


def test_delete_dir_not_empty(ws: Workspace):
    ws.write_text("sub/a.txt", "x")
    with pytest.raises(WorkspaceError, match="not empty"):
        ws.delete_path("sub")


def test_list_dir_with_max_results(ws: Workspace):
    for i in range(5):
        ws.write_text(f"file{i}.txt", "x")
    result = ws.list_dir(".", max_results=2)
    assert len(result) == 2


def test_glob_recursive(ws: Workspace):
    ws.write_text("a.py", "x")
    ws.write_text("sub/b.py", "x")
    ws.write_text("c.txt", "x")
    result = ws.glob("**/*.py")
    assert sorted(result) == ["a.py", "sub/b.py"]


def test_grep_regex(ws: Workspace):
    ws.write_text("a.py", "def foo():\n    pass\n")
    ws.write_text("b.py", "def bar():\n    pass\n")
    result = ws.grep(r"def (foo|bar)", path=".")
    assert len(result) == 2
    assert {hit["path"] for hit in result} == {"a.py", "b.py"}


def test_grep_substring(ws: Workspace):
    ws.write_text("a.txt", "alpha beta\n")
    result = ws.grep("beta", path=".", regex=False)
    assert len(result) == 1
    assert result[0]["text"] == "alpha beta"


def test_grep_prefers_rg_without_context(ws: Workspace, monkeypatch: pytest.MonkeyPatch):
    ws.write_text("a.txt", "alpha beta\n")
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)

        class Result:
            returncode = 0
            stdout = "a.txt:1:alpha beta\n"
            stderr = ""

        return Result()

    monkeypatch.setattr(workspace_module.shutil, "which", lambda name: "rg" if name == "rg" else None)
    monkeypatch.setattr(workspace_module.subprocess, "run", fake_run)

    result = ws.grep("beta", path=".", regex=False, max_results=5)

    assert result == [{"path": "a.txt", "line": 1, "text": "alpha beta", "context": ""}]
    assert calls
    assert "--fixed-strings" in calls[0]


def test_grep_context_uses_python_fallback(ws: Workspace, monkeypatch: pytest.MonkeyPatch):
    ws.write_text("a.txt", "alpha\nbeta\ngamma\n")

    def fail_run(*_args, **_kwargs):
        raise AssertionError("rg should not be used when context_lines is requested")

    monkeypatch.setattr(workspace_module.shutil, "which", lambda name: "rg" if name == "rg" else None)
    monkeypatch.setattr(workspace_module.subprocess, "run", fail_run)

    result = ws.grep("beta", path=".", regex=False, context_lines=1)

    assert len(result) == 1
    assert result[0]["context"] == "1: alpha\n2: beta\n3: gamma"
