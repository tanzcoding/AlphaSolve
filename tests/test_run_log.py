"""RunLogWriter：逐次 Codex 交互明细（token + 可见推理摘要 + 输出）与定期汇总。"""
from __future__ import annotations

import threading
from pathlib import Path

from alphasolve.solver.logging.run_log import RunLogWriter


def _usage(turn: int, *, agent: str, inp: int, out: int, cached: int = 0, elapsed: float = 1.0) -> dict:
    return {
        "type": "usage",
        "agent": agent,
        "turn": turn,
        "input_tokens": inp,
        "output_tokens": out,
        "cached_tokens": cached,
        "elapsed": elapsed,
    }


def _thinking(turn: int, content: str) -> dict:
    return {"type": "thinking", "turn": turn, "content": content}


def _assistant(turn: int, *, agent: str, content: str, tool_call_count: int = 0) -> dict:
    return {
        "type": "assistant_message",
        "turn": turn,
        "agent": agent,
        "content": content,
        "tool_call_count": tool_call_count,
    }


def test_records_call_detail_with_cot_and_tokens(tmp_path: Path):
    log_path = tmp_path / "alphasolve_run.log"
    writer = RunLogWriter(log_path, flush_interval=3600)  # 关掉周期线程干扰
    sink = writer.sink_for("worker/generator")

    # 恒定发射顺序：usage → thinking → assistant_message
    sink(_usage(1, agent="proposer", inp=1200, out=340, cached=800, elapsed=12.3))
    sink(_thinking(1, "先考虑归纳法，再验证边界情形。"))
    sink(_assistant(1, agent="proposer", content="命题成立，证明如下……"))
    writer.close()

    text = log_path.read_text(encoding="utf-8")
    # 明细头：label + agent + turn + 区分输入/输出/缓存 token
    assert "worker/generator" in text
    assert "agent=proposer" in text
    assert "in=1,200" in text
    assert "out=340" in text
    assert "cached=800" in text
    # 可见推理摘要 与输出正文均落盘
    assert "REASONING (visible summary)" in text
    assert "先考虑归纳法" in text
    assert "OUTPUT (content)" in text
    assert "命题成立" in text


def test_final_summary_aggregates_by_label_and_agent(tmp_path: Path):
    log_path = tmp_path / "run.log"
    writer = RunLogWriter(log_path, flush_interval=3600)
    gen = writer.sink_for("worker/generator")
    ver = writer.sink_for("worker/verifier")

    gen(_usage(1, agent="proposer", inp=100, out=10))
    gen(_assistant(1, agent="proposer", content="a"))
    ver(_usage(1, agent="checker", inp=200, out=20))
    ver(_assistant(1, agent="checker", content="b"))
    writer.close()

    text = log_path.read_text(encoding="utf-8")
    assert "FINAL TOKEN USAGE SUMMARY" in text
    # 总计 = 300 输入 + 30 输出
    assert "input=300" in text
    assert "output=30" in text
    assert "worker/generator" in text
    assert "worker/verifier" in text


def test_flush_leftover_pending_on_run_finish(tmp_path: Path):
    """某轮只有 usage / thinking 而无 assistant_message 时，收尾事件应兜底 flush。"""
    log_path = tmp_path / "run.log"
    writer = RunLogWriter(log_path, flush_interval=3600)
    sink = writer.sink_for("orchestrator")

    sink(_usage(1, agent="manager", inp=50, out=5))
    sink(_thinking(1, "残留 可见推理摘要，没有 assistant_message"))
    sink({"type": "run_finish", "turn": 1, "final_answer": "x"})
    writer.close()

    text = log_path.read_text(encoding="utf-8")
    assert "残留 可见推理摘要" in text
    assert "in=50" in text


