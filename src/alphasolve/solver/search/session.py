from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional

from .graph import Delta, NodeStatus, SearchGraph, SearchNode
from .insight import InsightRecord
from .proxy import ProxyScore, compute_proxy
from .scheduler import SchedulerStateStore


class SearchSession:
    """Append-only worker-attempt observability graph.

    It records attempt lineage only. Mathematical task state, recursive structure,
    and dispatchability belong exclusively to the curator-owned difficulty DAG.
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
        self.root = self.graph.add_node(problem_hypothesis)
        self.root.status = NodeStatus.RUNNING
        self._worker_to_state: dict[str, str] = {}
        self._prev_insight: dict[str, InsightRecord] = {}
        self._solved = False
        self._restore_attempt_graph()

    def _restore_attempt_graph(self) -> None:
        for event in self.scheduler.load_attempt_events():
            attempt_id = str(event.get("attempt_id") or "")
            if not attempt_id:
                continue
            if event.get("event") == "spawn" and attempt_id not in self.graph:
                parents = [str(item) for item in event.get("parent_attempt_ids") or [] if str(item) in self.graph] or [self.root.state_id]
                node = self.graph.add_node(str(event.get("hint") or "restored attempt"), parents=parents, state_id=attempt_id)
                node.status = NodeStatus.COOLED
                node.worker_id = str(event.get("worker_id") or "") or None
                node.difficulty_id = str(event.get("difficulty_id") or "") or None
                node.method_id = str(event.get("method_id") or "") or None
                if node.worker_id:
                    self._worker_to_state[node.worker_id] = node.state_id
            elif event.get("event") == "result" and attempt_id in self.graph:
                node = self.graph.get(attempt_id)
                node.status = NodeStatus.DONE
                node.result = str(event.get("summary") or "") or None
                if event.get("verified_file"):
                    node.delta = Delta(proof_ref=str(event["verified_file"]))

    def on_spawn(
        self,
        worker_id: str,
        hint: Optional[str],
        *,
        parent_id: Optional[str] = None,
        parent_ids: Optional[Iterable[str]] = None,
        difficulty_id: Optional[str] = None,
        method_id: Optional[str] = None,
    ) -> SearchNode:
        if worker_id in self._worker_to_state:
            return self.graph.get(self._worker_to_state[worker_id])
        raw_parents = list(parent_ids or ([] if parent_id is None else [parent_id]))
        parents = [str(item) for item in raw_parents if str(item) in self.graph] or [self.root.state_id]
        node = self.graph.add_node((hint or "initial exploration").strip() or "initial exploration", parents=parents)
        node.status = NodeStatus.RUNNING
        node.worker_id = worker_id
        node.difficulty_id = (difficulty_id or "").strip() or None
        node.method_id = (method_id or "direct_proof").strip() or "direct_proof"
        self._worker_to_state[worker_id] = node.state_id
        self.scheduler.append_attempt_event({
            "event": "spawn",
            "attempt_id": node.state_id,
            "worker_id": worker_id,
            "difficulty_id": node.difficulty_id,
            "method_id": node.method_id,
            "parent_attempt_ids": list(node.parents),
            "hint": node.hypothesis,
        })
        return node

    def on_worker_result(self, payload: dict[str, Any]) -> Optional[SearchNode]:
        worker_id = str(payload.get("worker_id") or "")
        state_id = self._worker_to_state.get(worker_id)
        if state_id is None:
            return None
        node = self.graph.get(state_id)
        node.status = NodeStatus.DONE
        node.result = str(payload.get("summary") or "").strip() or None
        node.difficulty_id = str(payload.get("difficulty_id") or node.difficulty_id or "").strip() or None
        if payload.get("verified_file") or payload.get("proposition_file"):
            node.delta = Delta(proof_ref=str(payload.get("verified_file") or payload.get("proposition_file")))
        node.insight = InsightRecord(
            verified_implications=[node.result] if str(payload.get("status") or "").lower() == "verified" and node.result else [],
            open_subgoals=[node.difficulty_id] if node.difficulty_id else [],
            direction_summary=node.result or "",
        )
        node.impact_status = "not_required"
        self.scheduler.append_attempt_event({
            "event": "result",
            "attempt_id": node.state_id,
            "worker_id": worker_id,
            "status": str(payload.get("status") or ""),
            "summary": (node.result or "")[:2000],
            "verified_file": payload.get("verified_file"),
        })
        self._solved = bool(payload.get("solved_problem")) or self._solved
        return node

    def proxy_of(self, state_id: str) -> ProxyScore:
        return compute_proxy(self.graph.get(state_id), prev_insight=self._prev_insight.get(state_id))

    def active_worker_nodes(self) -> list[SearchNode]:
        return [
            node for node in self.graph.nodes()
            if node.state_id != self.root.state_id and node.status not in {NodeStatus.PRUNED, NodeStatus.MERGED, NodeStatus.RUNNING, NodeStatus.PENDING}
        ]

    def frontier_ranking(self) -> list[dict[str, Any]]:
        nodes = sorted(self.active_worker_nodes(), key=lambda node: self.proxy_of(node.state_id).ranking_key())
        return [{
            "state_id": node.state_id,
            "hypothesis": node.hypothesis,
            "difficulty_id": node.difficulty_id,
            "method_id": node.method_id,
            "verified_delta": self.proxy_of(node.state_id).verified_delta,
        } for node in nodes]

    def advise(self) -> dict[str, Any]:
        payload = {"cycle": self.cycle, "frontier": self.frontier_ranking(), "solved": self._solved}
        self.cycle += 1
        self.scheduler.cycle = self.cycle
        self.scheduler.save()
        return payload
