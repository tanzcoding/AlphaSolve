"""调度层会话：把 orchestrator 的 worker 生命周期映射进 SearchGraph（§2/§3/§5）。

这是 `search` 子包的**编排门面**（facade）。它把彼此独立的纯算法模块（graph /
proxy / selection / insight / compaction）串成一个可被 orchestrator 持有的、跨 cycle
存活的调度状态对象，并只暴露一组高层、只读友好的 API：

- `on_spawn(worker_id, hint)`：一次 SpawnWorker → 建一个 depth-1 节点、记 dispatch。
- `on_worker_result(payload)`：一次 worker 完成 → 按 worker_id 回填 status / delta /
  最小 insight，并刷新 proxy 快照。
- `frontier_ranking()` / `quota()` / `advice()`：把 §3 proxy 排序、§5.5 配额、可比兄弟
  对候选打包成建议，供 orchestrator 参考（**建议而非强制**）。
- `record_sibling_duel()`：把 critic 的四值结果按 §5.3 计入局部胜率池。

**严格的边界声明（§8）**：
- 本模块是**纯调度层状态**，不 import agent/llm 层、不触碰 `WorkerManager` 调度语义、
  不改任何 role 的 prompt。它对 worker 的真实执行**只读**——只观察 SpawnWorker /
  TaskOutput 已经返回的 payload，不改变其行为。
- **已知结构性限制（诚实标注）**：当前 orchestrator 由 LLM 自主用 `hint` 驱动 spawn，
  既不提供节点父子关系，worker 也不产出结构化 `InsightRecord`。因此：
  * 所有 worker 节点先作为 root 的 **depth-1 兄弟**登记（它们确实同父，pairwise 语义成立）；
    真正的多层 parent（crossover / 深链）需要 orchestrator 显式传 parent，属后续（§9 P2）。
  * `insight` 由 worker 结果 payload **最小合成**（verified → 记一条 verified_implication；
    informative failure → 记一条 open_subgoal，且**不剪枝**，遵循 §6.3）。要得到 §6.2 的富
    insight（verified/falsified/open_subgoals 三分）需要 worker/curator 侧协作产出。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .graph import Delta, NodeStatus, SearchGraph, SearchNode
from .insight import InsightRecord, apply_falsification
from .proxy import ProgressSample, ProxyScore, compute_proxy, progress_rate
from .selection import (
    DuelEffect,
    DuelLedger,
    DuelOutcome,
    Quota,
    allocate_quota,
    record_duel,
)


# 认定为"已解决/已验证成果"的 worker 状态（用于最小 insight 合成）。
_VERIFIED_STATUSES = {"verified"}
# 认定为 informative failure 的 worker 状态（记 open_subgoal，但**不剪枝**，§6.3）。
_FAILURE_STATUSES = {"failed", "error", "unverified", "rejected"}


@dataclass
class _WindowAccumulator:
    """当前 selection cycle 的进展窗口累计（喂给 §5.5 退火）。"""

    verified_delta: int = 0
    dispatches: int = 0

    def reset(self) -> None:
        self.verified_delta = 0
        self.dispatches = 0

    def to_sample(self) -> ProgressSample:
        return ProgressSample(verified_delta=self.verified_delta, dispatches=self.dispatches)


class SearchSession:
    """orchestrator 生命周期内存活的调度层状态（§2/§3/§5 的编排门面）。

    线程模型：orchestrator 主 agent 是单线程串行调用工具（SpawnWorker / TaskOutput /
    Agent），本对象的方法都在该线程内被调用，因此内部不加锁。它不被 worker 线程访问。
    """

    def __init__(
        self,
        *,
        problem_hypothesis: str = "resolve problem.md",
        max_duels_per_pair: int = 2,
        progress_window: int = 5,
    ) -> None:
        self.graph = SearchGraph()
        self.ledger = DuelLedger(max_duels_per_pair=max_duels_per_pair)
        self.cycle = 0
        self._progress_window = max(1, int(progress_window))
        self._samples: list[ProgressSample] = []
        self._window = _WindowAccumulator()
        # root = 问题本身；所有 worker 节点先挂在它下面（depth-1 兄弟池）。
        self.root: SearchNode = self.graph.add_node(problem_hypothesis)
        self.root.status = NodeStatus.RUNNING
        self._worker_to_state: dict[str, str] = {}
        # 上一 cycle 的 insight 快照，用于 proxy 的 verified_delta 一阶差分。
        self._prev_insight: dict[str, InsightRecord] = {}
        self._solved: bool = False

    # ---- worker 生命周期映射 ------------------------------------------

    def on_spawn(self, worker_id: str, hint: Optional[str], *, parent_id: Optional[str] = None) -> SearchNode:
        """一次成功的 SpawnWorker：建 depth-1 节点、记录 dispatch（§5.4 全祖先回传）。

        `parent_id` 预留给未来显式多层 parent；缺省挂 root。重复 worker_id 直接复用。
        """
        if worker_id in self._worker_to_state:
            return self.graph.get(self._worker_to_state[worker_id])
        parent = parent_id or self.root.state_id
        hypothesis = (hint or "free exploration").strip() or "free exploration"
        node = self.graph.add_node(hypothesis, parents=[parent])
        node.status = NodeStatus.RUNNING
        self._worker_to_state[worker_id] = node.state_id
        self.graph.on_dispatch(node.state_id)
        self._window.dispatches += 1
        return node

    def on_worker_result(self, payload: dict[str, Any]) -> Optional[SearchNode]:
        """一次 worker 完成：按 worker_id 回填节点 status / delta / 最小 insight。

        `payload` 是 WorkerManager 交回的单个 completed 条目（worker_id / status /
        summary / proposition_file / verified_file / solved_problem 等）。找不到对应
        节点时返回 None（例如 free-exploration worker 未经 on_spawn 登记）。
        """
        worker_id = str(payload.get("worker_id") or "")
        sid = self._worker_to_state.get(worker_id)
        if sid is None:
            return None
        node = self.graph.get(sid)
        status = str(payload.get("status") or "").lower()
        summary = str(payload.get("summary") or "").strip()

        # delta：只存对权威 pool 的**引用**（不复制正文，§2.1）。
        ref = payload.get("verified_file") or payload.get("proposition_file")
        if ref:
            node.delta = Delta(proof_ref=str(ref))

        prev = node.insight
        if prev is not None:
            self._prev_insight[sid] = prev

        node.insight = self._synthesize_insight(status, summary)
        node.status = NodeStatus.DONE

        if node.insight is not None:
            self._window.verified_delta += self._verified_gain(sid, node.insight)
        if bool(payload.get("solved_problem")):
            self._solved = True
        return node

    def _synthesize_insight(self, status: str, summary: str) -> InsightRecord:
        """从 worker 结果 payload 最小合成 InsightRecord（见类 docstring 的限制说明）。"""
        if status in _VERIFIED_STATUSES:
            return InsightRecord(verified_implications=[summary or "verified proposition"])
        if status in _FAILURE_STATUSES:
            # informative failure：记为未决子目标，**不**触发 falsification 硬砍（§6.3）。
            return InsightRecord(open_subgoals=[summary or "attempt did not verify"])
        return InsightRecord(direction_summary=summary)

    def _verified_gain(self, sid: str, current: InsightRecord) -> int:
        prev = self._prev_insight.get(sid)
        before = len(prev.verified_implications) if prev is not None else 0
        return max(0, len(current.verified_implications) - before)

    # ---- proxy 排序（§3） ---------------------------------------------

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
            out.append(node)
        return out

    def frontier_ranking(self) -> list[dict[str, Any]]:
        """把活跃 worker 节点按 §3 proxy 优先级（exploit 优先）排序为可读清单。"""
        scored = [(n.state_id, self.proxy_of(n.state_id)) for n in self.active_worker_nodes()]
        scored.sort(key=lambda item: item[1].ranking_key())
        ranking: list[dict[str, Any]] = []
        for sid, score in scored:
            node = self.graph.get(sid)
            ranking.append({
                "state_id": sid,
                "hypothesis": node.hypothesis,
                "open_subgoals": score.open_subgoals,
                "verified_implications": score.verified_implications,
                "verified_delta": score.verified_delta,
                "falsification_hits": score.falsification_hits,
                "visit_count": node.visit_count,
                "q_value": round(node.q_value, 3),
                "crossover_candidate": node.crossover_candidate,
            })
        return ranking

    # ---- 兄弟 pairwise 候选 + duel 记账（§5.3/§5.6） ------------------

    def sibling_duel_candidates(self) -> list[tuple[str, str]]:
        """列出值得送去 critic 的同父兄弟对：都活跃、且该对未比够 K 次（§5.6 止损）。"""
        active = self.active_worker_nodes()
        pairs: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for node in active:
            for sib in self.graph.siblings(node.state_id):
                if sib.status in (NodeStatus.PRUNED, NodeStatus.MERGED, NodeStatus.RUNNING, NodeStatus.PENDING):
                    continue
                a, b = sorted((node.state_id, sib.state_id))
                key = (a, b)
                if key in seen or self.ledger.exhausted(a, b):
                    continue
                seen.add(key)
                pairs.append((a, b))
        return pairs

    def record_sibling_duel(self, a_id: str, b_id: str, outcome: DuelOutcome) -> DuelEffect:
        """把 critic 的四值判决按 §5.3 计入局部胜率池，并计入 §5.6 止损账本。"""
        return record_duel(self.graph, a_id, b_id, outcome, ledger=self.ledger)

    # ---- 配额 + 退火（§5.5） ------------------------------------------

    def quota(self, budget: int) -> Quota:
        rate = progress_rate(self._samples[-self._progress_window:]) if self._samples else 0.0
        has_siblings = len(self.active_worker_nodes()) >= 2
        return allocate_quota(
            budget,
            progress_rate=rate,
            has_siblings=has_siblings,
            cycle=self.cycle,
        )

    def current_progress_rate(self) -> float:
        return progress_rate(self._samples[-self._progress_window:]) if self._samples else 0.0

    # ---- falsification 转发（§6.3，需显式的 ¬B 证据才可调用） ----------

    def falsify(self, refuted_ansatz: str, *, reason: Optional[str] = None) -> list[SearchNode]:
        """硬砍所有依赖被证伪 ansatz 的活跃节点（§6.3）。调用方须确有 ¬B 的证明。"""
        return apply_falsification(self.graph, refuted_ansatz, reason=reason)

    # ---- 打包给 orchestrator 的建议 payload ---------------------------

    def advise(self, *, available_worker_slots: int) -> dict[str, Any]:
        """打包一份 selection 建议，并推进一个 selection cycle（§3+§5，建议而非强制）。

        返回结构给 orchestrator 作为 TaskOutput 的**只读附加信息**。调用它同时：
        归档当前进展窗口为一个 ProgressSample、cycle += 1、重置窗口累计。
        """
        budget = max(0, int(available_worker_slots))
        q = self.quota(budget)
        advice = {
            "note": (
                "Advisory only (MCTS-style dueling-bandit selection, design §3/§5). "
                "theorem_checker remains the sole termination signal."
            ),
            "cycle": self.cycle,
            "progress_rate": round(self.current_progress_rate(), 4),
            "frontier_by_proxy": self.frontier_ranking(),
            "sibling_duel_candidates": [
                {"a": a, "b": b, "duels_so_far": self.ledger.count(a, b)}
                for a, b in self.sibling_duel_candidates()
            ],
            "quota": {
                "budget": budget,
                "exploit": q.exploit,
                "sibling_explore": q.sibling_explore,
                "depth1_explore": q.depth1_explore,
                "fill": q.fill,
            },
            "solved": self._solved,
        }
        # 归档并推进 cycle。
        self._samples.append(self._window.to_sample())
        self._window.reset()
        self.cycle += 1
        return advice
