"""调度层搜索树的**只读**快照与渲染（观测层，冻结边界之外 §8）。

本模块把 `SearchSession` / `SearchGraph` 的内存状态拍成两种可落盘的形态：

- `build_search_snapshot(session)`：结构化 dict（`stats` 动态统计 + `nodes` 明细），
  供 `search_tree.jsonl` 逐 cycle 落盘、后续机器解析/可视化。
- `render_search_tree(session)`：人类可读的 ASCII 树 + 逐节点明细 + 统计头，
  供 `search_tree.log` 阅读。

**严格只读**：只观察 graph/session 已有字段，不调用 LLM、不改任何状态、不触碰
worker/调度语义。任何异常都不应影响主流程（调用方负责吞掉异常）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from .graph import NodeStatus, SearchGraph, SearchNode

if TYPE_CHECKING:
    from .insight import InsightRecord
    from .session import SearchSession


def _short(state_id: str, n: int = 8) -> str:
    return state_id[:n]


def _insight_to_dict(insight: "Optional[InsightRecord]") -> Optional[dict[str, Any]]:
    if insight is None:
        return None
    return {
        "verified_implications": list(insight.verified_implications),
        "falsified_implications": [f.to_dict() for f in insight.falsified_implications],
        "open_subgoals": list(insight.open_subgoals),
        "direction_summary": insight.direction_summary,
    }


def _node_to_dict(graph: SearchGraph, node: SearchNode) -> dict[str, Any]:
    """把单个节点拍成明细 dict：身份 + 内容(hint/delta/result) + lemma。"""
    return {
        # 身份 / 结构
        "state_id": node.state_id,
        "short_id": _short(node.state_id),
        "hint": node.hypothesis,  # hypothesis 即 spawn 时的 hint
        "difficulty_id": node.difficulty_id,
        "method_id": getattr(node, "method_id", None),
        "worker_id": getattr(node, "worker_id", None),
        "impact_status": node.impact_status,
        "impact_relation": node.impact_relation,
        "depth": node.depth,
        "status": node.status.value,
        "parents": list(node.parents),
        "children": list(node.children),
        # 内容：delta 引用（指向权威 pool）+ result 事实
        "delta_refs": node.delta.refs(),
        "result": node.result,
        # lemma / insight（因果教训，§6）
        "insight": _insight_to_dict(node.insight),
        "prune_reason": node.prune_reason,
    }


def build_search_snapshot(session: "SearchSession") -> dict[str, Any]:
    """一份完整的搜索树快照：动态统计 `stats` + 逐节点明细 `nodes`。

    lemma 口径（基于 §6 InsightRecord 的条目计数）：
    - verified_lemmas   = Σ len(insight.verified_implications)
    - unverified_lemmas = Σ len(insight.open_subgoals)      （未证子目标）
    - falsified_lemmas  = Σ len(insight.falsified_implications)
    另给出节点视角的 `nodes_with_verified` / `nodes_with_proof_ref` 以便对照。
    """
    graph = session.graph
    nodes = graph.nodes()

    status_counts: dict[str, int] = {}
    verified_lemmas = 0
    unverified_lemmas = 0
    falsified_lemmas = 0
    nodes_with_verified = 0
    nodes_with_proof_ref = 0
    for node in nodes:
        status_counts[node.status.value] = status_counts.get(node.status.value, 0) + 1
        if node.delta.refs():
            nodes_with_proof_ref += 1
        ins = node.insight
        if ins is not None:
            v = len(ins.verified_implications)
            verified_lemmas += v
            unverified_lemmas += len(ins.open_subgoals)
            falsified_lemmas += len(ins.falsified_implications)
            if v > 0:
                nodes_with_verified += 1

    stats = {
        "cycle": session.cycle,
        "total_nodes": len(nodes),
        "worker_nodes": max(0, len(nodes) - 1),  # 除去 root
        "active_nodes": sum(1 for n in nodes if n.is_active()),
        "status_counts": status_counts,
        "verified_lemmas": verified_lemmas,
        "unverified_lemmas": unverified_lemmas,
        "falsified_lemmas": falsified_lemmas,
        "nodes_with_verified": nodes_with_verified,
        "nodes_with_proof_ref": nodes_with_proof_ref,
        "solved": session._solved,
    }

    return {
        "cycle": session.cycle,
        "stats": stats,
        "nodes": [_node_to_dict(graph, n) for n in nodes],
    }


# ---- 人类可读渲染 ----------------------------------------------------------

_STATUS_GLYPH = {
    NodeStatus.PENDING.value: "◌",
    NodeStatus.RUNNING.value: "◐",
    NodeStatus.DONE.value: "●",
    NodeStatus.COOLED.value: "◍",
    NodeStatus.PRUNED.value: "✗",
    NodeStatus.MERGED.value: "⊕",
}


def _fmt_stats(stats: dict[str, Any]) -> str:
    lines = [
        f"cycle={stats['cycle']}  total_nodes={stats['total_nodes']} "
        f"(workers={stats['worker_nodes']}, active={stats['active_nodes']})",
        f"lemmas: verified={stats['verified_lemmas']}  "
        f"unverified/open_subgoals={stats['unverified_lemmas']}  "
        f"falsified={stats['falsified_lemmas']}  "
        f"(nodes_with_verified={stats['nodes_with_verified']}, "
        f"with_proof_ref={stats['nodes_with_proof_ref']})",
        f"status: {stats['status_counts']}  solved={stats['solved']}",
    ]
    return "\n".join(lines)


def _fmt_node_detail(nd: dict[str, Any]) -> list[str]:
    """一个节点的多行明细块（缩进由调用方处理）。"""
    out: list[str] = []
    if nd["delta_refs"]:
        out.append(f"delta_refs: {', '.join(nd['delta_refs'])}")
    if nd["result"]:
        out.append(f"result: {_clip(nd['result'])}")
    ins = nd["insight"]
    if ins is not None:
        if ins["verified_implications"]:
            out.append(f"verified_lemmas ({len(ins['verified_implications'])}):")
            out.extend(f"  + {_clip(x)}" for x in ins["verified_implications"])
        if ins["open_subgoals"]:
            out.append(f"open_subgoals ({len(ins['open_subgoals'])}):")
            out.extend(f"  ? {_clip(x)}" for x in ins["open_subgoals"])
        if ins["falsified_implications"]:
            out.append(f"falsified ({len(ins['falsified_implications'])}):")
            out.extend(
                f"  ✗ {_clip(f['implication'])}"
                + (f"  ⟂ {_clip(f['counterexample'])}" if f.get("counterexample") else "")
                for f in ins["falsified_implications"]
            )
        if ins["direction_summary"]:
            out.append(f"summary: {_clip(ins['direction_summary'])}")
    if nd["prune_reason"]:
        out.append(f"prune_reason: {_clip(nd['prune_reason'])}")
    return out


def _clip(text: str, n: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def render_search_tree(session: "SearchSession") -> str:
    """渲染 ASCII 树 + 逐节点明细 + 统计头，供人类阅读。"""
    snapshot = build_search_snapshot(session)
    by_id = {nd["state_id"]: nd for nd in snapshot["nodes"]}
    graph = session.graph

    lines: list[str] = []
    lines.append(_fmt_stats(snapshot["stats"]))
    lines.append("")

    visited: set[str] = set()

    def walk(sid: str, prefix: str, is_last: bool, is_root: bool) -> None:
        if sid not in by_id:
            return
        nd = by_id[sid]
        glyph = _STATUS_GLYPH.get(nd["status"], "?")
        connector = "" if is_root else ("└─ " if is_last else "├─ ")
        difficulty_note = f" difficulty={nd['difficulty_id']}" if nd.get("difficulty_id") else ""
        header = (
            f"{prefix}{connector}{glyph} {nd['short_id']} "
            f"[{nd['status']}] d{nd['depth']}{difficulty_note}  {_clip(nd['hint'], 90)}"
        )
        lines.append(header)

        # 明细块缩进：root 无连接线，子节点根据是否最后一个决定竖线
        child_prefix = prefix if is_root else prefix + ("   " if is_last else "│  ")
        detail_prefix = child_prefix + "  "
        if not is_root:
            detail_prefix = prefix + ("   " if is_last else "│  ") + "  "
        for dl in _fmt_node_detail(nd):
            lines.append(f"{detail_prefix}{dl}")

        if sid in visited:
            lines.append(f"{detail_prefix}(already shown; DAG re-entry)")
            return
        visited.add(sid)

        children = list(nd["children"])
        for i, cid in enumerate(children):
            walk(cid, child_prefix, i == len(children) - 1, is_root=False)

    roots = graph.roots()
    for r in roots:
        walk(r.state_id, "", True, is_root=True)

    # 任何未被树遍历到的游离节点（理论上不该有，防御性输出）
    orphans = [sid for sid in by_id if sid not in visited and by_id[sid]["parents"]]
    stray = [sid for sid in orphans if sid not in visited]
    if stray:
        lines.append("")
        lines.append("(unreachable nodes):")
        for sid in stray:
            nd = by_id[sid]
            lines.append(f"  {nd['short_id']} [{nd['status']}] {_clip(nd['hint'], 90)}")

    return "\n".join(lines)


__all__ = ["build_search_snapshot", "render_search_tree"]
