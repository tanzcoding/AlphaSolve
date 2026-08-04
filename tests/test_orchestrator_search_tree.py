"""编排器搜索树深挖 + 工具接线的回归测试。

覆盖：
1. `SpawnFreeExploration` 已进入 orchestrator 的工具白名单（替代旧的 `RecordDuel`）。
2. `Orchestrator._spawn_tool` 在显式传 `parent_id` 时会挂到该节点下；未传时缺省挂 root；
   若传入的 `parent_id` 指向不存在的节点，则静默回退挂 root，不报错。
   调度决策（该挂到哪个节点）完全由 orchestrator LLM 通过显式 `parent_id`/`parent_ids`
   传达，代码层不再有任何自动推断（如历史上的 bandit exploit arm）。
"""
from __future__ import annotations

from pathlib import Path

import alphasolve.solver as solver_pkg
from alphasolve.agent import load_agent_suite
from alphasolve.solver.blocker_registry import PersistentBlockerRegistry
from alphasolve.solver.orchestrator import Orchestrator
from alphasolve.solver.research_state import ResearchStateStore
from alphasolve.solver.search.session import SearchSession


class _PermissiveBlockerRegistry:
    def dispatch_preflight(self, **_kwargs):
        return {"allowed": True, "blockers": []}

    def pending_checkpoint_ids(self):
        return []

    def load(self):
        return {"blockers": {}}


class _FakeManager:
    """最小 WorkerManager 替身：只满足 _spawn_tool 用到的接口。"""

    solved_result = None
    solution_path = None

    def __init__(self) -> None:
        self._n = 0

    def spawn(self, hint, **kwargs):  # noqa: ANN001
        self._n += 1
        return {
            "spawned": True,
            "worker_id": f"w{self._n}",
            "direction_id": kwargs.get("direction_id"),
            "gap_id": kwargs.get("gap_id"),
        }


def _make_orch() -> Orchestrator:
    # 绕过重型 __init__，只装配 _spawn_tool 需要的字段。
    orch = object.__new__(Orchestrator)
    orch.search = SearchSession()
    orch._search_tree_sink = None
    orch.curator_queue = None
    orch.blocker_registry = _PermissiveBlockerRegistry()
    return orch


def test_research_feedback_tools_exposed_to_orchestrator():
    config_dir = Path(solver_pkg.__file__).parent / "config"
    suite = load_agent_suite(config_dir)
    tools = list(suite.agents["orchestrator"].tools)
    assert "SpawnFreeExploration" in tools
    assert "RegisterResearchRoute" in tools
    assert "AssessResearchRoute" in tools
    assert "RecordResearchImpact" in tools
    assert "RecordDispatchConstraint" in tools
    assert "SyncResearchState" in tools


def test_record_research_impact_updates_search_and_direction_state(tmp_path):
    orch = _make_orch()
    orch.research_state = ResearchStateStore(tmp_path / "verified_propositions")
    orch.research_state.sync(
        {
            "objective_summary": "Exact answer unknown.",
            "directions": [
                {
                    "direction_id": "D1",
                    "title": "Tightness route",
                    "goal": "Prove the current bound is tight.",
                    "status": "active",
                    "health": "unassessed",
                    "gaps": [{"gap_id": "G1", "statement": "Close the main gap.", "status": "open"}],
                    "plan": ["Try one targeted implication."],
                }
            ],
        }
    )
    node = orch.search.on_spawn("w1", "prove the target", direction_id="D1", gap_id="G1")
    orch.search.on_worker_result(
        {"worker_id": "w1", "status": "verified", "summary": "only a necessary condition", "solved_problem": False}
    )

    result = orch._record_research_impact_tool(
        {
            "worker_id": "w1",
            "relation_to_target": "necessary_condition_only",
            "gap_effect": "unchanged",
            "continuation_value": "low",
            "remaining_gaps": ["Close the main gap."],
            "summary": "Correct but weak; the assigned gap remains open.",
        }
    )

    assert not result.is_error
    assert node.impact_status == "assessed"
    assert node.insight.verified_implications == []
    direction = orch.research_state.load()["directions"]["D1"]
    assert direction["progress"]["attempts"] == 1
    assert direction["steps"]["G1"]["status"] == "open"
    assert "Correct but weak" in orch.research_state.markdown_path.read_text(encoding="utf-8")


