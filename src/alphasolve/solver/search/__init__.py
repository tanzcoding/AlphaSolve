"""tree/graph-structured best-first search 调度层内核。

落地 `docs/orchestrator-mcts-design.md`（原文件名 orchestrator-mcts-design.md，但 §1
已正名：这**不是** MCTS——无 rollout、reward 极稀疏、状态不可逆）。本子包提供**纯算法/
数据结构**，不含任何 LLM 调用、不 import agent/llm 层，严格落在冻结边界之外（§8）：

- graph.py      §2 数据模型（全局 pool 视图/增量 + DAG + state_id）
- insight.py    §6 result/insight 分离 + 半结构化 schema + 横向兄弟合成 + falsification
- proxy.py      §3 廉价分级信号（open_subgoals / verified_impl 增量 / falsif 命中）
- compaction.py §7 分层无损/有损压缩（参考 codex handoff）

调度决策（exploit 哪个方向、要不要自由探索）不在本子包之内，完全交给
orchestrator 主循环里的 LLM 根据 `SearchSession.advise()` 给出的客观事实
（frontier / pending_research_impacts）自行判断，见 `prompts/orchestrator.md`。

需 LLM 的两处能力（insight 抽象、轨迹摘要）均以 Protocol 表达，具体实现留待接入时提供
（接入 orchestrator 主循环涉及冻结的 orchestrator.md，须先解冻，见 §8.3）。
"""
from __future__ import annotations

from .compaction import (
    CompactionBudget,
    HandoffSummary,
    LayeredContext,
    TrajectorySummarizer,
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
from .report import build_search_snapshot, render_search_tree
from .scheduler import SchedulerStateStore
from .session import SearchSession

__all__ = [
    # report (read-only observability)
    "build_search_snapshot",
    "render_search_tree",
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
    # scheduler (cycle counter + attempt DAG crash-recovery log)
    "SchedulerStateStore",
    # session (orchestration facade)
    "SearchSession",
    # compaction
    "CompactionBudget",
    "HandoffSummary",
    "LayeredContext",
    "TrajectorySummarizer",
    "build_worker_context",
    "trim_ancestor_insights",
    "trim_vp_index",
]
