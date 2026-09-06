"""Insight backpropagation 与 falsification（§6）。

对应 `docs/orchestrator-mcts-design.md` §6：

- §6.1 result / insight 严格分离：`result` 是事实（raw 错误、卡在哪一步），存在
  `SearchNode.result`；`insight` 是因果教训，是本模块的 `InsightRecord`。合并会让
  LLM 混淆"发生了什么"和"为什么"，破坏抽象质量，也破坏 §7 的分层压缩基础。
- §6.2 半结构化 insight schema，`open_subgoals` 承载 gap 的结构化落地（不落绝对分）。
- §6.3 横向兄弟合成：每个祖先 = 对其**直接子节点** insight 的再抽象，逐层刷新；
  falsification 硬砍含被证伪 ansatz 的节点（节点不删除，仅标 pruned + prune_reason）。

抽象步骤本身需要 LLM，因此以可注入的 `InsightAbstractor` 协议表达，本模块不绑定
任何具体 LLM（保持可单元测试、且不 import agent/llm 层）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

import yaml

from .graph import NodeStatus, SearchGraph, SearchNode


@dataclass
class FalsifiedImplication:
    """一条已被证伪的推导及其反例（§6.2）。"""

    implication: str
    counterexample: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"implication": self.implication, "counterexample": self.counterexample}


@dataclass
class InsightRecord:
    """半结构化因果教训（§6.2）。

    刻意**不存** "还差 40%" 这类绝对 gap 分（自欺欺人）；改用 `open_subgoals`——
    "还剩几个明确未证子目标"，天然可回传、可合成、可被 falsification 判死。
    """

    verified_implications: list[str] = field(default_factory=list)  # proxy 增长率来源（§3.1）
    falsified_implications: list[FalsifiedImplication] = field(default_factory=list)
    open_subgoals: list[str] = field(default_factory=list)  # gap 的结构化落地（§4）
    direction_summary: str = ""  # <=200 字自然语言总结

    # ---- 序列化（yaml，保 key 顺序 + 允许 unicode 数学符号） ----------

    def to_yaml(self) -> str:
        data = {
            "verified_implications": list(self.verified_implications),
            "falsified_implications": [f.to_dict() for f in self.falsified_implications],
            "open_subgoals": list(self.open_subgoals),
            "direction_summary": self.direction_summary,
        }
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)

    @classmethod
    def from_yaml(cls, text: str) -> "InsightRecord":
        data = yaml.safe_load(text) or {}
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "InsightRecord":
        falsified: list[FalsifiedImplication] = []
        for item in data.get("falsified_implications", []) or []:
            if isinstance(item, dict):
                falsified.append(
                    FalsifiedImplication(
                        implication=str(item.get("implication", "")),
                        counterexample=str(item.get("counterexample", "")),
                    )
                )
            else:  # 容忍纯字符串形式
                falsified.append(FalsifiedImplication(implication=str(item)))
        return cls(
            verified_implications=[str(x) for x in (data.get("verified_implications") or [])],
            falsified_implications=falsified,
            open_subgoals=[str(x) for x in (data.get("open_subgoals") or [])],
            direction_summary=str(data.get("direction_summary", "")),
        )

    def open_subgoal_count(self) -> int:
        """按**结构化条目数**计数（§3.1 防过拟合：不按内容关键词计）。"""
        return len(self.open_subgoals)


class InsightAbstractor(Protocol):
    """把一组子节点 insight 再抽象为父节点 insight（§6.3，需 LLM）。

    实现方可以是一次 LLM 调用；单元测试可注入确定性的 fake。契约：输出的
    `direction_summary` 不超过 `max_words` 词。
    """

    def abstract(
        self, children_insights: list[InsightRecord], *, max_words: int = 200
    ) -> InsightRecord: ...


# 也允许直接传一个函数（比 Protocol 更轻），二者择一。
AbstractFn = Callable[[list["InsightRecord"], int], "InsightRecord"]


def _call_abstractor(
    abstractor: "InsightAbstractor | AbstractFn",
    children_insights: list[InsightRecord],
    max_words: int,
) -> InsightRecord:
    if hasattr(abstractor, "abstract"):
        return abstractor.abstract(children_insights, max_words=max_words)  # type: ignore[union-attr]
    return abstractor(children_insights, max_words)  # type: ignore[misc,operator]


def backpropagate_insights(
    graph: SearchGraph,
    leaf_id: str,
    abstractor: "InsightAbstractor | AbstractFn",
    *,
    max_words: int = 200,
) -> list[SearchNode]:
    """沿 leaf 的祖先链**逐层向上**刷新 insight（§6.3）。

    每个祖先的新 insight = 对其**直接子节点** insight 的再抽象（横向兄弟合成），
    不是把所有叶子平铺汇总到根，也不是 append。DAG 下按 depth 从深到浅处理，保证
    刷新某祖先时其子节点已就绪。

    返回被刷新（写回 insight）的祖先节点列表。
    """
    ancestors = graph.ancestors(leaf_id)
    # depth 从深到浅：先刷新更靠近 leaf 的祖先
    ancestors.sort(key=lambda n: n.depth, reverse=True)
    updated: list[SearchNode] = []
    for anc in ancestors:
        child_insights = [
            graph.get(cid).insight
            for cid in anc.children
            if graph.get(cid).insight is not None
        ]
        if not child_insights:
            continue
        anc.insight = _call_abstractor(abstractor, child_insights, max_words)
        updated.append(anc)
    return updated


DependsOnAnsatz = Callable[[SearchNode, str], bool]


def _default_depends_on_ansatz(node: SearchNode, ansatz: str) -> bool:
    """默认判据：节点的 hypothesis 或 insight 文本引用了被证伪的 ansatz（子串匹配）。

    §3.1/§8.4 防过拟合：这里只做**结构性/引用性**匹配，不硬编码任何领域数学名词。
    调用方可注入更精确的依赖判据（如基于 prop 引用图）。
    """
    needle = ansatz.strip()
    if not needle:
        return False
    if needle in node.hypothesis:
        return True
    ins = node.insight
    if ins is None:
        return False
    haystack = "\n".join(
        list(ins.verified_implications)
        + list(ins.open_subgoals)
        + [ins.direction_summary]
        + [f.implication for f in ins.falsified_implications]
    )
    return needle in haystack


def apply_falsification(
    graph: SearchGraph,
    refuted_ansatz: str,
    *,
    reason: Optional[str] = None,
    depends_on: DependsOnAnsatz = _default_depends_on_ansatz,
) -> list[SearchNode]:
    """数学场景硬证伪（§6.3，自研超纲增强，非 Arbor 原生）。

    新引理证出 `¬B` → 扫全局 DAG，硬砍**所有**依赖 ansatz B 的活跃节点。被砍节点
    仅置 `status=pruned` 并写 `prune_reason`（不删除），死因随后进 constraints view
    作为可复用负经验。

    注意（§6.3）：这是"逻辑级硬证伪"，仅用于确实证明了 `¬B` 的情形；worker 实现挂了
    / 评估器崩了属 informative failure，**不应**调用本函数去剪。

    返回被剪掉的节点列表。
    """
    prune_reason = reason or f"falsified: proved ¬({refuted_ansatz})"
    pruned: list[SearchNode] = []
    for node in graph.nodes():
        if node.status == NodeStatus.PRUNED:
            continue
        if depends_on(node, refuted_ansatz):
            graph.prune(node.state_id, prune_reason)
            pruned.append(node)
    return pruned