def test_registered_route_owns_spawned_worker_and_search_node(tmp_path):
    orch = _make_orch()
    orch.research_state = ResearchStateStore(tmp_path / "verified_propositions")
    orch.research_state.sync({
        "objective_summary": "Open",
        "directions": [{
            "direction_id": "D1", "title": "Reviewer route", "goal": "Close the main target",
            "status": "active", "health": "unassessed",
            "gaps": [{"gap_id": "G1", "statement": "Prove the missing implication", "status": "open"}],
        }],
    })
    state_id = orch.research_state.reviewer_snapshot()["state_id"]
    registered = orch._register_research_route_tool({
        "route_id": "route-main",
        "based_on_state_id": state_id,
        "direction_id": "D1",
        "gap_id": "G1",
        "route_claim": "This implication is the best next route.",
        "target": "Prove the missing implication.",
        "success_condition": "The main gap closes.",
        "stop_condition": "A verified counterexample refutes it.",
    })
    assert not registered.is_error
    manager = _FakeManager()
    unassigned = orch._spawn_tool(manager, {
        "hint": "Try an untracked branch.",
        "direction_id": "D1",
        "gap_id": "G1",
    })
    assert '"reason": "active_route_required"' in unassigned.content
    assert manager._n == 0

    result = orch._spawn_tool(manager, {
        "route_id": "route-main",
        "hint": "Try one branch under the reviewer route.",
        "method_id": "construction",
    })

    assert not result.is_error
    node = orch.search.graph.get(orch.search.root.children[0])
    assert node.route_id == "route-main"
    assert node.direction_id == "D1"
    assert node.gap_id == "G1"
    route = orch.research_state.load()["routes"]["route-main"]
    assert route["worker_ids"] == ["w1"]


def test_spawn_on_blank_state_uses_bootstrap_direction_and_remains_assessable(tmp_path):
    orch = _make_orch()
    verified = tmp_path / "verified_propositions"
    verified.mkdir()
    (verified / "state.md").write_text("", encoding="utf-8")
    orch.research_state = ResearchStateStore(verified)
    orch.research_state.ensure_initialized()
    manager = _FakeManager()

    result = orch._spawn_tool(manager, {"hint": "legacy targeted hint without ids"})

    assert not result.is_error
    node = orch.search.graph.get(orch.search.root.children[0])
    assert node.direction_id == "bootstrap-direction"
    assert node.gap_id == "bootstrap-gap"
    state = orch.research_state.load()
    assert "bootstrap-direction" in state["directions"]
    assert "bootstrap-gap" in state["directions"]["bootstrap-direction"]["steps"]
    assert "bootstrap-direction" in orch.research_state.markdown_path.read_text(encoding="utf-8")


def test_nonempty_legacy_state_requires_sync_before_spawn(tmp_path):
    orch = _make_orch()
    verified = tmp_path / "verified_propositions"
    verified.mkdir()
    legacy = "# Verified Propositions State\n\n- Keep this old direction.\n"
    (verified / "state.md").write_text(legacy, encoding="utf-8")
    orch.research_state = ResearchStateStore(verified)
    manager = _FakeManager()

    result = orch._spawn_tool(manager, {"hint": "do not overwrite legacy state"})

    assert not result.is_error
    assert '"reason": "research_state_migration_required"' in result.content
    assert manager._n == 0
    assert orch.research_state.markdown_path.read_text(encoding="utf-8") == legacy


