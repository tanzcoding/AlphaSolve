"""RoleWorkspaceAccess 角色策略 classmethod 的单元测试。

每个测试断言对应 classmethod 产出的 access 对象的关键字段——
这是 worker.py / orchestrator.py / curator.py 原本散落在 13 处的策略集中守门。
"""
from __future__ import annotations

import pytest

from alphasolve.agent import Workspace
from alphasolve.solver.workspace_access import RoleWorkspaceAccess


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path)


def test_generator_locks_to_single_proposition_file(ws):
    access = RoleWorkspaceAccess.generator(ws, "unverified_propositions/prop-abc")
    assert access.worker_rel == "unverified_propositions/prop-abc"
    assert access.deny_other_unverified is True
    assert access.single_proposition_file is True
    # 未指定 write_root_rel / exact_write_rel —— 写入只能走 single_proposition_file 路径
    assert access.write_root_rel is None
    assert access.exact_write_rel is None


def test_worker_read_only_disables_writes(ws):
    access = RoleWorkspaceAccess.worker_read_only(ws, "unverified_propositions/prop-xyz")
    assert access.worker_rel == "unverified_propositions/prop-xyz"
    assert access.deny_other_unverified is True
    # 三个写许可字段全部缺席 → write 系列方法会抛 ValueError
    assert access.single_proposition_file is False
    assert access.write_root_rel is None
    assert access.exact_write_rel is None


def test_verifier_attempt_blocks_review_md_and_other_attempts(ws):
    access = RoleWorkspaceAccess.verifier_attempt(
        ws,
        "unverified_propositions/prop-abc",
        all_verifier_ws_rel="unverified_propositions/prop-abc/verifier_workspace",
        config_name="verifier_stepwise",
    )
    assert access.deny_other_unverified is True
    assert "unverified_propositions/prop-abc/verifier_workspace" in access.deny_read_rels
    assert access.deny_read_file_names == ("review.md",)
    # 非 citation 策略不阻断 knowledge/
    assert "knowledge" not in access.deny_read_rels


def test_verifier_citation_additionally_blocks_knowledge(ws):
    access = RoleWorkspaceAccess.verifier_attempt(
        ws,
        "unverified_propositions/prop-abc",
        all_verifier_ws_rel="unverified_propositions/prop-abc/verifier_workspace",
        config_name="verifier_citation",
    )
    assert "knowledge" in access.deny_read_rels
    assert "unverified_propositions/prop-abc/verifier_workspace" in access.deny_read_rels


def test_theorem_checker_scopes_reads_to_verified(ws):
    access = RoleWorkspaceAccess.theorem_checker(ws, "unverified_propositions/prop-abc")
    assert access.read_root_rel == "verified_propositions"
    assert access.deny_other_unverified is True


def test_reviser_locks_writes_to_proposition_file(ws):
    access = RoleWorkspaceAccess.reviser(
        ws,
        "unverified_propositions/prop-abc",
        proposition_rel="unverified_propositions/prop-abc/proposition.md",
    )
    assert access.exact_write_rel == "unverified_propositions/prop-abc/proposition.md"
    assert access.deny_other_unverified is True


def test_orchestrator_owns_verified_root(ws):
    access = RoleWorkspaceAccess.orchestrator(ws)
    assert access.write_root_rel == "verified_propositions"
    assert access.destructive_protected_file_names == ("index.md", "state.md")
    assert access.preserve_markdown_file_names_on_rename is False
    # orchestrator 不绑定 worker_rel —— 它在 layout 顶层操作
    assert access.worker_rel is None


def test_orchestrator_subagent_is_read_only_excluding_unverified(ws):
    access = RoleWorkspaceAccess.orchestrator_subagent(ws)
    assert access.write_root_rel is None
    assert access.deny_read_rel == "unverified_propositions"


def test_curator_reads_portfolio_evidence_but_writes_only_knowledge(ws):
    access = RoleWorkspaceAccess.curator(ws)
    assert access.read_root_rels == ("knowledge", "progress_audits", "curation_records")
    assert access.write_root_rel == "knowledge"
    assert access.deny_text_write_rels == ("knowledge/references",)
    assert access.protected_reference_rels == ("knowledge/references",)
    assert access.destructive_protected_file_names == ("index.md", "common-errors.md")


def test_curator_reference_text_guardrails(ws, tmp_path):
    references = tmp_path / "knowledge" / "references"
    references.mkdir(parents=True)
    (references / "source.md").write_text("A\nB\nC\n", encoding="utf-8")
    (references / "parts").mkdir()
    (tmp_path / "knowledge" / "note.md").write_text("# Note\n", encoding="utf-8")

    access = RoleWorkspaceAccess.curator(ws)
    with pytest.raises(ValueError, match="Write/Edit cannot modify"):
        access.write_text("knowledge/references/new.md", "agent text")
    with pytest.raises(ValueError, match="Write/Edit cannot modify"):
        access.edit("knowledge/references/source.md", "A", "changed")

    result = access.split_reference_file(
        "knowledge/references/source.md",
        [{"path": "knowledge/references/parts/part-1.md", "start_line": 1, "end_line": 2}],
    )
    assert result["paths"] == ["knowledge/references/parts/part-1.md"]
    assert (references / "parts" / "part-1.md").read_text(encoding="utf-8") == "A\nB\n"
    assert (references / "source.md").read_text(encoding="utf-8") == "A\nB\nC\n"

    with pytest.raises(ValueError, match="protected reference boundary"):
        access.move_file("knowledge/note.md", "knowledge/references/parts")
    with pytest.raises(ValueError, match="delete is not allowed"):
        access.delete_path("knowledge/references/source.md")


def test_curator_subagent_is_portfolio_read_only(ws):
    access = RoleWorkspaceAccess.curator_subagent(ws)
    assert access.read_root_rels == ("knowledge", "progress_audits", "curation_records")
    assert access.write_root_rel is None


def test_policy_semantics_smoke_write(ws, tmp_path):
    """端到端验证 generator policy：只能写 proposition.md，写别的文件抛错。"""
    (tmp_path / "unverified_propositions" / "prop-abc").mkdir(parents=True)
    access = RoleWorkspaceAccess.generator(ws, "unverified_propositions/prop-abc")
    # 允许写 proposition.md
    rel = access.write_text(
        "unverified_propositions/prop-abc/proposition.md",
        "Statement: foo\nProof: bar",
    )
    assert rel == "unverified_propositions/prop-abc/proposition.md"
    # 拒绝写其他文件名
    with pytest.raises(ValueError, match="proposition.md"):
        access.write_text(
            "unverified_propositions/prop-abc/other.md",
            "should fail",
        )


def test_policy_semantics_smoke_read_only(ws, tmp_path):
    """worker_read_only policy：写入应被拒绝。"""
    (tmp_path / "unverified_propositions" / "prop-abc").mkdir(parents=True)
    (tmp_path / "unverified_propositions" / "prop-abc" / "proposition.md").write_text(
        "stmt", encoding="utf-8"
    )
    access = RoleWorkspaceAccess.worker_read_only(ws, "unverified_propositions/prop-abc")
    # 允许读
    page = access.read_text_page("unverified_propositions/prop-abc/proposition.md")
    assert "stmt" in page.output
    # 拒绝写
    with pytest.raises(ValueError, match="write_file is not enabled"):
        access.write_text(
            "unverified_propositions/prop-abc/anything.md",
            "x",
        )
