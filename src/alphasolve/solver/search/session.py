from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional

from .graph import Delta, NodeStatus, SearchGraph, SearchNode
from .scheduler import SchedulerStateStore
from .insight import FalsifiedImplication, InsightRecord, apply_falsification
from .proxy import ProxyScore, compute_proxy


# 认定为"已解决/已验证成果"的 worker 状态（用于最小 insight 合成）。
_VERIFIED_STATUSES = {"verified"}
# 认定为 informative failure 的 worker 状态（记 open_subgoal，但**不剪枝**，§6.3）。
_FAILURE_STATUSES = {"failed", "error", "unverified", "rejected"}


class SearchSession:
    """orchestrator 生命周期内存活的调度层状态（§2/§3/§6 的编排门面）。

    调度决策——exploit 哪个方向、要不要自由探索、选什么 method——完全交给
    orchestrator 的 LLM 自主判断（见 `prompts/orchestrator.md`）。本模块不做任何
    自动选择/打分/排序，只负责：

    - 维护 attempt DAG（worker 生命周期 → `SearchNode`），供崩溃恢复；
    - 按 §3 廉价 proxy 信号给出只读的 frontier 排序，作为**参考事实**提供给 LLM；
    - 记录 pending `ResearchImpact` 待评估项；
    - 落盘 attempt 事件供进程重启后恢复。

    线程模型：orchestrator 主 agent 是单线程串行调用工具（SpawnWorker / TaskOutput /
    Agent），本对象的方法都在该线程内被调用，因此内部不加锁。它不被 worker 线程访问。
    """

    def __init__(
        self,
        *,
        problem_hypothesis: str = "resolve problem.md",
        scheduler_state_path: Path | None = None,
        attempt_graph_path: Path | None = None,
    ) -> None:
        self.graph = SearchGraph()
        self.scheduler = SchedulerStateStore(scheduler_state_path, attempt_graph_path)
        self.cycle = self.scheduler.cycle
        # root = 问题本身；所有 worker 节点先挂在它下面。
        self.root: SearchNode = self.graph.add_node(problem_hypothesis)
        self.root.status = NodeStatus.RUNNING
        self._worker_to_state: dict[str, str] = {}
        # 上一 cycle 的 insight 快照，用于 proxy 的 verified_delta 一阶差分。
        self._prev_insight: dict[str, InsightRecord] = {}
        self._solved: bool = False
        self._restore_attempt_graph()

    def sync_research_state(self, state: dict[str, Any]) -> None:
        """兼容既有调用点的空操作。

        direction/gap 的生命周期完全由 `ResearchStateStore`（state.md/research_state.json）
        管理；调度层不再维护独立的 arm 副本，故这里无需做任何事。保留此方法签名只是为了
        不必改动 orchestrator.py 里现有的调用点。
        """
        return None

    def _restore_attempt_graph(self) -> None:
        """从 append-only 事件恢复已知 attempt；未完成旧 attempt 视为 cooled。"""
        events = self.scheduler.load_attempt_events()
        for event in events:
            event_type = str(event.get("event") or "")
            attempt_id = str(event.get("attempt_id") or "")
            if not attempt_id:
                continue
            if event_type == "spawn" and attempt_id not in self.graph:
                parents = [
                    str(item) for item in event.get("parent_attempt_ids", [])
                    if str(item) in self.graph
                ] or [self.root.state_id]
                node = self.graph.add_node(
                    str(event.get("hint") or "restored attempt"),
                    parents=parents,
                    state_id=attempt_id,
                )
                node.status = NodeStatus.COOLED
                node.route_id = str(event.get("route_id") or "").strip() or None
                node.direction_id = str(event.get("direction_id") or "").strip() or None
                node.gap_id = str(event.get("gap_id") or "").strip() or None
                node.method_id = str(event.get("method_id") or "").strip() or None
                node.worker_id = str(event.get("worker_id") or "").strip() or None
                if node.worker_id:
                    self._worker_to_state[node.worker_id] = node.state_id
            elif event_type in {"result", "impact"} and attempt_id in self.graph:
                node = self.graph.get(attempt_id)
                node.status = NodeStatus.DONE
                if event.get("summary"):
                    node.result = str(event["summary"])
                if event.get("verified_file"):
                    node.delta = Delta(proof_ref=str(event["verified_file"]))

    # ---- worker 生命周期映射 ------------------------------------------

    def on_spawn(
        self,
        worker_id: str,
        hint: Optional[str],
        *,
        parent_id: Optional[str] = None,
        parent_ids: Optional[Iterable[str]] = None,
        route_id: Optional[str] = None,
        direction_id: Optional[str] = None,
        gap_id: Optional[str] = None,
        method_id: Optional[str] = None,
    ) -> SearchNode:
        """建立一次 attempt 记录（纯记账，不触发任何自动调度决策）。"""
        if worker_id in self._worker_to_state:
            return self.graph.get(self._worker_to_state[worker_id])
        raw_parents = list(parent_ids or ([] if parent_id is None else [parent_id]))
        parents = [str(item) for item in raw_parents if str(item) in self.graph]
        if not parents:
            parents = [self.root.state_id]
        hypothesis = (hint or "free exploration").strip() or "free exploration"
        node = self.graph.add_node(hypothesis, parents=parents)
        node.status = NodeStatus.RUNNING
        node.worker_id = worker_id
        node.route_id = (route_id or "").strip() or None
        node.direction_id = (direction_id or "").strip() or None
        node.gap_id = (gap_id or "").strip() or None
        node.method_id = (method_id or "direct_proof").strip() or "direct_proof"
        self._worker_to_state[worker_id] = node.state_id
        # append-only attempt 事件；崩溃时至少保留 lineage 事实供重启恢复。
        self.scheduler.append_attempt_event({
            "event": "spawn",
            "attempt_id": node.state_id,
            "worker_id": worker_id,
            "route_id": node.route_id,
            "direction_id": node.direction_id,
            "gap_id": node.gap_id,
            "method_id": node.method_id,
            "parent_attempt_ids": list(node.parents),
            "hint": hypothesis,
        })
        return node

    def on_worker_result(self, payload: dict[str, Any]) -> Optional[SearchNode]:
        """回填 raw result；未评估的 verified 结果不会自动获得正向 reward。"""
        worker_id = str(payload.get("worker_id") or "")
        sid = self._worker_to_state.get(worker_id)
        if sid is None:
            return None
        node = self.graph.get(sid)
        status = str(payload.get("status") or "").lower()
        summary = str(payload.get("summary") or "").strip()
        node.result = summary
        node.direction_id = str(payload.get("direction_id") or node.direction_id or "").strip() or None
        node.gap_id = str(payload.get("gap_id") or node.gap_id or "").strip() or None

        ref = payload.get("verified_file") or payload.get("proposition_file")
        if ref:
            node.delta = Delta(proof_ref=str(ref))

        if status in _VERIFIED_STATUSES and not bool(payload.get("solved_problem")):
            if payload.get("auto_assessed"):
                # consolidation worker 已被代码自动评估（wall/blocked_gap），不进 pending
                node.insight = InsightRecord(
                    open_subgoals=[node.gap_id] if node.gap_id else [],
                    direction_summary=f"Auto-assessed: {payload.get('consolidation_outcome', 'wall')}",
                )
                node.impact_status = "assessed"
            else:
                node.insight = InsightRecord(
                    open_subgoals=[node.gap_id] if node.gap_id else [],
                    direction_summary="Awaiting orchestrator ResearchImpact assessment.",
                )
                node.impact_status = "pending"
        else:
            node.insight = self._synthesize_unassessed_insight(status, summary)
            node.impact_status = "assessed" if bool(payload.get("solved_problem")) else "not_required"
        node.status = NodeStatus.DONE
        failure_kind = str(payload.get("failure_kind") or "").strip() or None
        self.scheduler.append_attempt_event({
            "event": "result",
            "attempt_id": node.state_id,
            "worker_id": worker_id,
            "status": status,
            "failure_kind": failure_kind,
            "summary": summary[:2000],
            "verified_file": payload.get("verified_file"),
        })

        if bool(payload.get("solved_problem")):
            self._solved = True
        return node

    def _synthesize_unassessed_insight(self, status: str, summary: str) -> InsightRecord:
        if status in _VERIFIED_STATUSES:
            return InsightRecord(verified_implications=[summary or "verified proposition"])
        if status in _FAILURE_STATUSES:
            return InsightRecord(open_subgoals=[summary or "attempt did not verify"])
        return InsightRecord(direction_summary=summary)

    def pending_impact_worker_ids(self) -> list[str]:
        return sorted(
            worker_id
            for worker_id, sid in self._worker_to_state.items()
            if self.graph.get(sid).impact_status == "pending"
        )

    def research_target(self, worker_id: str) -> dict[str, Any] | None:
        sid = self._worker_to_state.get(worker_id)
        if sid is None:
            return None
        node = self.graph.get(sid)
        return {
            "worker_id": worker_id,
            "state_id": sid,
            "route_id": node.route_id,
            "direction_id": node.direction_id,
            "gap_id": node.gap_id,
            "method_id": node.method_id,
            "hint": node.hypothesis,
            "impact_status": node.impact_status,
        }

    def record_research_impact(self, worker_id: str, impact: dict[str, Any]) -> SearchNode:
        sid = self._worker_to_state.get(worker_id)
        if sid is None:
            raise ValueError(f"unknown worker_id: {worker_id}")
        node = self.graph.get(sid)
        if node.impact_status != "pending":
            raise ValueError(f"worker {worker_id} has no pending ResearchImpact assessment")

        relation = str(impact.get("relation_to_target") or "")
        summary = str(impact.get("summary") or "").strip()
        remaining = [str(item).strip() for item in impact.get("remaining_gaps", []) if str(item).strip()]
        positive = relation in {"solves_target", "sufficient_for_target", "direct_advance"}
        falsified: list[FalsifiedImplication] = []
        if relation == "refutes_target":
            falsified.append(FalsifiedImplication(node.hypothesis, summary))
        previous = node.insight
        if previous is not None:
            self._prev_insight[sid] = previous
        node.insight = InsightRecord(
            verified_implications=[summary] if positive else [],
            falsified_implications=falsified,
            open_subgoals=remaining,
            direction_summary=summary,
        )
        node.impact_status = "assessed"
        node.impact_relation = relation
        if relation == "refutes_target":
            node.status = NodeStatus.PRUNED
            node.prune_reason = summary or "research target refuted"
        self.scheduler.append_attempt_event({
            "event": "impact",
            "attempt_id": node.state_id,
            "worker_id": worker_id,
            "relation_to_target": relation,
            "summary": summary[:2000],
        })
        return node

    # ---- proxy 排序（§3，独立于调度决策，只是只读参考信号） -------------

    def proxy_of(self, state_id: str) -> ProxyScore:
        return compute_proxy(self.graph.get(state_id), prev_insight=self._prev_insight.get(state_id))

    def active_worker_nodes(self) -> list[SearchNode]:
        """已完成但仍"活跃"（可作为下一步 exploit/explore 起点）的 worker 节点。

        排除 root、排除 pruned、排除仍在 RUNNING 的（结果未回填、proxy 无意义）。
        """
        out: list[SearchNode] = []
        for node in self.graph.nodes():
            if node.state_id == self.root.state_id:
                continue
            if node.status in (NodeStatus.PRUNED, NodeStatus.MERGED, NodeStatus.RUNNING, NodeStatus.PENDING):
                continue
            if node.impact_status == "pending":
                continue
            out.append(node)
        return out

    def frontier_ranking(self) -> list[dict[str, Any]]:
        """把活跃 worker 节点按 §3 proxy 优先级排序为可读清单。

        这是**只读事实**，供 orchestrator 的 LLM 参考；本模块不据此做任何自动选择。
        """
        scored = [(n.state_id, self.proxy_of(n.state_id)) for n in self.active_worker_nodes()]
        scored.sort(key=lambda item: item[1].ranking_key())
        ranking: list[dict[str, Any]] = []
        for sid, score in scored:
            node = self.graph.get(sid)
            ranking.append({
                "state_id": sid,
                "hypothesis": node.hypothesis,
                "route_id": node.route_id,
                "direction_id": node.direction_id,
                "gap_id": node.gap_id,
                "impact_status": node.impact_status,
                "impact_relation": node.impact_relation,
                "open_subgoals": score.open_subgoals,
                "verified_implications": score.verified_implications,
                "verified_delta": score.verified_delta,
                "falsification_hits": score.falsification_hits,
            })
        return ranking

    # ---- falsification 转发（§6.3，需显式的 ¬B 证据才可调用） ----------

    def falsify(self, refuted_ansatz: str, *, reason: Optional[str] = None) -> list[SearchNode]:
        """硬砍所有依赖被证伪 ansatz 的活跃节点（§6.3）。调用方须确有 ¬B 的证明。"""
        return apply_falsification(self.graph, refuted_ansatz, reason=reason)

    # ---- 打包给 orchestrator 的只读事实 --------------------------------

    def advise(self) -> dict[str, Any]:
        """打包一份纯客观事实的 payload，并推进一个 selection cycle。

        不做任何排序/打分/自动选择——exploit 哪个方向、要不要自由探索，完全交给
        orchestrator 的 LLM 根据 `frontier` 和 `pending_research_impacts` 自行判断
        （见 `prompts/orchestrator.md`）。
        """
        advice = {
            "cycle": self.cycle,
            "pending_research_impacts": self.pending_impact_worker_ids(),
            "frontier": self.frontier_ranking(),
            "solved": self._solved,
        }
        self.cycle += 1
        self.scheduler.cycle = self.cycle
        self.scheduler.save()
        return advice
