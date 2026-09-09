"""工具日志保留可解析的结果行，并分别记录系统时间和单调耗时。"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from alphasolve.solver.logging import event_log


@pytest.mark.parametrize("is_error, tag", [(False, "result"), (True, "error")])
def test_tool_timestamps_survive_wall_clock_rollback(monkeypatch, tmp_path, is_error, tag):
    started = datetime(2026, 9, 9, 3, 0, 0, 123456, tzinfo=timezone.utc)
    finished = datetime(2026, 9, 9, 2, 59, 0, 987654, tzinfo=timezone.utc)
    wall_times = iter([started, finished])
    monotonic_times = iter([10.0, 11.5])

    class ClockDatetime(datetime):
        @classmethod
        def now(cls):
            return next(wall_times)

    monkeypatch.setattr(event_log, "datetime", ClockDatetime)
    monkeypatch.setattr(event_log, "time", SimpleNamespace(monotonic=lambda: next(monotonic_times)))
    call = {"type": "tool_call", "name": "RunPython", "tool_call_id": "python-1",
            "arguments": {"code": "print(4)"}}
    result = {"type": "tool_result", "name": "RunPython", "tool_call_id": "python-1",
              "content": "4", "is_error": is_error}
    path = tmp_path / "agent.log"
    with event_log.EventLogWriter(path) as log:
        log(call)
        # 开始时间须立即落盘，执行未返回时也能定位等待从何时开始。
        assert path.read_text(encoding="utf-8") == (
            "  [tool] RunPython\n"
            f"    started_at: {started.astimezone().isoformat(timespec='milliseconds')}\n"
            '    args: {"code": "print(4)"}\n'
        )
        log(result)

    assert path.read_text(encoding="utf-8") == (
        "  [tool] RunPython\n"
        f"    started_at: {started.astimezone().isoformat(timespec='milliseconds')}\n"
        '    args: {"code": "print(4)"}\n'
        f"    finished_at: {finished.astimezone().isoformat(timespec='milliseconds')}\n"
        f"    {tag} (1.5s, 1 bytes): 4\n\n"
    )
    # 记录时间只影响日志，原始事件及供其他消费者读取的内容保持原样。
    assert "started_at" not in call
    assert "finished_at" not in result
    assert result["content"] == "4"


def test_overlapping_tool_calls_keep_independent_elapsed_times(monkeypatch, tmp_path):
    monotonic_times = iter([10.0, 11.0, 13.0, 14.0])
    monkeypatch.setattr(event_log, "time", SimpleNamespace(monotonic=lambda: next(monotonic_times)))
    path = tmp_path / "agent.log"
    with event_log.EventLogWriter(path) as log:
        for tool_id in ("first", "second"):
            log({"type": "tool_call", "name": "RunPython", "tool_call_id": tool_id})
        for tool_id in ("second", "first"):
            log({"type": "tool_result", "name": "RunPython", "tool_call_id": tool_id,
                 "content": tool_id})
    text = path.read_text(encoding="utf-8")
    assert "    result (2.0s, 6 bytes): second\n" in text
    assert "    result (4.0s, 5 bytes): first\n" in text


def test_unmatched_result_has_finish_time_without_fabricated_elapsed(tmp_path):
    path = tmp_path / "agent.log"
    with event_log.EventLogWriter(path) as log:
        log({"type": "tool_result", "name": "RunPython", "content": "4"})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("    finished_at: ")
    timestamp = lines[0].removeprefix("    finished_at: ")
    assert datetime.fromisoformat(timestamp).utcoffset() is not None
    assert lines[1] == "    result (1 bytes): 4"
