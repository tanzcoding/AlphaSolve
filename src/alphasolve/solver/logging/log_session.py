from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from alphasolve.solver.logging.event_log import EventLogWriter
from alphasolve.solver.logging.run_log import RunLogWriter
from alphasolve.solver.logging.search_tree_log import SearchTreeLogWriter
from alphasolve.solver.logging.token_usage_log import TokenUsageAggregator

AgentEventSink = Callable[[dict[str, Any]], None]


class LogSession:
    """Manages a timestamped log directory and creates event sinks.

    两档日志：

    - **统一运行日志**（``alphasolve_run.log``，写在项目根）：逐次 Codex 交互明细
      （token 区分输入/输出 + 可见推理摘要 + 输出）+ 定期汇总。**始终启用**，与 ``detail``
      无关，满足"默认就能看到 token 消耗"的诉求。
    - **详细 trace 日志**（``{base_dir}/{run_id}/`` 下的 orchestrator.log / worker /
      curator / subagent / search_tree，以及 token_usage.log/jsonl）：仅在
      ``detail=True``（即 ``--debug``）时启用，避免非 debug 运行产生海量落盘。

    详细模式目录布局::

        {base_dir}/
          {run_id}/
            orchestrator.log
            curator/
              20260428_153045_123.log
            subagents/
              research_reviewer/
                20260428_153045_789.log
            workers/
              worker_{hash}.log
    """

    def __init__(
        self,
        base_dir: str = "logs",
        *,
        run_id: str | None = None,
        detail: bool = True,
    ) -> None:
        self.base_dir = base_dir
        self.detail = detail
        # 统一运行日志：**始终启用**。固定写到 **项目根** 下的 alphasolve_run.log
        # （与 logs/ 目录同级），便于人工快速定位，不随 run_id 变动。base_dir 即
        # project_root/logs，故其父目录就是 project_root。
        self.run_log = RunLogWriter(Path(base_dir).parent / "alphasolve_run.log")

        # 以下为详细 trace 相关资源，仅 detail 模式创建；非 detail 时保持 None，
        # 且不创建任何 run_dir/子目录，避免污染。
        self.run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        self.run_dir = Path(base_dir) / self.run_id
        self.workers_dir = self.run_dir / "workers"
        self.token_usage: TokenUsageAggregator | None = None
        if self.detail:
            self.workers_dir.mkdir(parents=True, exist_ok=True)
            # 跨角色 token 用量聚合器（R2）：整个 run 共享一个，线程安全，动态刷新
            # run_dir/token_usage.log + token_usage.jsonl。
            self.token_usage = TokenUsageAggregator(self.run_dir)

    def token_usage_sink(self, role: str) -> AgentEventSink | None:
        """返回一个把该 role 的 ``usage`` 事件计入共享聚合器的 event sink。

        非 detail 模式下无 token 聚合器，返回 ``None``（``compose_event_sinks``
        会自动丢弃）。
        """
        if self.token_usage is None:
            return None
        return self.token_usage.sink_for(role)

    def run_log_sink(self, label: str) -> AgentEventSink:
        """返回一个把该 label（调用方角色）的 LLM 调用记入统一运行日志的 event sink。

        label 用于标识调用方（如 ``orchestrator`` / ``worker/generator`` /
        ``worker-subagent/xxx`` / ``curator``）；具体 agent 名由事件自带的
        ``agent`` 字段（config.name）细分。始终返回有效 sink。
        """
        return self.run_log.sink_for(label)

    def close_token_usage(self) -> None:
        """收尾刷新并关闭 token 聚合器（run 结束时调用）。非 detail 模式为 no-op。"""
        if self.token_usage is not None:
            self.token_usage.close()

    def close_run_log(self) -> None:
        """收尾刷新并关闭统一运行日志（整个 run 结束时调用）。"""
        self.run_log.close()

    def create_orchestrator_sink(self) -> EventLogWriter | None:
        if not self.detail:
            return None
        return EventLogWriter(
            log_path=self.run_dir / "orchestrator.log",
            scope="orchestrator",
        )

    def create_worker_sink(self, worker_id: str) -> EventLogWriter | None:
        if not self.detail:
            return None
        return EventLogWriter(
            log_path=self.workers_dir / f"worker_{worker_id}.log",
            scope=f"worker:{worker_id}",
        )

    def create_search_sink(self) -> "SearchTreeLogWriter | _SearchTreeFanout":
        """搜索树观测日志（每 selection cycle 落一次快照）。**始终启用**。

        - 项目根固定路径 ``alphasolve_search_tree.log``（+ ``.jsonl``）：与
          ``alphasolve_run.log`` 并列，默认可见，不必开 ``--debug``。
        - ``detail``（``--debug``）模式下额外再写一份到 ``run_dir/search_tree.log``，
          保持原有目录布局；此时返回一个把快照同时分发到两份的 fan-out 包装。
        """
        root_writer = SearchTreeLogWriter(
            Path(self.base_dir).parent / "alphasolve_search_tree.log"
        )
        if not self.detail:
            return root_writer
        detail_writer = SearchTreeLogWriter(self.run_dir / "search_tree.log")
        return _SearchTreeFanout([root_writer, detail_writer])

    def create_curator_sink(self) -> EventLogWriter | None:
        if not self.detail:
            return None
        curator_dir = self.run_dir / "curator"
        curator_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        return EventLogWriter(
            log_path=curator_dir / f"{ts}.log",
            scope="curator",
        )

    def create_subagent_sink(self, agent_type: str) -> EventLogWriter | None:
        if not self.detail:
            return None
        subagent_dir = self.run_dir / "subagents" / agent_type
        subagent_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        return EventLogWriter(
            log_path=subagent_dir / f"{ts}.log",
            scope=f"subagent:{agent_type}",
        )


class _SearchTreeFanout:
    """把一次搜索树快照同时分发到多个 :class:`SearchTreeLogWriter` 的轻量包装。

    仅暴露 orchestrator 用到的 ``snapshot`` / ``close`` 两个方法；对主流程无害
    （底层 writer 的 ``snapshot`` 已自行吞掉异常）。
    """

    def __init__(self, writers: list[SearchTreeLogWriter]) -> None:
        self._writers = writers

    def snapshot(self, session: Any, *, label: str = "") -> None:
        for writer in self._writers:
            writer.snapshot(session, label=label)

    def close(self) -> None:
        for writer in self._writers:
            writer.close()
