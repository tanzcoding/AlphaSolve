"""LogSession 的 detail 分档：统一运行日志始终启用，详细 trace 仅 --debug。"""
from __future__ import annotations

from pathlib import Path

from alphasolve.solver.logging.log_session import LogSession


def _emit_one_call(sink, *, agent: str = "a") -> None:
    sink({"type": "usage", "agent": agent, "turn": 1,
          "input_tokens": 100, "output_tokens": 10, "cached_tokens": 0, "elapsed": 1.0})
    sink({"type": "thinking", "turn": 1, "content": "思考中"})
    sink({"type": "assistant_message", "turn": 1, "agent": agent, "content": "答案"})


def test_non_detail_only_produces_run_log(tmp_path: Path):
    """detail=False：产出项目根的 alphasolve_run.log 与 search_tree 日志，不建 run_dir / token_usage。"""
    logs_dir = tmp_path / "logs"
    session = LogSession(base_dir=str(logs_dir), detail=False)

    # 统一运行日志始终可用
    run_sink = session.run_log_sink("worker/generator")
    _emit_one_call(run_sink)
    # token 聚合与详细 trace sink（orchestrator/worker/curator/subagent）在非 detail 下应缺席
    assert session.token_usage is None
    assert session.token_usage_sink("worker") is None
    assert session.create_orchestrator_sink() is None
    assert session.create_worker_sink("w1") is None
    assert session.create_curator_sink() is None
    assert session.create_subagent_sink("x") is None
    # 但搜索树日志始终启用（写项目根固定路径）
    search = session.create_search_sink()
    assert search is not None
    search.close()

    session.close_token_usage()  # 应为 no-op，不抛错
    session.close_run_log()

    run_log = tmp_path / "alphasolve_run.log"
    assert run_log.exists()
    text = run_log.read_text(encoding="utf-8")
    assert "worker/generator" in text
    assert "in=100" in text and "out=10" in text
    assert "思考中" in text
    # 搜索树日志落在项目根固定路径
    assert (tmp_path / "alphasolve_search_tree.log").exists()
    # 非 detail 不应创建 run_dir 及其下的 token_usage.log / search_tree.log
    assert not (logs_dir / session.run_id).exists()


def test_detail_produces_trace_sinks(tmp_path: Path):
    """detail=True：run_dir、token 聚合与各 trace sink 均可用；搜索树同时写两份。"""
    logs_dir = tmp_path / "logs"
    session = LogSession(base_dir=str(logs_dir), detail=True)
    try:
        assert session.token_usage is not None
        assert session.token_usage_sink("worker") is not None
        orch = session.create_orchestrator_sink()
        assert orch is not None
        orch.close()
        search = session.create_search_sink()
        search.close()
        assert (logs_dir / session.run_id).exists()
        # detail 模式下项目根与 run_dir 各一份
        assert (tmp_path / "alphasolve_search_tree.log").exists()
        assert (logs_dir / session.run_id / "search_tree.log").exists()
    finally:
        session.close_token_usage()
        session.close_run_log()

    # 统一运行日志同样存在
    assert (tmp_path / "alphasolve_run.log").exists()


def test_run_log_survives_multiple_orchestrator_scopes(tmp_path: Path):
    """模拟多次 orchestrator restart 共享同一 session：run_log 到 run 结束才关闭。"""
    session = LogSession(base_dir=str(tmp_path / "logs"), detail=False)
    # 第一段（如同第一次 orchestrator.run）
    _emit_one_call(session.run_log_sink("orchestrator"), agent="mgr1")
    # 期间只关 token（detail=False 为 no-op），不关 run_log
    session.close_token_usage()
    # 第二段（restart 后）仍应能写入
    _emit_one_call(session.run_log_sink("orchestrator"), agent="mgr2")
    session.close_run_log()

    text = (tmp_path / "alphasolve_run.log").read_text(encoding="utf-8")
    assert "agent=mgr1" in text
    assert "agent=mgr2" in text
