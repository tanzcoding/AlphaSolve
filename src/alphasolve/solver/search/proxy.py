"""廉价分级评估信号（§3，本期根因）。

对应 `docs/orchestrator-mcts-design.md` §3：

AlphaSolve 只有 `theorem_checker` 的二元稀疏信号（解了/没解），缺 Arbor 赖以运转的
"便宜、连续、分级"的过程评估器。本模块用三个可得量组合出一个**廉价 proxy 分**，
**只用于相对比较，绝不落绝对阈值**：

| 分量 | 含义 | 方向 |
|---|---|---|
| open_subgoals 剩余计数 | 还剩几个明确未证子目标 | 越少越接近目标 |
| verified_implications 净增量 | 本 cycle 新验证几条推导（类 dev score 一阶差分） | 越大进展越快 |
| falsification 命中次数 | 子目标被证伪的负信号 | 越多越该退出 |

§3.1 防过拟合硬约束：proxy 分**纯按结构化条目计数**，不解析、不硬编码任何领域数学
名词（upper bound / group / estimate 等），只可能涉及连接逻辑词。因此本模块不做任何
自然语言关键词匹配。

`theorem_checker` 仍是**唯一终止判据**；proxy 分只喂给 ① 局部 pairwise 排 exploit
优先级、② exhaustion 判定、③ 退火的 progress_rate。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .graph import SearchNode
from .insight import InsightRecord


@dataclass(frozen=True)
class ProxyScore:
    """某节点某 cycle 的廉价代理分（§3.1）。只做相对比较，不做绝对阈值。"""

    open_subgoals: int
    verified_implications: int
    verified_delta: int  # 本 cycle 相对上 cycle 的 verified_implications 净增量
    falsification_hits: int

    def ranking_key(self) -> tuple[int, int, int]:
        """供 `sorted(..., key=...)` 升序使用：**越靠前越优先 exploit**。

        方向统一到升序：open_subgoals 少者优先；verified_delta 大者优先（取负）；
        falsification 命中少者优先。刻意返回序数元组而非加权标量——避免把不可公度的
        三个量硬凑成一个"绝对分"（§3 定案）。
        """
        return (self.open_subgoals, -self.verified_delta, self.falsification_hits)


def compute_proxy(
    node: SearchNode,
    *,
    prev_insight: Optional[InsightRecord] = None,
) -> ProxyScore:
    """计算节点当前 cycle 的 proxy 分（§3.1）。

    `prev_insight` 是该节点上一 cycle 的 insight 快照，用来算 verified_implications
    的净增量（一阶差分，类 dev score）。首次计算时 prev 为 None，净增量取全量。
    """
    ins = node.insight
    if ins is None:
        return ProxyScore(open_subgoals=0, verified_implications=0, verified_delta=0, falsification_hits=0)
    verified_now = len(ins.verified_implications)
    verified_before = len(prev_insight.verified_implications) if prev_insight is not None else 0
    return ProxyScore(
        open_subgoals=ins.open_subgoal_count(),
        verified_implications=verified_now,
        verified_delta=verified_now - verified_before,
        falsification_hits=len(ins.falsified_implications),
    )


def better(a: ProxyScore, b: ProxyScore) -> bool:
    """a 是否比 b 更该被 exploit（严格优于）。只做相对判断。"""
    return a.ranking_key() < b.ranking_key()


def rank_by_proxy(scored: Sequence[tuple[str, ProxyScore]]) -> list[str]:
    """把 [(state_id, ProxyScore)] 按 exploit 优先级从高到低排序，返回 state_id 列表。"""
    return [sid for sid, _ in sorted(scored, key=lambda item: item[1].ranking_key())]


@dataclass(frozen=True)
class ProgressSample:
    """一个 cycle 的进展采样，用于窗口化 progress_rate（§5.5 退火输入）。"""

    verified_delta: int  # 该 cycle 新增 verified_implications 总数
    dispatches: int  # 该 cycle 的 dispatch 数


def progress_rate(samples: Sequence[ProgressSample]) -> float:
    """最近 W 个 cycle 的 `Σverified_delta / Σdispatch`（§5.5）。

    复用 §3.1 的 proxy 信号驱动自适应退火 τ：进展快 → 早转 exploit；进展慢 → 多留
    explore。窗口 W 由调用方通过传入的 samples 数量控制。无 dispatch 时返回 0。
    """
    total_dispatch = sum(max(0, s.dispatches) for s in samples)
    if total_dispatch <= 0:
        return 0.0
    total_progress = sum(s.verified_delta for s in samples)
    return total_progress / total_dispatch