def test_curated_blocker_gates_other_spawns_and_audit_remains_candidate(tmp_path):
    orch = _make_orch()
    orch.research_state = ResearchStateStore(tmp_path / "verified_propositions")
    orch.research_state.sync({
        "objective_summary": "Open",
        "directions": [{
            "direction_id": "D1", "title": "Route", "goal": "Resolve the global gap",
            "status": "active", "health": "unassessed",
            "gaps": [
                {"gap_id": "G1", "statement": "Old generic route", "status": "open"},
                {"gap_id": "G2", "statement": "Side lemma", "status": "open"},
            ],
        }],
    })
    workspace = tmp_path / "workspace"
    checkpoint_dir = workspace / "progress_audits" / "checkpoint-0005"
    checkpoint_dir.mkdir(parents=True)
    (checkpoint_dir / "decision.json").write_text(
        '{"checkpoint_id":"checkpoint-0005","status":"completed","watermark":2}', encoding="utf-8"
    )
    (workspace / "progress_audit_outcomes.jsonl").write_text(
        '{"sequence":1,"direction_id":"D1","gap_id":"G1","method_id":"counting"}\n'
        '{"sequence":2,"direction_id":"D1","gap_id":"G2","method_id":"induction"}\n',
        encoding="utf-8",
    )
    orch.blocker_registry = PersistentBlockerRegistry(workspace)
    orch.blocker_registry.record_curation(
        checkpoint_id="checkpoint-0005",
        blockers=[{
            "blocker_id": "cross-term",
            "statement": "Bound the missing cross term without assuming the target.",
            "gate": {"direction_id": "D1", "gap_id": "cross-term-blocker"},
            "approaches": [
                {
                    "direction_id": "D1", "gap_id": "G1", "method_id": "counting",
                    "description": "Counting route leaves the cross term uncontrolled.", "outcome_sequences": [1],
                },
                {
                    "direction_id": "D1", "gap_id": "G2", "method_id": "induction",
                    "description": "Inductive route leaves the same cross term uncontrolled.", "outcome_sequences": [2],
                },
            ],
        }],
        resolved_blocker_ids=[],
        current_difficulties=[{
            "difficulty_id": "cross-term-obligation",
            "statement": "Bound the missing cross term without assuming the target.",
            "gate": {"direction_id": "D1", "gap_id": "cross-term-blocker"},
            "blocker_id": "cross-term",
        }],
        blocker_relations=[],
    )
    payload = {
        "progress_audit": {"latest": {"repeated_avoided_obligation": {
            "direction_id": "D1", "gap_id": "audit-candidate", "statement": "Auditor lead only.",
        }}}
    }
    candidate = orch._materialize_process_audit_blocker(payload, orch.research_state)
    assert candidate is not None
    assert payload["process_audit_blocker_candidate"]["gap_id"] == "audit-candidate"
    manager = _FakeManager()

    blocked = orch._spawn_tool(manager, {"hint": "Try a side lemma", "direction_id": "D1", "gap_id": "G2"})
    assert '"reason": "active_curated_blocker"' in blocked.content
    direct = orch._spawn_tool(manager, {"hint": "Attack the cross term", "direction_id": "D1", "gap_id": "cross-term-blocker"})
    assert '"spawned": true' in direct.content
    spawned = orch.search.graph.get(orch.search.root.children[-1])
    assert "Active Curated Blocker" in spawned.hypothesis


def test_refuted_dispatch_constraint_blocks_all_retries_before_spawn(tmp_path):
    orch = _make_orch()
    orch.research_state = ResearchStateStore(tmp_path / "verified_propositions")
    orch.research_state.sync({
        "objective_summary": "Open",
        "directions": [{
            "direction_id": "D1", "title": "Route", "goal": "Prove the proposed bound",
            "status": "active", "health": "unassessed",
            "gaps": [{"gap_id": "G1", "statement": "Prove the exact bound", "status": "open"}],
        }],
    })
    recorded = orch._record_dispatch_constraint_tool({
        "direction_id": "D1",
        "gap_id": "G1",
        "claim": "Every instance satisfies the proposed bound.",
        "status": "refuted",
        "evidence_level": "explicit_witness",
        "evidence": "An explicit counterexample violates the target.",
        "source_paths": ["knowledge/counterexample.md"],
    })
    assert not recorded.is_error
    manager = _FakeManager()

    blocked = orch._spawn_tool(manager, {
        "hint": "Retry the refuted theorem.",
        "direction_id": "D1",
        "gap_id": "G1",
        "consolidation": True,
        "pinned_target": "Every instance satisfies the proposed bound.",
    })

    assert '"reason": "target_refuted"' in blocked.content
    assert manager._n == 0


