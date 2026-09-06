"""调度层搜索图（§2 数据模型）。

对应 `docs/orchestrator-mcts-design.md`：

- §1 正名：这不是 MCTS。没有 rollout、reward 极稀疏、状态不可逆。
- §2.1 全局单一 pool + 节点是"视图/增量"：权威材料（verified_propositions/）只存
  一份，节点不复制整套 prop，只持有 `delta`（相对父节点新增的 prop/lemma 引用）。
  节点视图 = 父视图 ∪ 自身 delta（语义成立，存储上是增量）。
- §2.2 节点 schema（本期最小集）。

本模块严格落在冻结边界之外（§8）：不触碰 `orchestrator.md` / `WorkerManager`
调度语义 / 任何 role 的 prompt。它是可选的调度层元数据结构。

调度决策（exploit 哪个方向 / 要不要自由探索）不在本模块之内——那完全交给
orchestrator 的 LLM 自主判断。本模块只提供纯数据结构与图运算。
"""
from __future__ import annotations

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
    # curator-owned canonical difficulty attacked by this worker, if any.
    difficulty_id: Optional[str] = None
    # result = raw worker evidence; the curator owns mathematical difficulty state.
    result: Optional[str] = None
    insight: "Optional[InsightRecord]" = None
    impact_status: str = "not_required"  # not_required / pending / assessed
    impact_relation: Optional[str] = None
    prune_reason: Optional[str] = None  # pruned 时必填（§6.3）

    @property
    def attempt_id(self) -> str:
        """`state_id` 的语义化别名；保留旧字段以兼容现有日志与 API。"""
        return self.state_id

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

        DAG 里同一祖先可经多条路径到达，这里**去重**。
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

    def prune(self, state_id: str, reason: str) -> None:
        """标记 pruned（§6.3）。节点**不删除**，仅置状态并记死因（供 constraints view）。"""
        if not reason:
            raise ValueError("prune_reason is required (design §6.3)")
        node = self._nodes[state_id]
        node.status = NodeStatus.PRUNED
        node.prune_reason = reason
