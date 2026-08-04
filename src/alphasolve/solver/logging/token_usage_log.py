"""跨角色 token 消耗聚合日志（R2）。

一个 run 共享一个 :class:`TokenUsageAggregator`，它监听所有 agent（orchestrator /
worker / 各 subagent）emit 的 ``usage`` 事件，按 **role 分组**累加，并在每次更新时
动态重写两份文件：

- ``token_usage.log``（人读）：分组汇总表 + 总计，随 run 实时刷新。
- ``token_usage.jsonl``（机读）：每次 LLM 调用一行原始 usage 记录。

线程安全：worker 在多线程里跑，聚合器内部用一把锁保护累加与文件写入。

本模块只做"观测/累加"，不改变任何 agent 的执行语义。usage 数据源是
``CompletionResponse.usage``（agent.py 在每轮模型返回后 emit 的 ``usage`` 事件）。
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

AgentEventSink = Callable[[dict[str, Any]], None]


@dataclass
class _RoleTally:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    # 该 role 下按 agent 名细分（例如 worker 下的 generator / verifier / theorem_checker）
    by_agent: dict[str, int] = field(default_factory=dict)

    def add(self, *, agent: str, inp: int, out: int, cached: int) -> None:
        self.calls += 1
        self.input_tokens += inp
        self.output_tokens += out
        self.cached_tokens += cached
        if agent:
            self.by_agent[agent] = self.by_agent.get(agent, 0) + inp + out


class TokenUsageAggregator:
    """一个 run 内共享、线程安全的 token 用量累加器 + 动态日志写入器。"""

    def __init__(self, run_dir: Path) -> None:
        self._lock = threading.Lock()
        self._log_path = run_dir / "token_usage.log"
        self._jsonl_path = run_dir / "token_usage.jsonl"
        run_dir.mkdir(parents=True, exist_ok=True)
        self._roles: dict[str, _RoleTally] = {}
        self._started = datetime.now()
        # 打开 jsonl 追加流（每次调用追加一行）
        self._jsonl = open(self._jsonl_path, "w", encoding="utf-8")
        self._closed = False
        # 先写一份空表，占位
        self._rewrite_human_log_locked()

    # ---- 事件入口 -------------------------------------------------------

    def sink_for(self, role: str) -> AgentEventSink:
        """返回一个 AgentEventSink：把该 role 下 agent emit 的 ``usage`` 事件计入聚合。"""

        def _sink(event: dict[str, Any]) -> None:
            if event.get("type") != "usage":
                return
            self.record(
                role=role,
                agent=str(event.get("agent") or ""),
                turn=int(event.get("turn") or 0),
                input_tokens=int(event.get("input_tokens") or 0),
                output_tokens=int(event.get("output_tokens") or 0),
                cached_tokens=int(event.get("cached_tokens") or 0),
            )

        return _sink

    def record(
        self,
        *,
        role: str,
        agent: str,
        turn: int,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
    ) -> None:
        with self._lock:
            if self._closed:
                return
            tally = self._roles.setdefault(role, _RoleTally())
            tally.add(agent=agent, inp=input_tokens, out=output_tokens, cached=cached_tokens)
            # 机读：追加一行
            self._jsonl.write(json.dumps({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "role": role,
                "agent": agent,
                "turn": turn,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_tokens": cached_tokens,
            }, ensure_ascii=False) + "\n")
            self._jsonl.flush()
            # 人读：整表重写（动态刷新）
            self._rewrite_human_log_locked()

    # ---- 人读表渲染 -----------------------------------------------------

    def _rewrite_human_log_locked(self) -> None:
        lines: list[str] = []
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"AlphaSolve token usage — updated {now}")
        lines.append("=" * 92)

        tot_calls = sum(t.calls for t in self._roles.values())
        tot_in = sum(t.input_tokens for t in self._roles.values())
        tot_out = sum(t.output_tokens for t in self._roles.values())
        tot_cached = sum(t.cached_tokens for t in self._roles.values())
        lines.append(
            f"TOTAL: calls={tot_calls:,}  input={tot_in:,}  output={tot_out:,}  "
            f"cached={tot_cached:,}  (input+output={tot_in + tot_out:,})"
        )
        lines.append("")
        header = f"{'role':<42}{'calls':>8}{'input':>14}{'output':>14}{'cached':>13}{'in+out':>14}"
        lines.append(header)
        lines.append("-" * len(header))
        for role in sorted(self._roles, key=lambda r: -(self._roles[r].input_tokens + self._roles[r].output_tokens)):
            t = self._roles[role]
            lines.append(
                f"{role[:42]:<42}{t.calls:>8,}{t.input_tokens:>14,}{t.output_tokens:>14,}"
                f"{t.cached_tokens:>13,}{t.input_tokens + t.output_tokens:>14,}"
            )
            # 细分（仅当该 role 下有多个 agent 时展开）
            if len(t.by_agent) > 1:
                for agent in sorted(t.by_agent, key=lambda a: -t.by_agent[a]):
                    lines.append(f"    └─ {agent[:36]:<36}{'':>8}{'':>14}{'':>14}{'':>13}{t.by_agent[agent]:>14,}")

        content = "\n".join(lines) + "\n"
        # 原子替换：先写临时再 rename，避免读到半截
        tmp = self._log_path.parent / (self._log_path.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(self._log_path)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._rewrite_human_log_locked()
            finally:
                self._jsonl.close()