def test_sampled_constraint_requires_falsification_worker(tmp_path):
    orch = _make_orch()
    orch.research_state = ResearchStateStore(tmp_path / "verified_propositions")
    orch.research_state.sync({
        "objective_summary": "Open",
        "directions": [{
            "direction_id": "D1", "title": "Route", "goal": "Prove the proposed bound",
            "status": "active", "health": "unassessed",
            "gaps": [{"gap_id": "G1", "statement": "Prove the exact bound", "status": "open"}],
        }],
    })
    orch._record_dispatch_constraint_tool({
        "direction_id": "D1",
        "gap_id": "G1",
        "claim": "The sampled pattern is universal.",
        "status": "needs_falsification",
        "evidence_level": "sampled",
        "evidence": "Only a random sample was checked.",
    })
    manager = _FakeManager()

    blocked = orch._spawn_tool(manager, {"hint": "Prove it", "direction_id": "D1", "gap_id": "G1"})
    assert '"reason": "falsification_required"' in blocked.content
    allowed = orch._spawn_tool(manager, {
        "hint": "Search for a counterexample.",
        "direction_id": "D1",
        "gap_id": "G1",
        "method_id": "falsification",
    })
    assert '"spawned": true' in allowed.content


def test_stalled_direction_blocks_ordinary_worker_but_allows_consolidation(tmp_path):
    orch = _make_orch()
    orch.research_state = ResearchStateStore(tmp_path / "verified_propositions")
    orch.research_state.sync({
        "objective_summary": "Open",
        "directions": [{
            "direction_id": "D1", "title": "Stalled", "goal": "Resolve target",
            "status": "stalled", "health": "stalled",
            "gaps": [{"gap_id": "G1", "statement": "Main gap", "status": "open"}],
        }],
    })
    orch.search.sync_research_state(orch.research_state.load())
    manager = _FakeManager()

    blocked = orch._spawn_tool(manager, {"hint": "retry", "direction_id": "D1", "gap_id": "G1"})
    assert '"reason": "direction_not_schedulable"' in blocked.content
    allowed = orch._spawn_tool(manager, {
        "hint": "cash out", "direction_id": "D1", "gap_id": "G1",
        "consolidation": True, "pinned_target": "Resolve target",
    })
    assert '"spawned": true' in allowed.content


def test_spawn_without_parent_id_stays_at_root():
    orch = _make_orch()
    mgr = _FakeManager()
    orch._spawn_tool(mgr, {"hint": "first spawn"})
    assert len(orch.search.root.children) == 1
    child = orch.search.graph.get(orch.search.root.children[0])
    assert child.depth == 1


def test_explicit_parent_id_attaches_deeper_and_takes_precedence():
    orch = _make_orch()
    mgr = _FakeManager()
    seed_a = orch.search.on_spawn("wA", "A")
    seed_b = orch.search.on_spawn("wB", "B")
    # 显式指定挂到 B 之下，与 A 无关。
    orch._spawn_tool(mgr, {"hint": "x", "parent_id": seed_b.state_id})
    assert len(seed_b.children) == 1
    assert len(seed_a.children) == 0
    child = orch.search.graph.get(seed_b.children[0])
    assert child.depth == seed_b.depth + 1 == 2


def test_stale_parent_id_falls_back_to_root():
    """显式传入的 parent_id 指向的节点已不存在时，静默回退挂 root，不报错。"""
    orch = _make_orch()
    mgr = _FakeManager()
    orch._spawn_tool(mgr, {"hint": "y", "parent_id": "nonexistent-state-id"})
    root = orch.search.root
    assert len(root.children) == 1
    child = orch.search.graph.get(root.children[0])
    assert child.depth == 1
