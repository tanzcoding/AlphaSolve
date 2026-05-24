"""第二层 build_default_tool_registry 注册基础工具的测试。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alphasolve.agent import Workspace  # noqa: E402
from alphasolve.agent.tools import build_default_tool_registry  # noqa: E402


@pytest.fixture
def registry(tmp_path: Path):
    ws = Workspace(root=tmp_path)
    return build_default_tool_registry(ws)


def test_registers_all_basic_tools(registry):
    names = {tool.name for tool in registry.registered_tools()}
    expected = {
        "Read", "Write", "Edit", "MakeDir", "Rename", "Move", "Delete",
        "Glob", "ListDir", "Grep", "ResearchProgressReview", "InspectMarkdown", "GetCurrentTime",
    }
    # Bash 或 Shell（按平台二选一）
    shell_tool = ({"Bash"} & names) or ({"Shell"} & names)
    assert shell_tool, "Bash or Shell must be registered"
    missing = expected - names
    assert not missing, f"missing tools: {missing}"


def test_bash_unrestricted_when_available(tmp_path: Path):
    """Bash 解除 ls 限制（如果平台有 bash）。"""
    from alphasolve.agent.shell import has_bash
    if not has_bash():
        pytest.skip("bash not available on this platform")
    ws = Workspace(root=tmp_path)
    registry = build_default_tool_registry(ws)
    bash_tool = next(t for t in registry.registered_tools() if t.name == "Bash")
    # 调用 echo（非 ls）应当成功
    result = bash_tool.handler({"command": "echo hello"})
    assert "hello" in result.content
    assert not result.is_error


def test_write_mode_in_parameters(registry):
    write_tool = next(t for t in registry.registered_tools() if t.name == "Write")
    mode_param = write_tool.parameters["properties"]["mode"]
    assert mode_param["enum"] == ["overwrite", "append"]
    assert mode_param["default"] == "overwrite"


def test_inspect_markdown_surfaces_statement_and_tail(registry, tmp_path: Path):
    (tmp_path / "proof.md").write_text(
        "# Proposition\n"
        "Statement: enough conditions imply the target result.\n\n"
        "## Proof\n"
        "The middle computation is routine.\n"
        "Therefore target_value > baseline_value.\n",
        encoding="utf-8",
    )

    result = registry.execute("InspectMarkdown", {"path": "proof.md"})

    assert not result.is_error
    assert "== proof.md ==" in result.content
    assert "Statement: enough conditions imply the target result." in result.content
    assert "important-looking lines near file end:" in result.content
    assert "Therefore target_value > baseline_value." in result.content


def test_read_markdown_warns_when_proof_tail_outclaims_statement(registry, tmp_path: Path):
    (tmp_path / "proof.md").write_text(
        "## Statement\n"
        "The target quantity is at least L.\n\n"
        "## Proof\n"
        "After the main estimate, the remaining correction is positive.\n"
        "Therefore the target quantity is strictly greater than L.\n",
        encoding="utf-8",
    )

    result = registry.execute("Read", {"path": "proof.md", "read_all": True})

    assert not result.is_error
    assert "Markdown proof-review hint" in result.content
    assert "proof tail" in result.content
    assert "possible underclaimed proof" in result.content
    assert "next proposition should normally be an explicit statement" in result.content
    assert "strictly greater than L" in result.content


def test_read_markdown_does_not_warn_without_statement_and_proof(registry, tmp_path: Path):
    (tmp_path / "problem.md").write_text(
        "Decide the final response.\n\n"
        "The answer should be compared against the reference value.\n",
        encoding="utf-8",
    )

    result = registry.execute("Read", {"path": "problem.md", "read_all": True})

    assert not result.is_error
    assert "Markdown proof-review hint" not in result.content


def test_list_dir_suggests_progress_review_for_research_workspace(registry, tmp_path: Path):
    (tmp_path / "problem.md").write_text("Problem\n", encoding="utf-8")
    (tmp_path / "verified_propositions").mkdir()
    (tmp_path / "knowledge").mkdir()

    result = registry.execute("ListDir", {"path": "."})

    assert not result.is_error
    assert "research workspace hint" in result.content
    assert "ResearchProgressReview" in result.content


def test_research_progress_review_surfaces_workspace_and_attempt_notes(registry, tmp_path: Path):
    (tmp_path / "problem.md").write_text(
        "# Problem\n\n"
        "## Current status\n"
        "The target is not complete yet.\n",
        encoding="utf-8",
    )
    verified_dir = tmp_path / "verified_propositions"
    verified_dir.mkdir()
    (verified_dir / "index.md").write_text(
        "# Index\n\n"
        "## Remaining gaps\n"
        "A compact interface step is still missing.\n",
        encoding="utf-8",
    )
    (tmp_path / "knowledge").mkdir()
    attempt_dir = tmp_path / "unverified_propositions" / "attempt-a"
    attempt_dir.mkdir(parents=True)
    (attempt_dir / "review.md").write_text(
        "# Review\n\n"
        "## Failed check\n"
        "This broad attempt failed because it skipped a prerequisite.\n",
        encoding="utf-8",
    )

    result = registry.execute("ResearchProgressReview", {"path": "."})

    assert not result.is_error
    assert "workspace notes" in result.content
    assert "The target is not complete yet." in result.content
    assert "A compact interface step is still missing." in result.content
    assert "unfinished attempt notes" in result.content
    assert "failed because it skipped a prerequisite" in result.content


def test_research_progress_review_surfaces_underclaimed_proof(registry, tmp_path: Path):
    (tmp_path / "problem.md").write_text("Decide the final response.\n", encoding="utf-8")
    verified_dir = tmp_path / "verified_propositions"
    verified_dir.mkdir()
    (tmp_path / "knowledge").mkdir()
    (verified_dir / "answer.md").write_text(
        "## Statement\n"
        "The recorded answer reaches L.\n\n"
        "## Proof\n"
        "The final comparison has a positive correction.\n"
        "Therefore the answer is strictly above L.\n",
        encoding="utf-8",
    )

    result = registry.execute("ResearchProgressReview", {"path": "."})

    assert not result.is_error
    assert "possible underclaimed proof statements: 1" in result.content
    assert "verified_propositions/answer.md" in result.content
    assert "general next-action rule" in result.content
    assert "suggested next proposition: make the selected underclaimed proof-tail conclusion explicit" in result.content
    assert "selected target: verified_propositions/answer.md" in result.content
    assert "strictly above L" in result.content


def test_research_progress_review_prioritizes_shallow_answer_changing_candidates(registry, tmp_path: Path):
    verified_dir = tmp_path / "verified_propositions"
    technical_dir = verified_dir / "technical"
    technical_dir.mkdir(parents=True)
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    (tmp_path / "problem.md").write_text("Decide the final response.\n", encoding="utf-8")
    (knowledge_dir / "notes.md").write_text(
        "## Statement\n"
        "A planning note records the target.\n\n"
        "## Proof\n"
        "Therefore the planning note has a stronger result than the summary.\n",
        encoding="utf-8",
    )
    (technical_dir / "technical-check.md").write_text(
        "## Statement\n"
        "A local helper check holds.\n\n"
        "## Proof\n"
        "Therefore the auxiliary value is positive.\n"
        "Hence the local check is > 0 in this case.\n",
        encoding="utf-8",
    )
    (verified_dir / "main-result.md").write_text(
        "## Statement\n"
        "The answer reaches L.\n\n"
        "## Proof\n"
        "The construction gives the stronger chain\n"
        "answer >= L + adjustment > L.\n"
        "Hence the answer is strictly above L.\n",
        encoding="utf-8",
    )

    result = registry.execute("ResearchProgressReview", {"path": "."})

    assert not result.is_error
    main_pos = result.content.index("verified_propositions/main-result.md")
    technical_pos = result.content.index("verified_propositions/technical/technical-check.md")
    assert main_pos < technical_pos
    assert "selected target: verified_propositions/main-result.md" in result.content
    assert "selected target: knowledge/notes.md" not in result.content
