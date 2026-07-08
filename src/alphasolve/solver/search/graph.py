"""调度层搜索图（§2 数据模型 + §5.4 visit_count 全祖先回传）。

对应 `docs/orchestrator-mcts-design.md`：

- §1 正名：这不是 MCTS。没有 rollout、reward 极稀疏、状态不可逆。实现的是
  **tree/graph-structured dueling bandit + best-first search**。本模块只提供
  纯数据结构与图运算，不含任何 LLM 调用，可独立单元测试。
- §2.1 全局单一 pool + 节点是"视图/增量"：权威材料（verified_propositions/）只存
  一份，节点不复制整套 prop，只持有 `delta`（相对父节点新增的 prop/lemma 引用）。
  节点视图 = 父视图 ∪ 自身 delta（语义成立，存储上是增量）。
- §2.2 节点 schema（本期最小集）。
- §5.4 visit_count 沿**全祖先链**累加（治"一条道走到黑"）；wins/duels 只在
  局部同父兄弟池累加（§5.1）。

本模块严格落在冻结边界之外（§8）：不触碰 `orchestrator.md` / `WorkerManager`
调度语义 / 任何 role 的 prompt。它是可选的调度层元数据结构。
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Iterable, Optional

if TYPE_CHECKING:  # 避免与 insight.py 的循环 import；仅类型注解用
    from .insight import InsightRecord


class NodeStatus(str, Enum):
    """节点生命周期状态（§2.2 `status`）。

    注意 `pruned` 只是标记，节点**不删除**（§6.3：保留为 evidence，死因入
    constraints view 作为可复用负经验）。
    """

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    COOLED = "cooled"
    PRUNED = "pruned"
    MERGED = "merged"


@dataclass
class Delta:
    """节点相对父节点新增的那一个 prop/lemma 引用（§2.1）。

    只存**引用**（路径 / UUID），不复制命题正文——权威材料在全局 pool 里唯一存一份。
    """

    proof_ref: Optional[str] = None
    code_ref: Optional[str] = None

    def refs(self) -> list[str]:
        return [r for r in (self.proof_ref, self.code_ref) if r]


@dataclass
class SearchNode:
    """搜索图中的一个节点（§2.2 front matter 最小集）。

    字段写入者归属见设计文档 §2.2 表格。这里只承载调度层元数据，命题正文与
    proof 内容仍在文件系统 pool 中，本节点通过 `delta` 引用它们。
    """

    hypothesis: str
    state_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    parents: list[str] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    depth: int = 0
    status: NodeStatus = NodeStatus.PENDING
    delta: Delta = field(default_factory=Delta)
    # result = 事实（raw 错误、卡在哪一步）；insight = 因果教训。二者严格分离（§6.1）。
    result: Optional[str] = None
    insight: "Optional[InsightRecord]" = None
    # 调度统计
    visit_count: int = 0  # 子树累计 dispatch，全祖先回传（§5.4）
    self_picked_count: int = 0  # 自身被直接 dispatch 次数
    wins: float = 0.0  # 局部 pairwise 胜率池（仅同父兄弟内，§5.3）；平局可 +0.5
    duels: int = 0
    prune_reason: Optional[str] = None  # pruned 时必填（§6.3）
    crossover_candidate: bool = False  # comparable-tie 标记（§5.3 → §6.4）

    @property
    def q_value(self) -> float:
        """局部 pairwise 胜率 Q = wins / duels。

        从未比过（duels==0）返回中性 0.5，而非 0——避免把"没测过"当成"输光"，
        否则新 arm 会被系统性压制（§5.3 平局 +0.5 无偏的同一动机）。
        """
        if self.duels <= 0:
            return 0.5
        return self.wins / self.duels

    def is_active(self) -> bool:
        return self.status in (NodeStatus.PENDING, NodeStatus.RUNNING, NodeStatus.COOLED)


class SearchGraph:
    """全局搜索 DAG（§2）。

    - 全局单一 pool 的**视图聚合**：节点存增量 `delta`，`materialize_view` 按祖先链
      程序化聚合出该节点可见的全部 prop 引用（零 LLM 成本，§7.3）。
    - `parents` 为 list → 支持 crossover（多父合并成图节点，§6.4）。纯树是图的特例。
    """

    def __init__(self) -> None:
        self._nodes: dict[str, SearchNode] = {}
        self.total_visits: int = 0

    # ---- 基础增删查 ---------------------------------------------------

    def add_node(
        self,
        hypothesis: str,
        *,
        parents: Optional[Iterable[str]] = None,
        delta: Optional[Delta] = None,
        state_id: Optional[str] = None,
    ) -> SearchNode:
        parent_ids = list(parents or [])
        for pid in parent_ids:
            if pid not in self._nodes:
                raise KeyError(f"unknown parent state_id: {pid}")
        node = SearchNode(hypothesis=hypothesis, parents=parent_ids, delta=delta or Delta())
        if state_id is not None:
            node.state_id = state_id
        if node.state_id in self._nodes:
            raise ValueError(f"duplicate state_id: {node.state_id}")
        # depth = 所有父节点 depth 的最大值 + 1（root depth=0）
        node.depth = 0 if not parent_ids else 1 + max(self._nodes[p].depth for p in parent_ids)
        self._nodes[node.state_id] = node
        for pid in parent_ids:
            parent = self._nodes[pid]
            if node.state_id not in parent.children:
                parent.children.append(node.state_id)
        return node

    def get(self, state_id: str) -> SearchNode:
        return self._nodes[state_id]

    def __contains__(self, state_id: str) -> bool:
        return state_id in self._nodes

    def __len__(self) -> int:
        return len(self._nodes)

    def nodes(self) -> list[SearchNode]:
        return list(self._nodes.values())

    def roots(self) -> list[SearchNode]:
        return [n for n in self._nodes.values() if not n.parents]

    # ---- 图运算 -------------------------------------------------------

    def ancestors(self, state_id: str) -> list[SearchNode]:
        """返回全部祖先（DAG 去重，不含自身）。

        DAG 里同一祖先可经多条路径到达，这里**去重**——保证 visit_count 回传时每个
        祖先只 +1（§5.4）。
        """
        seen: set[str] = set()
        order: list[str] = []
        stack = list(self._nodes[state_id].parents)
        while stack:
            pid = stack.pop()
            if pid in seen or pid not in self._nodes:
                continue
            seen.add(pid)
            order.append(pid)
            stack.extend(self._nodes[pid].parents)
        return [self._nodes[pid] for pid in order]

    def siblings(self, state_id: str) -> list[SearchNode]:
        """同父兄弟：与本节点共享至少一个父节点的其他节点（§5.1 局部 pairwise 域）。"""
        node = self._nodes[state_id]
        out: dict[str, SearchNode] = {}
        for pid in node.parents:
            for cid in self._nodes[pid].children:
                if cid != state_id:
                    out[cid] = self._nodes[cid]
        return list(out.values())

    def common_ancestor(self, a: str, b: str) -> Optional[SearchNode]:
        """最近公共祖先（§7.3 critic 视图组装用）。

        取 a、b 祖先集合的交集中 depth 最大者。无公共祖先返回 None。
        """
        anc_a = {n.state_id: n for n in self.ancestors(a)}
        anc_a[a] = self._nodes[a]
        anc_b = {n.state_id for n in self.ancestors(b)}
        anc_b.add(b)
        shared = [anc_a[sid] for sid in anc_a if sid in anc_b]
        if not shared:
            return None
        return max(shared, key=lambda n: n.depth)

    def materialize_view(self, state_id: str) -> list[str]:
        """程序化聚合节点可见的全部 prop 引用（§2.1 / §7.3，零 LLM 成本）。

        视图 = 所有祖先的 delta ∪ 自身 delta。返回去重后的引用列表（保持自底向上、
        近祖先靠后的稳定顺序）。这就是"整套 prop set 当 state"的语义实现，存储上仍是增量。
        """
        node = self._nodes[state_id]
        refs: list[str] = []
        seen: set[str] = set()
        # 从最远祖先到自身：ancestors() 是 BFS-from-parent 顺序，逆序得到自根向下
        chain = list(reversed(self.ancestors(state_id))) + [node]
        for n in chain:
            for ref in n.delta.refs():
                if ref not in seen:
                    seen.add(ref)
                    refs.append(ref)
        return refs

    # ---- 调度统计回传 -------------------------------------------------

    def on_dispatch(self, state_id: str) -> None:
        """一次 dispatch 的统计回传（§5.4）。

        - 自身 self_picked_count += 1；
        - 自身 + **全部祖先** visit_count += 1（探索偏置沿整条祖先链下降）；
        - 全局 total_visits += 1（UCB-style 的 N）。

        胜率只在局部兄弟池累加（record_duel），不在这里动。
        """
        node = self._nodes[state_id]
        node.self_picked_count += 1
        node.visit_count += 1
        for anc in self.ancestors(state_id):
            anc.visit_count += 1
        self.total_visits += 1

    def explore_bonus(self, state_id: str, *, c: float = 1.0) -> float:
        """UCB-style 探索项 c * sqrt(ln N / n)（§5.4 只作软探索偏置，不做统计裁决）。

        n 用 self_picked_count（自身被直接选的次数）+1 平滑；N 用 total_visits。
        某支被深挖 → 其 n 升高 → bonus 下降 → 算力被推向别处。
        """
        n = self._nodes[state_id].self_picked_count + 1
        big_n = max(1, self.total_visits)
        return c * math.sqrt(math.log(big_n + 1) / n)

    def prune(self, state_id: str, reason: str) -> None:
        """标记 pruned（§6.3）。节点**不删除**，仅置状态并记死因（供 constraints view）。"""
        if not reason:
            raise ValueError("prune_reason is required (design §6.3)")
        node = self._nodes[state_id]
        node.status = NodeStatus.PRUNED
        node.prune_reason = reason
