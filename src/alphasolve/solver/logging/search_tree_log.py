"""搜索树观测日志写入器（每 selection cycle 落一次快照）。

不同于 `EventLogWriter`（记 agent 事件流），本写入器专门把调度层 `SearchSession`
的搜索树周期性拍照，同时产出两份文件：

- ``search_tree.log``   人类可读：每 cycle 一段带时间戳的 ASCII 树 + 逐节点明细 + 统计头。
- ``search_tree.jsonl`` 机器可读：每 cycle 一行 JSON（`build_search_snapshot` 的结构），
  便于后续可视化 / 增量分析。

写入器对主流程**完全无害**：`snapshot()` 内部吞掉所有异常，绝不因日志失败而影响
orchestrator 的真实执行。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from alphasolve.solver.search.report import build_search_snapshot, render_search_tree

if TYPE_CHECKING:
    from alphasolve.solver.search.session import SearchSession


class SearchTreeLogWriter:
    """把 `SearchSession` 的搜索树逐 cycle 落盘（人读 .log + 机读 .jsonl）。"""

    def __init__(self, log_path: Path) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._text_path = log_path
        self._jsonl_path = log_path.with_suffix(".jsonl")
        self._text = open(log_path, "w", encoding="utf-8")
        self._jsonl = open(self._jsonl_path, "w", encoding="utf-8")
        self._count = 0
        self._opened = True
        self._write_header()

    def _write_header(self) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._text.write(f"═══ SEARCH TREE · {ts}\n")
        self._text.write(
            "每个 selection cycle 一段快照：ASCII 树 + 逐节点明细"
            "（hint / delta / result / lemma）+ 动态统计。\n\n"
        )
        self._text.flush()

    def snapshot(self, session: "SearchSession", *, label: str = "") -> None:
        """落一份当前搜索树快照。任何异常都被吞掉，不影响主流程。"""
        if not self._opened:
            return
        try:
            self._count += 1
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            snap = build_search_snapshot(session)
            snap["snapshot_seq"] = self._count
            snap["timestamp"] = ts
            if label:
                snap["label"] = label

            # 机读：一行 JSON
            self._jsonl.write(json.dumps(snap, ensure_ascii=False) + "\n")
            self._jsonl.flush()

            # 人读：分隔 + 树
            head = f"── snapshot #{self._count} · cycle={session.cycle} · {ts}"
            if label:
                head += f" · {label}"
            self._text.write(head + "\n")
            self._text.write(render_search_tree(session) + "\n\n")
            self._text.flush()
        except Exception:
            # 观测层绝不影响主流程
            pass

    def close(self) -> None:
        if not self._opened:
            return
        self._opened = False
        try:
            self._text.close()
        finally:
            self._jsonl.close()


__all__ = ["SearchTreeLogWriter"]
