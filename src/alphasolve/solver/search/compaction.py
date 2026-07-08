"""上下文压缩：分层无损/有损 + 公共祖先增量组装（§7，参考 codex）。

对应 `docs/orchestrator-mcts-design.md` §7。关键澄清（读 `codex/codex-rs/core/src/
compact.rs` + `prompts/templates/compact/` 得出）：**codex 的压缩并非"全量有损"**——
它把权威输入（user 消息）逐字保留，只对模型自己的推理/工具轨迹做摘要。这与 §6.1 的
result/insight 分离同构，也化解了"数学压缩会丢常数/命题/依赖"的担忧。

据此本模块落地"分层"策略：

- **无损层**（= codex 逐字保留的 user 消息）：verified_propositions 的精确陈述、常数、
  prop 依赖、`delta` 引用。绝不摘要、绝不改写数学陈述。
- **有损层**（= codex 摘要的模型轨迹）：节点 `result`（raw 错误、死路、尝试轨迹），
  LLM 蒸馏为 `insight`（半结构化，保 open_subgoals/implications）。
- **预算 + 丢最旧**（= codex 的 remove_first_item）：ancestor insights ≤ N 层、vp 索引
  ≤ M 条，超限时丢**最远**祖先，保近祖先与 summary。
- **复用 prop 结构减少 LLM 调用**（§7.3）：平时 state = pool 视图 + delta，程序化聚合，
  零 LLM 成本；仅当抽 2 个节点做 critic 时，才组装 [公共祖先视图]+[A 增量]+[B 增量]
  交给长程 harness 按 handoff 方式压缩，天然避免重复压缩公共部分。

有损摘要需 LLM，以 `TrajectorySummarizer` 协议表达，本模块不绑定具体实现。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, Sequence

from .graph import SearchGraph


# 预算默认值（§7.2，对齐 design/06 §3）
DEFAULT_MAX_ANCESTOR_INSIGHT_LAYERS = 8
DEFAULT_MAX_VP_INDEX = 20


@dataclass
class CompactionBudget:
    max_ancestor_insight_layers: int = DEFAULT_MAX_ANCESTOR_INSIGHT_LAYERS
    max_vp_index: int = DEFAULT_MAX_VP_INDEX


@dataclass
class CriticView:
    """喂给 pairwise critic 的分层视图（§7.3）。common 部分只组装一次，避免重复压缩。"""

    common_ancestor_id: Optional[str]
    common_view_refs: list[str] = field(default_factory=list)
    a_delta_refs: list[str] = field(default_factory=list)
    b_delta_refs: list[str] = field(default_factory=list)


def assemble_critic_view(graph: SearchGraph, a_id: str, b_id: str) -> CriticView:
    """组装 [公共祖先视图] + [A 增量] + [B 增量]（§7.3，零 LLM）。

    A/B 的"增量"= 各自 materialize_view 相对公共祖先视图的**新增**引用。这样公共部分
    只出现一次，天然避免重复压缩，也让 critic 聚焦在真正分岔的地方。
    """
    ca = graph.common_ancestor(a_id, b_id)
    common_refs = graph.materialize_view(ca.state_id) if ca is not None else []
    common_set = set(common_refs)
    a_delta = [r for r in graph.materialize_view(a_id) if r not in common_set]
    b_delta = [r for r in graph.materialize_view(b_id) if r not in common_set]
    return CriticView(
        common_ancestor_id=ca.state_id if ca is not None else None,
        common_view_refs=common_refs,
        a_delta_refs=a_delta,
        b_delta_refs=b_delta,
    )


def trim_ancestor_insights(
    insights_near_to_far: Sequence[str],
    *,
    max_layers: int = DEFAULT_MAX_ANCESTOR_INSIGHT_LAYERS,
) -> list[str]:
    """预算裁剪：保留**近端** max_layers 层 ancestor insight，丢最远（§7.2）。

    入参按"近→远"排列（近祖先在前）。对齐 codex 的 remove_first_item：溢出时丢最旧/最远，
    保住与当前节点最相关的近祖先。
    """
    if max_layers <= 0:
        return []
    return list(insights_near_to_far[:max_layers])


def trim_vp_index(refs: Sequence[str], *, max_items: int = DEFAULT_MAX_VP_INDEX) -> list[str]:
    """vp 索引裁剪：保留前 max_items 条引用（§7.2）。无损层只裁**索引条数**，不改任何陈述内容。"""
    if max_items <= 0:
        return []
    return list(refs[:max_items])


class TrajectorySummarizer(Protocol):
    """把节点探索轨迹（result，事实层）有损摘要为交接文本（§7.1，需 LLM）。

    对齐 codex：只摘要"模型自己的轨迹"，权威内容不走这里。单元测试可注入 fake。
    """

    def summarize(self, trajectory_text: str, *, max_words: int = 300) -> str: ...


@dataclass
class HandoffSummary:
    """codex 风格的交接摘要（§7.1 四类）：让下一个 LLM 无缝接着干。"""

    progress_and_decisions: str = ""  # 当前进展与关键决策
    context_and_constraints: str = ""  # 重要上下文/约束/偏好（含 prune_reason 硬约束）
    next_steps: list[str] = field(default_factory=list)  # 剩余待办（来自 open_subgoals）
    key_data_refs: list[str] = field(default_factory=list)  # 继续所需关键数据/引用（无损）

    def render(self) -> str:
        lines = ["## Progress & Key Decisions", self.progress_and_decisions.strip(), ""]
        lines += ["## Context & Constraints", self.context_and_constraints.strip(), ""]
        lines += ["## Next Steps"]
        lines += [f"- {s}" for s in self.next_steps] or ["- (none)"]
        lines += ["", "## Key Data / References (verbatim, do not paraphrase)"]
        lines += [f"- {r}" for r in self.key_data_refs] or ["- (none)"]
        return "\n".join(lines)


@dataclass
class LayeredContext:
    """一次压缩产物：无损权威层 + 有损轨迹摘要层（§7.2）。"""

    authoritative_refs: list[str] = field(default_factory=list)  # 无损：绝不摘要
    trajectory_summary: str = ""  # 有损：LLM 蒸馏
    handoff: Optional[HandoffSummary] = None


def build_worker_context(
    graph: SearchGraph,
    node_id: str,
    *,
    ancestor_insight_summaries_near_to_far: Sequence[str],
    trajectory_text: str,
    summarizer: TrajectorySummarizer,
    budget: Optional[CompactionBudget] = None,
    max_words: int = 300,
) -> LayeredContext:
    """为某节点组装分层上下文（§7.2）。

    - 无损层：该节点 materialize_view 的 prop 引用（vp 索引），按预算裁剪条数（不改内容）。
    - 有损层：ancestor insight 摘要按层数裁剪（丢最远）+ 本节点轨迹经 summarizer 蒸馏。
    """
    budget = budget or CompactionBudget()
    authoritative = trim_vp_index(
        graph.materialize_view(node_id), max_items=budget.max_vp_index
    )
    kept_ancestor = trim_ancestor_insights(
        ancestor_insight_summaries_near_to_far, max_layers=budget.max_ancestor_insight_layers
    )
    trajectory_summary = summarizer.summarize(trajectory_text, max_words=max_words) if trajectory_text else ""
    combined = "\n\n".join(filter(None, ["\n".join(kept_ancestor), trajectory_summary]))
    return LayeredContext(
        authoritative_refs=authoritative,
        trajectory_summary=combined,
    )
