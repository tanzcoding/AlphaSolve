from __future__ import annotations

from datetime import datetime
from pathlib import Path

from alphasolve.utils.event_logger import EventLogWriter


class LogSession:
    """Manages a timestamped log directory and creates event sinks.

    Directory layout::

        {base_dir}/
          {run_id}/
            orchestrator.log
            digests/
              20260428_153045_123.log
              20260428_153102_456.log
            subagents/
              research_reviewer/
                20260428_153045_789.log
              knowledge_digest/
                20260428_153102_012.log
            workers/
              worker_{hash}.log
    """

    def __init__(
        self,
        base_dir: str = "logs",
        *,
        run_id: str | None = None,
    ) -> None:
        self.base_dir = base_dir
        self.run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        self.run_dir = Path(base_dir) / self.run_id
        self.workers_dir = self.run_dir / "workers"
        self.workers_dir.mkdir(parents=True, exist_ok=True)

    def create_orchestrator_sink(self) -> EventLogWriter:
        return EventLogWriter(
            log_path=self.run_dir / "orchestrator.log",
            scope="orchestrator",
        )

    def create_worker_sink(self, worker_id: str) -> EventLogWriter:
        return EventLogWriter(
            log_path=self.workers_dir / f"worker_{worker_id}.log",
            scope=f"worker:{worker_id}",
        )

    def create_digest_sink(self) -> EventLogWriter:
        digest_dir = self.run_dir / "digests"
        digest_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        return EventLogWriter(
            log_path=digest_dir / f"{ts}.log",
            scope="knowledge_digest",
        )

    def create_subagent_sink(self, agent_type: str) -> EventLogWriter:
        subagent_dir = self.run_dir / "subagents" / agent_type
        subagent_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        return EventLogWriter(
            log_path=subagent_dir / f"{ts}.log",
            scope=f"subagent:{agent_type}",
        )
