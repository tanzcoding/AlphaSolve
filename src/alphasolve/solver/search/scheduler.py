"""调度层的最小持久化基础设施：selection cycle 计数 + attempt DAG 崩溃恢复日志。

不含任何排序/打分/自动选择逻辑——exploit 哪个方向、要不要自由探索完全由
orchestrator 的 LLM 自主判断（见 prompts/orchestrator.md）。本模块只负责把
`cycle` 计数和 append-only 的 attempt 事件落盘，供进程重启后恢复。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class SchedulerStateStore:
    """持久化 selection cycle 计数，并追加记录 attempt DAG 事件。"""

    SCHEMA_VERSION = 1

    def __init__(self, state_path: Path | None = None, attempt_log_path: Path | None = None) -> None:
        self.state_path = state_path
        self.attempt_log_path = attempt_log_path
        self.cycle = 0
        self._event_seq = 0
        self._load()

    def append_attempt_event(self, event: dict[str, Any]) -> None:
        self._event_seq += 1
        payload = {"seq": self._event_seq, **event}
        if self.attempt_log_path is not None:
            self.attempt_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.attempt_log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
        self.save()

    def load_attempt_events(self) -> list[dict[str, Any]]:
        if self.attempt_log_path is None or not self.attempt_log_path.is_file():
            return []
        events: list[dict[str, Any]] = []
        try:
            for line in self.attempt_log_path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    events.append(item)
        except OSError:
            return []
        return sorted(events, key=lambda item: int(item.get("seq", 0)))

    def save(self) -> None:
        if self.state_path is None:
            return
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "cycle": self.cycle,
            "last_event_seq": self._event_seq,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=self.state_path.name + ".", suffix=".tmp", dir=self.state_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
            Path(temp_name).replace(self.state_path)
        finally:
            temp = Path(temp_name)
            if temp.exists():
                temp.unlink()

    def _load(self) -> None:
        if self.state_path is None or not self.state_path.is_file():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        self.cycle = max(0, int(raw.get("cycle", 0)))
        self._event_seq = max(0, int(raw.get("last_event_seq", 0)))
