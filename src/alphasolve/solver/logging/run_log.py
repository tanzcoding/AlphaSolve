"""统一运行日志：逐次 Codex 交互明细 + 定期 token 汇总（写到 alphasolve_run.log）。

与 :mod:`token_usage_log` 的区别与分工：

- ``token_usage_log`` 只做**汇总表**（按 role 分组累加），不含 可见推理摘要 与输出正文；
- 本模块面向"想看清每一次 LLM 调用花了多少 token、模型想了什么（可见推理摘要）、
  最终输出了什么"的诊断诉求，逐次调用落一条明细，并由后台线程**定期**把
  跨 agent/subagent 的 token 汇总追加到同一份日志里。

日志对象是整个 run 共享的一份 ``alphasolve_run.log``。每个 agent（orchestrator /
worker 各角色 / subagent / curator）在创建时通过 :meth:`RunLogWriter.sink_for`
领到一个独立的 event sink 闭包：

- 闭包内部按 ``turn`` 维护"半成品调用记录"（pending）。因为单个 Agent 实例的
  ``run()`` 是顺序执行的，同一时刻同一 turn 只会有一条 pending，闭包天然隔离，
  无需加锁即可关联同一轮的 ``usage`` / ``thinking`` / ``assistant_message``。
- 只有"写文件"和"累加全局统计"需要跨线程同步（worker 在多线程里跑），由
  :class:`RunLogWriter` 内部一把锁保护。

Codex 可能先发出正文，再报告用量；同一轮的用量和正文到齐后才落盘。
这里一轮对应一次 ``Agent.run``，包含 Codex 内部完成任务所需的模型与工具循环。
若某轮因异常或中断缺少其中一项，则在代理收尾事件
（``run_finish`` / ``run_error`` / ``run_stopped``）时写入已经收到的内容。

本模块只做"观测/记录"，不改变任何 agent 的执行语义。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

AgentEventSink = Callable[[dict[str, Any]], None]

# 可见推理摘要 与输出正文的单条截断上限（字符）。取较大值以尽量保留完整 可见推理摘要，
# 同时避免个别超长响应把日志撑爆。
_TRUNCATE_REASONING_CHARS = 16_000
_TRUNCATE_OUTPUT_CHARS = 12_000

# 后台汇总线程的默认刷新周期（秒）。
_DEFAULT_FLUSH_INTERVAL = 30.0


@dataclass
class _PendingCall:
    """单轮 LLM 调用在落盘前逐事件累积的半成品记录。"""

    agent: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    elapsed: float = 0.0
    reasoning: str = ""
    output: str = ""
    tool_call_count: int = 0
    usage_seen: bool = False
    assistant_seen: bool = False


@dataclass
class _Tally:
    """某个分组（label 或 label 下的具体 agent）的累计用量。"""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

    def add(self, *, inp: int, out: int, cached: int) -> None:
        self.calls += 1
        self.input_tokens += inp
        self.output_tokens += out
        self.cached_tokens += cached


@dataclass
class _GroupTally:
    """一个顶层 label（如 worker/generator、orchestrator、curator）的累计。"""

    total: _Tally = field(default_factory=_Tally)
    # label 下再按具体 agent 名（config.name）细分。
    by_agent: dict[str, _Tally] = field(default_factory=dict)

    def add(self, *, agent: str, inp: int, out: int, cached: int) -> None:
        self.total.add(inp=inp, out=out, cached=cached)
        if agent:
            self.by_agent.setdefault(agent, _Tally()).add(inp=inp, out=out, cached=cached)


class RunLogWriter:
    """整个 run 共享、线程安全的运行日志写入器。

    职责：
    - 通过 :meth:`sink_for` 为每个 agent 发一个 event sink，逐次 Codex 交互落一条
      明细（含输入/输出/缓存 token、可见推理摘要、输出正文）；
    - 后台线程按 ``flush_interval`` 秒周期，把跨 agent/subagent 的 token 汇总
      追加写入同一份日志。
    """

    def __init__(
        self,
        log_path: Path,
        *,
        flush_interval: float = _DEFAULT_FLUSH_INTERVAL,
    ) -> None:
        self._log_path = Path(log_path)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._flush_interval = max(1.0, float(flush_interval))
        self._lock = threading.Lock()
        self._groups: dict[str, _GroupTally] = {}
        self._started = datetime.now()
        # "w" 覆盖：每个 run 一份新的运行日志，避免跨 run 追加混淆。
        self._log = open(self._log_path, "w", encoding="utf-8")
        self._closed = False
        self._write_header_locked()
        # 后台定期汇总线程：daemon，确保不阻塞进程退出（含 Ctrl+C）。
        self._stop = threading.Event()
        self._timer = threading.Thread(
            target=self._periodic_loop,
            name="alphasolve-run-log",
            daemon=True,
        )
        self._timer.start()

    # ---- 事件入口 -------------------------------------------------------

    def sink_for(self, label: str) -> AgentEventSink:
        """返回某个调用方（label，如 ``worker/generator``）的 event sink。

        闭包内部按 turn 维护 pending，关联同一轮的 usage / thinking /
        assistant_message，并在该轮结束时把明细落盘。
        """
        pending: dict[int, _PendingCall] = {}

        def _sink(event: dict[str, Any]) -> None:
            etype = event.get("type") or ""
            if etype == "usage":
                turn = int(event.get("turn") or 0)
                call = pending.setdefault(turn, _PendingCall())
                call.agent = str(event.get("agent") or "")
                call.input_tokens = int(event.get("input_tokens") or 0)
                call.output_tokens = int(event.get("output_tokens") or 0)
                call.cached_tokens = int(event.get("cached_tokens") or 0)
                call.elapsed = float(event.get("elapsed") or 0.0)
                call.usage_seen = True
                if call.assistant_seen:
                    pending.pop(turn)
                    self._record_call(label=label, turn=turn, call=call)
            elif etype == "thinking":
                turn = int(event.get("turn") or 0)
                call = pending.setdefault(turn, _PendingCall())
                content = str(event.get("content") or "")
                call.reasoning = "\n".join(part for part in (call.reasoning, content) if part)
            elif etype == "assistant_message":
                turn = int(event.get("turn") or 0)
                call = pending.setdefault(turn, _PendingCall(agent=str(event.get("agent") or "")))
                content = str(event.get("content") or "")
                # 同一次原生 turn 可以在工具前后完成多条消息，应按顺序保留每一项。
                call.output = "\n\n".join(part for part in (call.output, content) if part)
                call.tool_call_count = max(call.tool_call_count, int(event.get("tool_call_count") or 0))
                call.assistant_seen = True
                if call.usage_seen:
                    pending.pop(turn)
                    self._record_call(label=label, turn=turn, call=call)
            elif etype == "tool_call":
                turn = int(event.get("turn") or 0)
                call = pending.setdefault(turn, _PendingCall(agent=str(event.get("agent") or "")))
                call.tool_call_count += 1
            elif etype in ("run_finish", "run_error", "run_stopped"):
                # 兜底：把该 agent 未随 assistant_message 落盘的残留 pending 冲刷掉。
                for turn in sorted(pending):
                    leftover = pending[turn]
                    if leftover.usage_seen or leftover.reasoning or leftover.assistant_seen:
                        self._record_call(label=label, turn=turn, call=leftover)
                pending.clear()

        return _sink

    def _record_call(self, *, label: str, turn: int, call: _PendingCall) -> None:
        with self._lock:
            if self._closed:
                return
            group = self._groups.setdefault(label, _GroupTally())
            group.add(
                agent=call.agent,
                inp=call.input_tokens,
                out=call.output_tokens,
                cached=call.cached_tokens,
            )
            self._write_call_locked(label=label, turn=turn, call=call)

    # ---- 明细落盘 -------------------------------------------------------

    def _write_call_locked(self, *, label: str, turn: int, call: _PendingCall) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        agent = call.agent or "?"
        elapsed = f"{call.elapsed:.1f}s" if call.elapsed else "-"
        header = (
            f"[{ts}] AGENT TURN │ {label} · agent={agent} · turn={turn} │ "
            f"in={call.input_tokens:,} out={call.output_tokens:,} "
            f"cached={call.cached_tokens:,} · {elapsed}"
        )
        self._log.write(header + "\n")
        reasoning = _truncate(call.reasoning, _TRUNCATE_REASONING_CHARS)
        if reasoning.strip():
            self._log.write("  ├─ REASONING (visible summary):\n")
            self._write_indented(reasoning, prefix="  │  ")
        out = _truncate(call.output, _TRUNCATE_OUTPUT_CHARS)
        if out.strip():
            self._log.write("  └─ OUTPUT (content):\n")
            self._write_indented(out, prefix="     ")
        elif call.tool_call_count:
            self._log.write(f"  └─ OUTPUT: <{call.tool_call_count} tool call(s), no text>\n")
        self._log.write("\n")
        self._log.flush()

    def _write_indented(self, text: str, *, prefix: str) -> None:
        for line in text.splitlines() or [""]:
            self._log.write(f"{prefix}{line}\n")

    # ---- 自由文本注记 --------------------------------------------------

    def note(self, message: str) -> None:
        """写入一条带时间戳的自由文本注记，用于记录编排决策等非 LLM 调用事件。

        线程安全；run 结束（``close``）后为 no-op。与 LLM 调用明细并列写入同一份
        ``alphasolve_run.log``，便于人工在运行日志里直接看到决策轨迹。
        """
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            if self._closed:
                return
            self._log.write(f"[{ts}] NOTE │ {message}\n\n")
            self._log.flush()

    # ---- 定期汇总 -------------------------------------------------------

    def _periodic_loop(self) -> None:
        while not self._stop.wait(self._flush_interval):
            with self._lock:
                if self._closed:
                    return
                self._write_summary_locked(final=False)

    def _write_summary_locked(self, *, final: bool) -> None:
        now = datetime.now()
        elapsed = now - self._started
        title = "FINAL TOKEN USAGE SUMMARY" if final else "TOKEN USAGE SUMMARY"
        lines: list[str] = []
        lines.append("─" * 92)
        lines.append(
            f"{title} @ {now.strftime('%Y-%m-%d %H:%M:%S')} "
            f"(run elapsed {_format_duration(elapsed.total_seconds())})"
        )

        tot = _Tally()
        for group in self._groups.values():
            tot.calls += group.total.calls
            tot.input_tokens += group.total.input_tokens
            tot.output_tokens += group.total.output_tokens
            tot.cached_tokens += group.total.cached_tokens
        lines.append(
            f"TOTAL: calls={tot.calls:,}  input={tot.input_tokens:,}  "
            f"output={tot.output_tokens:,}  cached={tot.cached_tokens:,}  "
            f"(input+output={tot.input_tokens + tot.output_tokens:,})"
        )
        header = (
            f"{'group / agent':<44}{'calls':>7}{'input':>13}{'output':>13}"
            f"{'cached':>12}{'in+out':>13}"
        )
        lines.append(header)
        lines.append("-" * len(header))
        for label in sorted(
            self._groups,
            key=lambda g: -(self._groups[g].total.input_tokens + self._groups[g].total.output_tokens),
        ):
            group = self._groups[label]
            t = group.total
            lines.append(
                f"{label[:44]:<44}{t.calls:>7,}{t.input_tokens:>13,}"
                f"{t.output_tokens:>13,}{t.cached_tokens:>12,}"
                f"{t.input_tokens + t.output_tokens:>13,}"
            )
            # 仅当该 label 下有多个具体 agent 时展开细分。
            if len(group.by_agent) > 1:
                for agent in sorted(
                    group.by_agent,
                    key=lambda a: -(group.by_agent[a].input_tokens + group.by_agent[a].output_tokens),
                ):
                    at = group.by_agent[agent]
                    lines.append(
                        f"    └─ {agent[:38]:<38}{at.calls:>7,}{at.input_tokens:>13,}"
                        f"{at.output_tokens:>13,}{at.cached_tokens:>12,}"
                        f"{at.input_tokens + at.output_tokens:>13,}"
                    )
        lines.append("─" * 92)
        self._log.write("\n".join(lines) + "\n\n")
        self._log.flush()

    # ---- 生命周期 -------------------------------------------------------

    def _write_header_locked(self) -> None:
        ts = self._started.strftime("%Y-%m-%d %H:%M:%S")
        self._log.write("=" * 92 + "\n")
        self._log.write(f"AlphaSolve 运行日志（逐次 Codex 交互明细 + 定期 token 汇总）\n")
        self._log.write(f"启动时间: {ts}\n")
        self._log.write("=" * 92 + "\n\n")
        self._log.flush()

    def close(self) -> None:
        """停止后台线程、写最终汇总并关闭文件（run 结束时调用）。"""
        self._stop.set()
        # 后台线程是 daemon，join 加超时避免收尾时被拖住。
        if self._timer.is_alive():
            self._timer.join(timeout=2.0)
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._write_summary_locked(final=True)
            finally:
                self._log.close()


# -- helpers ------------------------------------------------------------------

def _truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} more chars]"


def _format_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"
