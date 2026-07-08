"""tree/graph-structured dueling bandit + best-first search 调度层内核。

落地 `docs/orchestrator-mcts-design.md`（原文件名 orchestrator-mcts-design.md，但 §1
已正名：这**不是** MCTS——无 rollout、reward 极稀疏、状态不可逆）。本子包提供**纯算法/
数据结构**，不含任何 LLM 调用、不 import agent/llm 层，严格落在冻结边界之外（§8）：

- graph.py      §2 数据模型（全局 pool 视图/增量 + DAG + state_id）；§5.4 visit_count 全祖先回传
- insight.py    §6 result/insight 分离 + 半结构化 schema + 横向兄弟合成 + falsification
- proxy.py      §3 廉价分级信号（open_subgoals / verified_impl 增量 / falsif 命中）+ progress_rate
- selection.py  §5 四值 critic + 平局定案(+0.5 / incomparable) + K 次止损 + 硬配额 + 退火
- compaction.py §7 分层无损/有损压缩 + 公共祖先增量组装（参考 codex handoff）

需 LLM 的三处能力（insight 抽象、pairwise critic、轨迹摘要）均以 Protocol 表达，具体
实现留待接入时提供（接入 orchestrator 主循环涉及冻结的 orchestrator.md，须先解冻，见 §8.3）。
"""
from __future__ import annotations

from .compaction import (
    CompactionBudget,
    CriticView,
    HandoffSummary,
    LayeredContext,
    TrajectorySummarizer,
    assemble_critic_view,
    build_worker_context,
    trim_ancestor_insights,
    trim_vp_index,
)
from .graph import Delta, NodeStatus, SearchGraph, SearchNode
from .insight import (
    FalsifiedImplication,
    InsightAbstractor,
    InsightRecord,
    apply_falsification,
    backpropagate_insights,
)
from .proxy import (
    ProgressSample,
    ProxyScore,
    better,
    compute_proxy,
    progress_rate,
    rank_by_proxy,
)
from .selection import (
    CriticContext,
    DuelEffect,
    DuelLedger,
    DuelOutcome,
    PairwiseCritic,
    Quota,
    allocate_quota,
    anneal_exploit_fraction,
    jaccard,
    record_duel,
    select_diverse_fill,
    tie_break,
)
from .session import SearchSession

__all__ = [
    # graph
    "Delta",
    "NodeStatus",
    "SearchGraph",
    "SearchNode",
    # insight
    "FalsifiedImplication",
    "InsightAbstractor",
    "InsightRecord",
    "apply_falsification",
    "backpropagate_insights",
    # proxy
    "ProgressSample",
    "ProxyScore",
    "better",
    "compute_proxy",
    "progress_rate",
    "rank_by_proxy",
    # selection
    "CriticContext",
    "DuelEffect",
    "DuelLedger",
    "DuelOutcome",
    "PairwiseCritic",
    "Quota",
    "allocate_quota",
    "anneal_exploit_fraction",
    "jaccard",
    "record_duel",
    "select_diverse_fill",
    "tie_break",
    # session (orchestration facade)
    "SearchSession",
    # compaction
    "CompactionBudget",
    "CriticView",
    "HandoffSummary",
    "LayeredContext",
    "TrajectorySummarizer",
    "assemble_critic_view",
    "build_worker_context",
    "trim_ancestor_insights",
    "trim_vp_index",
]
