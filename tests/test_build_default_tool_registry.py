"""第二层 build_default_tool_registry 注册基础工具的测试。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alphasolve.agent import Workspace  # noqa: E402
from alphasolve.agent.tools import build_default_tool_registry  # noqa: E402
from alphasolve.solver.tool_runtime import register_research_markdown_tools  # noqa: E402


@pytest.fixture
def registry(tmp_path: Path):
    ws = Workspace(root=tmp_path)
    return build_default_tool_registry(ws)


@pytest.fixture
def research_registry(tmp_path: Path):
    ws = Workspace(root=tmp_path)
    registry = build_default_tool_registry(ws)
    register_research_markdown_tools(registry, ws)
    return registry


def test_registers_all_basic_tools(registry):
    names = {tool.name for tool in registry.registered_tools()}
    expected = {
        "Read", "Write", "Edit", "MakeDir", "Rename", "Move", "Delete",
        "Glob", "ListDir", "Grep", "GetCurrentTime",
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


def test_inspect_markdown_surfaces_statement_and_tail(research_registry, tmp_path: Path):
    (tmp_path / "proof.md").write_text(
        "# Proposition\n"
        "Statement: enough conditions imply the target result.\n\n"
        "## Proof\n"
        "The middle computation is routine.\n"
        "Therefore target_value > baseline_value.\n",
        encoding="utf-8",
    )

    result = research_registry.execute("InspectMarkdown", {"path": "proof.md"})

    assert not result.is_error
    assert "== proof.md ==" in result.content
    assert "Statement: enough conditions imply the target result." in result.content
    assert "important-looking lines near file end:" in result.content
    assert "Therefore target_value > baseline_value." in result.content


def test_default_read_markdown_has_no_research_tail_hint(registry, tmp_path: Path):
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
    assert "Markdown proof-review hint" not in result.content
    assert "strictly greater than L" in result.content


def test_research_read_markdown_warns_when_proof_tail_outclaims_statement(research_registry, tmp_path: Path):
    (tmp_path / "proof.md").write_text(
        "## Statement\n"
        "The target quantity is at least L.\n\n"
        "## Proof\n"
        "After the main estimate, the remaining correction is positive.\n"
        "Therefore the target quantity is strictly greater than L.\n",
        encoding="utf-8",
    )

    result = research_registry.execute("Read", {"path": "proof.md", "read_all": True})

    assert not result.is_error
    assert "Markdown proof-review hint" in result.content
    assert "proof tail" in result.content
    assert "possible underclaimed proof" in result.content
    assert "research plan should normally first create an explicit Statement" in result.content
    assert "proposition-index gap" in result.content
    assert "strictly greater than L" in result.content


def test_research_read_default_markdown_statement_tail_preview(research_registry, tmp_path: Path):
    middle_lines = "\n".join(f"Middle proof computation {index}." for index in range(1, 80))
    (tmp_path / "proof.md").write_text(
        "## Statement\n"
        "The target quantity is at least L.\n\n"
        "## Proof\n"
        f"{middle_lines}\n"
        "Therefore the target quantity is strictly greater than L.\n",
        encoding="utf-8",
    )

    result = research_registry.execute("Read", {"path": "proof.md"})

    assert not result.is_error
    assert "Default research Read preview" in result.content
    assert "The target quantity is at least L." in result.content
    assert "Middle proof computation 1." not in result.content
    assert "Middle proof computation 79." in result.content
    assert "Therefore the target quantity is strictly greater than L." in result.content
    assert "read_all=true" in result.content


def test_research_read_markdown_does_not_warn_without_statement_and_proof(research_registry, tmp_path: Path):
    (tmp_path / "problem.md").write_text(
        "Decide the final response.\n\n"
        "The answer should be compared against the reference value.\n",
        encoding="utf-8",
    )

    result = research_registry.execute("Read", {"path": "problem.md", "read_all": True})

    assert not result.is_error
    assert "Markdown proof-review hint" not in result.content


def test_list_dir_does_not_expose_research_tool_hint_from_default_registry(registry, tmp_path: Path):
    (tmp_path / "problem.md").write_text("Problem\n", encoding="utf-8")
    (tmp_path / "verified_propositions").mkdir()
    (tmp_path / "knowledge").mkdir()

    result = registry.execute("ListDir", {"path": "."})

    assert not result.is_error
    assert "ResearchProgressReview" not in result.content
    assert "InspectMarkdown" not in result.content


def test_research_progress_review_surfaces_workspace_and_attempt_notes(research_registry, tmp_path: Path):
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

    result = research_registry.execute("ResearchProgressReview", {"path": "."})

    assert not result.is_error
    assert "review stopping guide" in result.content
    assert "scoped exploration guide" in result.content
    assert "priority guide" in result.content
    assert "workspace notes" in result.content
    assert "The target is not complete yet." in result.content
    assert "A compact interface step is still missing." in result.content
    assert "unfinished attempt notes" in result.content
    assert "failed because it skipped a prerequisite" in result.content


def test_research_progress_review_surfaces_repairable_failed_attempt(research_registry, tmp_path: Path):
    (tmp_path / "problem.md").write_text("Decide the final response.\n", encoding="utf-8")
    (tmp_path / "verified_propositions").mkdir()
    (tmp_path / "knowledge").mkdir()
    attempt_dir = tmp_path / "unverified_propositions" / "attempt-a"
    attempt_dir.mkdir(parents=True)
    (attempt_dir / "review.md").write_text(
        "## Review\n\n"
        "**Candidate**: `unverified_propositions/attempt-a/proposition.md`\n\n"
        "### Critical Gap Found\n"
        "One local step is missing.\n\n"
        "### Assessment\n"
        "The overall architecture is sound and the gap is localized and fixable.\n\n"
        "### Verdict: fail\n",
        encoding="utf-8",
    )

    result = research_registry.execute("ResearchProgressReview", {"path": "."})

    assert not result.is_error
    assert "repairable attempt candidates" in result.content
    assert "evidence budget" in result.content
    assert "unverified_propositions/attempt-a/review.md" in result.content
    assert "selected target: unverified_propositions/attempt-a/review.md" in result.content
    assert "localized and fixable" in result.content


def test_research_progress_review_prioritizes_unreviewed_high_level_attempt(research_registry, tmp_path: Path):
    (tmp_path / "problem.md").write_text("Decide the final response.\n", encoding="utf-8")
    (tmp_path / "verified_propositions").mkdir()
    (tmp_path / "knowledge").mkdir()
    attempt_dir = tmp_path / "unverified_propositions" / "attempt-final"
    attempt_dir.mkdir(parents=True)
    (attempt_dir / "proposition.md").write_text(
        "## Statement\n"
        "This final assembly claims a complete solution using several cited pieces.\n\n"
        "## Proof\n"
        "The argument closes the main interface.\n",
        encoding="utf-8",
    )
    (attempt_dir / "worker_hint.md").write_text(
        "## Target\n\n"
        "Finish the end-to-end assembly. Check each hypothesis, interface, restriction, and level change.\n",
        encoding="utf-8",
    )

    result = research_registry.execute("ResearchProgressReview", {"path": "."})

    assert not result.is_error
    assert "unreviewed high-level attempts" in result.content
    assert "selected target: unverified_propositions/attempt-final/proposition.md" in result.content
    assert "make the high-level attempt's exact interface explicit" in result.content


def test_research_progress_review_surfaces_underclaimed_proof(research_registry, tmp_path: Path):
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

    result = research_registry.execute("ResearchProgressReview", {"path": "."})

    assert not result.is_error
    assert "high-priority proposition-index gap" in result.content
    assert "highest-priority underclaimed proof statements: 1 shown" in result.content
    assert "workspace notes" in result.content
    assert "verified_propositions/answer.md" in result.content
    assert "general next-action rule" in result.content
    assert "recommended first check: make an already-established proof-tail conclusion explicit" in result.content
    assert "selected target: verified_propositions/answer.md" in result.content
    assert "strictly above L" in result.content


def test_research_progress_review_keeps_context_for_strict_tail_that_later_weakens(
    research_registry,
    tmp_path: Path,
):
    (tmp_path / "problem.md").write_text("Find the objective value.\n", encoding="utf-8")
    verified_dir = tmp_path / "verified_propositions"
    verified_dir.mkdir()
    (tmp_path / "knowledge").mkdir()
    (verified_dir / "objective.md").write_text(
        "## Statement\n"
        "The objective is at least L.\n\n"
        "## Proof\n"
        "A direct construction gives the objective value\n"
        "objective >= L + positive_correction\n"
        "> L.\n"
        "Hence the objective is at least L.\n",
        encoding="utf-8",
    )

    result = research_registry.execute("ResearchProgressReview", {"path": "."})

    assert not result.is_error
    assert "selected target: verified_propositions/objective.md" in result.content
    assert "objective >= L + positive_correction" in result.content
    assert "> L" in result.content


def test_research_progress_review_shows_selected_verified_underclaim_first(
    research_registry,
    tmp_path: Path,
):
    (tmp_path / "problem.md").write_text("Find the contest objective value.\n", encoding="utf-8")
    verified_dir = tmp_path / "verified_propositions"
    knowledge_dir = tmp_path / "knowledge"
    verified_dir.mkdir()
    knowledge_dir.mkdir()
    (knowledge_dir / "technical-condition.md").write_text(
        "## Statement\n"
        "A technical condition is useful.\n\n"
        "## Proof\n"
        "A side calculation is strictly negative under this condition.\n",
        encoding="utf-8",
    )
    (verified_dir / "objective.md").write_text(
        "## Statement\n"
        "The contest objective is at least L.\n\n"
        "## Proof\n"
        "The construction gives the contest objective value\n"
        "contest objective >= L + correction\n"
        "> L.\n"
        "Hence the contest objective is at least L.\n",
        encoding="utf-8",
    )

    result = research_registry.execute("ResearchProgressReview", {"path": "."})

    assert not result.is_error
    selected_pos = result.content.index("selected target: verified_propositions/objective.md")
    verified_pos = result.content.index("- verified_propositions/objective.md")
    knowledge_pos = result.content.index("- knowledge/technical-condition.md")
    assert selected_pos >= 0
    assert verified_pos < knowledge_pos


def test_research_read_index_prioritizes_problem_focused_strict_tail_context(
    research_registry,
    tmp_path: Path,
):
    (tmp_path / "problem.md").write_text("Find ticket_1985 objective value.\n", encoding="utf-8")
    verified_dir = tmp_path / "verified_propositions"
    verified_dir.mkdir()
    (verified_dir / "index.md").write_text("# Index\n", encoding="utf-8")
    (verified_dir / "local.md").write_text(
        "## Statement\n"
        "A local monotonicity fact holds.\n\n"
        "## Proof\n"
        "Set h(r)=r-1.\n"
        "Hence h(r)>0 for r>1.\n",
        encoding="utf-8",
    )
    (verified_dir / "objective.md").write_text(
        "## Statement\n"
        "The ticket_1985 objective is at least L.\n\n"
        "## Proof\n"
        "The construction gives\n"
        "ticket_1985 objective >= L + correction\n"
        "> L.\n"
        "Hence the ticket_1985 objective is at least L.\n",
        encoding="utf-8",
    )

    result = research_registry.execute("Read", {"path": "verified_propositions/index.md", "read_all": True})

    assert not result.is_error
    objective_pos = result.content.index("verified_propositions/objective.md")
    local_pos = result.content.index("verified_propositions/local.md")
    assert objective_pos < local_pos
    assert "ticket_1985 objective >= L + correction" in result.content
    assert "> L" in result.content


def test_research_progress_review_ignores_markdown_blockquote_marker_as_strict_tail(
    research_registry,
    tmp_path: Path,
):
    (tmp_path / "problem.md").write_text("Assess the argument.\n", encoding="utf-8")
    verified_dir = tmp_path / "verified_propositions"
    verified_dir.mkdir()
    (tmp_path / "knowledge").mkdir()
    (verified_dir / "quoted.md").write_text(
        "## Statement\n"
        "The cited argument applies.\n\n"
        "## Proof\n"
        "The final paragraph quotes a note.\n"
        "> Therefore the cited argument applies.\n",
        encoding="utf-8",
    )

    result = research_registry.execute("ResearchProgressReview", {"path": "."})

    assert not result.is_error
    assert "verified_propositions/quoted.md" not in result.content
    assert "Therefore the cited argument applies" not in result.content


def test_research_progress_review_does_not_promote_proof_mechanics_when_statement_covers_result(
    research_registry,
    tmp_path: Path,
):
    (tmp_path / "problem.md").write_text("Establish the final containment property.\n", encoding="utf-8")
    verified_dir = tmp_path / "verified_propositions"
    verified_dir.mkdir()
    (tmp_path / "knowledge").mkdir()
    (verified_dir / "containment.md").write_text(
        "## Statement\n"
        "For every time t, the supported state stays inside the certified region R(t).\n\n"
        "## Proof\n"
        "The local energy on an auxiliary box vanishes.\n"
        "Therefore the state is zero outside the half-space x < s + t.\n"
        "Intersecting the half-spaces gives the certified region R(t).\n",
        encoding="utf-8",
    )

    result = research_registry.execute("ResearchProgressReview", {"path": "."})

    assert not result.is_error
    assert "compact review mode" not in result.content
    assert "primary research action signal" not in result.content
    assert "verified_propositions/containment.md" not in result.content
    assert "state is zero outside the half-space" not in result.content


def test_research_progress_review_prioritizes_shallow_answer_changing_candidates(research_registry, tmp_path: Path):
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

    result = research_registry.execute("ResearchProgressReview", {"path": "."})

    assert not result.is_error
    main_pos = result.content.index("verified_propositions/main-result.md")
    technical_pos = result.content.index("verified_propositions/technical/technical-check.md")
    assert main_pos < technical_pos
    assert "selected target: verified_propositions/main-result.md" in result.content
    assert "selected target: knowledge/notes.md" not in result.content


def test_research_progress_review_prefers_problem_focused_tail(research_registry, tmp_path: Path):
    verified_dir = tmp_path / "verified_propositions"
    verified_dir.mkdir()
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "problem.md").write_text("Resolve ticket_2026 and report final_code.\n", encoding="utf-8")
    (verified_dir / "generic-result.md").write_text(
        "## Statement\n"
        "A generic result reaches L.\n\n"
        "## Proof\n"
        "Therefore the generic result is strictly above L.\n",
        encoding="utf-8",
    )
    (verified_dir / "ticket-result.md").write_text(
        "## Statement\n"
        "The ticket_2026 result reaches L.\n\n"
        "## Proof\n"
        "The final_code calculation gives an extra positive adjustment.\n"
        "Therefore ticket_2026 final_code is strictly above L.\n",
        encoding="utf-8",
    )

    result = research_registry.execute("ResearchProgressReview", {"path": "."})

    assert not result.is_error
    assert "selected target: verified_propositions/ticket-result.md" in result.content


def test_research_progress_review_surfaces_choice_classification_signal(research_registry, tmp_path: Path):
    (tmp_path / "problem.md").write_text(
        "Prove there exists a model and parameter choice satisfying the theorem hypotheses.\n",
        encoding="utf-8",
    )
    verified_dir = tmp_path / "verified_propositions"
    knowledge_dir = tmp_path / "knowledge"
    verified_dir.mkdir()
    knowledge_dir.mkdir()
    (verified_dir / "conditional-chain.md").write_text(
        "## Statement\n"
        "A conditional theorem chain proves the target if hypotheses H1-H3 hold.\n\n"
        "## Proof\n"
        "The verified infrastructure establishes the conditional theorem chain.\n",
        encoding="utf-8",
    )
    (knowledge_dir / "choices.md").write_text(
        "Remaining blocker: choose admissible parameters and input families so the hypotheses hold.\n",
        encoding="utf-8",
    )

    result = research_registry.execute("ResearchProgressReview", {"path": "."})

    assert not result.is_error
    assert "selected target: choice-classification proposition over admissible models" in result.content
    assert "choice-classification proposition before proposing stronger general analytic machinery" in result.content
    assert "conditional assembly with the choice-dependent hypothesis left" in result.content
    assert "support, positivity/lower-bound, nonlinear remainder, regularity" in result.content
    assert "verified-infrastructure guardrail" in result.content
    assert "method-change guardrail" in result.content


def test_research_progress_review_does_not_make_local_repair_default_over_interface(research_registry, tmp_path: Path):
    (tmp_path / "problem.md").write_text("Assemble the final energy interface.\n", encoding="utf-8")
    unverified_dir = tmp_path / "unverified_propositions" / "prop-local"
    unverified_dir.mkdir(parents=True)
    (tmp_path / "verified_propositions").mkdir()
    (tmp_path / "knowledge").mkdir()
    (unverified_dir / "review.md").write_text(
        "## Verdict\n"
        "fail but repairable. The proof is mostly correct and the gap is a one-line localized fix.\n",
        encoding="utf-8",
    )

    result = research_registry.execute("ResearchProgressReview", {"path": "."})

    assert not result.is_error
    assert "repair is only one component needed to make levels, constants, remainders, and restrictions compose" in result.content
    assert "decide whether this should be a direct repair or a named prerequisite/subclaim" in result.content


def test_research_progress_review_ignores_tail_already_in_statement(research_registry, tmp_path: Path):
    verified_dir = tmp_path / "verified_propositions"
    verified_dir.mkdir()
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "problem.md").write_text("Resolve ticket_2026.\n", encoding="utf-8")
    (verified_dir / "repeated.md").write_text(
        "## Statement\n"
        "The ticket_2026 value is at least 5.\n\n"
        "## Proof\n"
        "Therefore the ticket_2026 value is at least 5.\n",
        encoding="utf-8",
    )
    (verified_dir / "stronger.md").write_text(
        "## Statement\n"
        "The ticket_2026 value is at least 5.\n\n"
        "## Proof\n"
        "Therefore the ticket_2026 value is strictly above 5.\n",
        encoding="utf-8",
    )

    result = research_registry.execute("ResearchProgressReview", {"path": "."})

    assert not result.is_error
    assert "verified_propositions/repeated.md" not in result.content
    assert "selected target: verified_propositions/stronger.md" in result.content