def test_thread_safe_concurrent_sinks(tmp_path: Path):
    log_path = tmp_path / "run.log"
    writer = RunLogWriter(log_path, flush_interval=3600)

    def worker(idx: int) -> None:
        sink = writer.sink_for(f"worker/w{idx}")
        for turn in range(1, 21):
            sink(_usage(turn, agent=f"agent{idx}", inp=10, out=1))
            sink(_assistant(turn, agent=f"agent{idx}", content=f"out-{idx}-{turn}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    writer.close()

    text = log_path.read_text(encoding="utf-8")
    # 5 个线程 × 20 轮 = 100 次调用，总输入 100×10=1000，输出 100×1=100
    assert "input=1,000" in text
    assert "output=100" in text
    # 每个线程的明细都应完整落盘（抽查最后一条）
    for i in range(5):
        assert f"out-{i}-20" in text


def test_close_is_idempotent(tmp_path: Path):
    writer = RunLogWriter(tmp_path / "run.log", flush_interval=3600)
    writer.close()
    writer.close()  # 二次 close 不应抛错


def test_note_writes_free_text_line(tmp_path: Path):
    log_path = tmp_path / "run.log"
    writer = RunLogWriter(log_path, flush_interval=3600)
    writer.note("dispatch preflight decision: leaf is actionable")
    writer.close()

    text = log_path.read_text(encoding="utf-8")
    assert "NOTE │" in text
    assert "dispatch preflight decision: leaf is actionable" in text


def test_note_is_noop_after_close(tmp_path: Path):
    log_path = tmp_path / "run.log"
    writer = RunLogWriter(log_path, flush_interval=3600)
    writer.close()
    writer.note("should not appear")  # close 后为 no-op，且不抛错

    text = log_path.read_text(encoding="utf-8")
    assert "should not appear" not in text


def test_usage_can_arrive_after_assistant_output(tmp_path: Path):
    """Codex 晚到的用量应归入同一条记录，不能重复累计调用。"""
    path = tmp_path / "run.log"
    writer = RunLogWriter(path, flush_interval=3600)
    sink = writer.sink_for("orchestrator")
    sink(_thinking(1, "检查已验证结论。"))
    sink(_assistant(1, agent="manager", content="已完成本轮规划。"))
    sink(_usage(1, agent="manager", inp=120, out=30, cached=20))
    sink({"type": "run_finish", "turn": 1})
    writer.close()

    text = path.read_text(encoding="utf-8")
    assert text.count("AGENT TURN │") == 1
    assert "已完成本轮规划。" in text
    assert "检查已验证结论。" in text
    assert "TOTAL: calls=1" in text
    assert "input=120" in text
    assert "output=30" in text


def test_interrupted_output_without_usage_is_preserved(tmp_path: Path):
    """中断时即便服务尚未报告用量，也保留已收到的正文。"""
    path = tmp_path / "run.log"
    writer = RunLogWriter(path, flush_interval=3600)
    sink = writer.sink_for("worker")
    sink(_assistant(1, agent="generator", content="已经写入部分推导。"))
    sink({"type": "run_stopped", "turn": 1})
    writer.close()

    assert "已经写入部分推导。" in path.read_text(encoding="utf-8")


def test_tool_only_native_turn_retains_tool_count(tmp_path: Path):
    """原生 turn 没有正文时，直接从工具事件保留调用次数。"""
    path = tmp_path / "run.log"
    writer = RunLogWriter(path, flush_interval=3600)
    sink = writer.sink_for("worker")
    sink({"type": "tool_call", "agent": "generator", "turn": 1, "name": "Read", "tool_call_id": "r"})
    sink({"type": "tool_call", "agent": "generator", "turn": 1, "name": "Finish", "tool_call_id": "f"})
    sink(_usage(1, agent="generator", inp=80, out=10))
    sink({"type": "run_finish", "turn": 1, "reason": "tool_requested_stop"})
    writer.close()

    text = path.read_text(encoding="utf-8")
    assert text.count("AGENT TURN │") == 1
    assert "<2 tool call(s), no text>" in text
