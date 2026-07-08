"""局部 pairwise selection + 平局定案 + 配额 + 退火（§5）。

对应 `docs/orchestrator-mcts-design.md` §5，逐条落地：

- §5.1 只做**局部同父兄弟** pairwise，不做全局 Elo（从架构上消除传递性破坏，
  避免冷门正解被全局线性化淹没，note-4）。
- §5.2 critic 输出**四值**（`DuelOutcome`），显式区分"势均力敌"与"正交不可比"。
- §5.3 平局定案（本设计核心增量）：
    comparable-tie → 各 +0.5 入胜率池、标 crossover 候选；
    incomparable   → **不入池、不排序**，两支各从 explore 配额独立保底（多样性保护）。
- §5.4 visit_count 全祖先回传由 `graph.on_dispatch` 负责；这里用
  `graph.explore_bonus` 作 UCB-style 软探索偏置。
- §5.5 explore/exploit **硬配额** + progress_rate 自适应退火，替代裸 ε-greedy。
- §5.6 反复平局止损：同一对最多比 K 次，K 次仍 tie 就停比，交给 proxy + 配额。

critic 本身需 LLM，以 `PairwiseCritic` 协议表达；本模块不绑定具体实现，可单元测试。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional, Protocol, Sequence

from .graph import SearchGraph, SearchNode
from .proxy import ProxyScore


class DuelOutcome(str, Enum):
    """四值 critic 输出（§5.2）。"""

    A_WINS = "A>B"
    B_WINS = "B>A"
    COMPARABLE_TIE = "comparable-tie"  # 可比、势均力敌
    INCOMPARABLE = "incomparable"  # 正交、根本无法比较


@dataclass
class CriticContext:
    """喂给 pairwise critic 的 rubric 输入（§5.2）。

    包含公共祖先视图 + 两支各自增量（由 compaction.assemble_critic_view 组装），
    以及 proxy 分（用 open_subgoals 表达 gap，不用绝对分）。rubric 受 §3.1 防过拟合约束。
    """

    common_view_refs: list[str] = field(default_factory=list)
    a_delta_refs: list[str] = field(default_factory=list)
    b_delta_refs: list[str] = field(default_factory=list)
    a_proxy: Optional[ProxyScore] = None
    b_proxy: Optional[ProxyScore] = None
    handoff_summary: str = ""


class PairwiseCritic(Protocol):
    """同父兄弟 pairwise 裁决（§5.2，需 LLM）。单元测试可注入确定性 fake。"""

    def judge(self, a: SearchNode, b: SearchNode, context: CriticContext) -> DuelOutcome: ...


def _pair_key(a_id: str, b_id: str) -> tuple[str, str]:
    return (a_id, b_id) if a_id <= b_id else (b_id, a_id)


class DuelLedger:
    """记录每对无序兄弟被比较的次数，实现反复平局止损（§5.6）。"""

    def __init__(self, max_duels_per_pair: int = 2) -> None:
        # 建议 K=1~2（§5.6）
        self.max_duels_per_pair = max(1, int(max_duels_per_pair))
        self._counts: dict[tuple[str, str], int] = {}

    def count(self, a_id: str, b_id: str) -> int:
        return self._counts.get(_pair_key(a_id, b_id), 0)

    def record(self, a_id: str, b_id: str) -> int:
        key = _pair_key(a_id, b_id)
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]

    def exhausted(self, a_id: str, b_id: str) -> bool:
        """是否已比够 K 次——达到后停止比较，交给 proxy + 配额接管（§5.6）。"""
        return self.count(a_id, b_id) >= self.max_duels_per_pair


@dataclass(frozen=True)
class DuelEffect:
    """一次 duel 对调度的影响记录（供上层决策/日志）。"""

    outcome: DuelOutcome
    scored: bool  # 是否计入胜率池
    crossover_candidate: bool  # 是否标记为 crossover 候选（§6.4）
    diversity_protected: bool  # incomparable 两支是否转入多样性保护（§5.3）


def record_duel(
    graph: SearchGraph,
    a_id: str,
    b_id: str,
    outcome: DuelOutcome,
    *,
    ledger: Optional[DuelLedger] = None,
) -> DuelEffect:
    """按四值结果更新局部胜率池（§5.3）。胜率**只在同父兄弟池**累加。

    - A>B / B>A：胜者 +1，双方 duels+1。
    - comparable-tie：各 +0.5（**不是 +1**——+1 会系统性抬高 Q、压制新 arm），
      双方 duels+1，并标记 crossover 候选（§6.4，合并前须过 falsification）。
    - incomparable：**不改 wins、不改 duels、不回传排序**；两支转多样性保护
      （从 explore 配额各自独立保底演化，§5.3）。
    """
    a = graph.get(a_id)
    b = graph.get(b_id)
    if ledger is not None:
        ledger.record(a_id, b_id)

    if outcome == DuelOutcome.A_WINS:
        a.wins += 1.0
        a.duels += 1
        b.duels += 1
        return DuelEffect(outcome, scored=True, crossover_candidate=False, diversity_protected=False)
    if outcome == DuelOutcome.B_WINS:
        b.wins += 1.0
        a.duels += 1
        b.duels += 1
        return DuelEffect(outcome, scored=True, crossover_candidate=False, diversity_protected=False)
    if outcome == DuelOutcome.COMPARABLE_TIE:
        a.wins += 0.5
        b.wins += 0.5
        a.duels += 1
        b.duels += 1
        a.crossover_candidate = True
        b.crossover_candidate = True
        return DuelEffect(outcome, scored=True, crossover_candidate=True, diversity_protected=False)
    # INCOMPARABLE：不入池、不排序，转多样性保护
    return DuelEffect(outcome, scored=False, crossover_candidate=False, diversity_protected=True)


NoveltyFn = Callable[[str], float]


def tie_break(
    graph: SearchGraph,
    a_id: str,
    b_id: str,
    *,
    proxy: dict[str, ProxyScore],
    novelty: Optional[NoveltyFn] = None,
) -> str:
    """comparable-tie 的 tie-break 优先级（§5.3）：

    ① proxy 分（open_subgoals 少 / verified_delta 大者优先）→
    ② visit_count 少者优先（等价 UCB 探索项，偏向欠探索支）→
    ③ novelty 高者优先。

    返回应优先 exploit 的 state_id。
    """
    pa, pb = proxy.get(a_id), proxy.get(b_id)
    if pa is not None and pb is not None and pa.ranking_key() != pb.ranking_key():
        return a_id if pa.ranking_key() < pb.ranking_key() else b_id
    va, vb = graph.get(a_id).visit_count, graph.get(b_id).visit_count
    if va != vb:
        return a_id if va < vb else b_id
    if novelty is not None:
        na, nb = novelty(a_id), novelty(b_id)
        if na != nb:
            return a_id if na > nb else b_id
    return a_id


# ---- explore/exploit 配额 + 退火（§5.5） ------------------------------


@dataclass(frozen=True)
class Quota:
    """一个 cycle 的 dispatch 配额（§5.5）。硬约束，非 LLM 建议；无合格候选少派不凑数。"""

    exploit: int
    sibling_explore: int
    depth1_explore: int
    fill: int

    @property
    def total(self) -> int:
        return self.exploit + self.sibling_explore + self.depth1_explore + self.fill


def anneal_exploit_fraction(
    rate: float,
    *,
    min_fraction: float = 0.3,
    max_fraction: float = 0.8,
    scale: float = 1.0,
) -> float:
    """progress_rate 驱动的自适应 exploit 占比（§5.5，替代固定 ε/τ）。

    进展快（rate 高）→ exploit 占比高（早收敛）；进展慢 → 压低 exploit、多留 explore。
    用有界饱和映射把 [0, +inf) 的 rate 压到 [min_fraction, max_fraction]。
    """
    r = max(0.0, rate) * max(1e-9, scale)
    saturated = r / (1.0 + r)  # ∈ [0, 1)
    return min_fraction + (max_fraction - min_fraction) * saturated


def allocate_quota(
    budget: int,
    *,
    progress_rate: float,
    has_siblings: bool,
    cycle: int,
    depth1_threshold: int = 3,
    min_exploit_fraction: float = 0.3,
    max_exploit_fraction: float = 0.8,
) -> Quota:
    """把本 cycle 的 worker 预算切成硬配额（§5.5）。

    - Exploit：总是有（至少 1，只要有预算），占比由退火决定。
    - Sibling-explore：exploit 节点有兄弟时才分（含 incomparable 支的多样性保护）。
    - Depth-1 explore（新 arm）：`cycle >= depth1_threshold` 才强制开新 depth-1 hint，
      替代裸 ε-greedy。
    - Fill：剩余额度，按 score + Jaccard 多样性填（见 `select_diverse_fill`）。
    """
    budget = max(0, int(budget))
    if budget == 0:
        return Quota(0, 0, 0, 0)

    exploit_fraction = anneal_exploit_fraction(
        progress_rate, min_fraction=min_exploit_fraction, max_fraction=max_exploit_fraction
    )
    exploit = max(1, round(budget * exploit_fraction))
    exploit = min(exploit, budget)
    remaining = budget - exploit

    sibling_explore = 0
    if has_siblings and remaining > 0:
        sibling_explore = max(1, remaining // 2)
        sibling_explore = min(sibling_explore, remaining)
        remaining -= sibling_explore

    depth1_explore = 0
    if cycle >= depth1_threshold and remaining > 0:
        depth1_explore = 1
        remaining -= 1

    fill = max(0, remaining)
    return Quota(exploit=exploit, sibling_explore=sibling_explore, depth1_explore=depth1_explore, fill=fill)


# ---- fill 的多样性选择（§5.5 E：score + Jaccard 多样性） ---------------


def jaccard(a: Sequence[str], b: Sequence[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    union = sa | sb
    return len(sa & sb) / len(union) if union else 0.0


def select_diverse_fill(
    graph: SearchGraph,
    candidates: Sequence[str],
    *,
    proxy: dict[str, ProxyScore],
    k: int,
    max_similarity: float = 0.8,
) -> list[str]:
    """贪心地按 proxy 优先 + 与已选集合的 Jaccard 多样性挑 k 个 fill 节点（§5.5 E）。

    先按 proxy 优先级排序，再逐个加入：若与已选中任一节点的 materialize_view 相似度
    超过 `max_similarity` 则跳过（保多样，避免算力堆在近乎重复的方向）。候选不足时
    **少派不凑数**（§5.5）。
    """
    if k <= 0:
        return []
    ordered = sorted(
        candidates,
        key=lambda sid: proxy[sid].ranking_key() if sid in proxy else (1 << 30, 0, 0),
    )
    picked: list[str] = []
    picked_views: list[list[str]] = []
    for sid in ordered:
        if len(picked) >= k:
            break
        view = graph.materialize_view(sid)
        if any(jaccard(view, pv) > max_similarity for pv in picked_views):
            continue
        picked.append(sid)
        picked_views.append(view)
    return picked
