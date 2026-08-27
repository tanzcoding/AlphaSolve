"""短程任务交付审计测试。

它与长程进度审计的分工必须保持清晰：
- 短程只问"我分派的任务被完成了吗"，同步执行，结论随同一次 TaskOutput 返回；
- 长程只问"整体是否在推进原题"，异步执行，不看 rubric。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from alphasolve.solver.project import ProjectLayout
from alphasolve.solver.task_audit import TaskAuditor, summarize_task_audits


_AUDIT_TEXT = """### Task Delivery
DELIVERY: off_target

### Rubric Check
- [pass] Statement covers every integrable H — the normalization is stated in full.
- [fail] Bound is exactly 2 — the Statement proves only an unspecified constant C.
- [unclear] No extra hypothesis is introduced — the Statement's regularity clause is ambiguous.

### Assigned Versus Delivered
The assigned obligation was the sharp bound 2; the proven Statement gives a constant C. The difference is an unquantified constant.

### Residual Obligation
Establish the explicit constant 2 rather than an unspecified C.

### Cited Evidence
- `verified_propositions/bound.md`

```text
RUBRIC_SCORE: 1/3
SCOPE_DRIFT: The bound was weakened from the explicit 2 to an unspecified constant C.
```
"""


def _layout(tmp_path: Path) -> ProjectLayout:
    (tmp_path / "problem.md").write_text("# Problem\n\nProve the sharp bound.\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    return layout


def _auditor(layout: ProjectLayout, runner) -> TaskAuditor:
    return TaskAuditor(
        layout=layout,
        suite=SimpleNamespace(agents={"task_auditor": object()}),
        client_factory=lambda _config: None,
        audit_runner=runner,
    )


def _payload(layout: ProjectLayout, **overrides):
    verified = layout.verified_dir / "bound.md"
    verified.write_text(
        "## Statement\n\nThere exists a constant C with the stated bound.\n\n## Proof\n\nOmitted.\n",
        encoding="utf-8",
    )
    payload = {
        "worker_id": "worker-a",
        "status": "verified",
        "summary": "Proved a bound with an unspecified constant.",
        "method_id": "direct_proof",
        "difficulty_id": "sharp-bound",
        "worker_hint": "Prove the sharp bound 2.",
        "rubric": (
            "- Statement covers every integrable H with the stated normalization\n"
            "- Bound is exactly 2, not an unspecified constant\n"
            "- No extra smoothness hypothesis is introduced"
        ),
        "verified_file": str(verified),
    }
    payload.update(overrides)
    return payload


def test_task_audit_detects_scope_drift_on_a_verified_worker(tmp_path):
    """verified 不等于交付：证明了正确但更弱的结论必须被判为 off_target。"""
    layout = _layout(tmp_path)
    prompts: list[str] = []

    def runner(prompt: str) -> str:
        prompts.append(prompt)
        return _AUDIT_TEXT

    result = _auditor(layout, runner).audit(_payload(layout))

    assert result is not None
    assert result["delivery"] == "off_target"
    assert result["delivered"] is False
    assert result["rubric_score"] == "1/3"
    assert result["rubric_passed"] == 1 and result["rubric_total"] == 3
    assert "weakened" in result["scope_drift"]
    assert result["residual_obligation"].startswith("Establish the explicit constant 2")
    assert [item["verdict"] for item in result["rubric_checks"]] == ["pass", "fail", "unclear"]

    # 审计提示必须同时含"要求什么"与"交付了什么"，否则无法比对。
    prompt = prompts[0]
    assert "Prove the sharp bound 2." in prompt
    assert "Bound is exactly 2, not an unspecified constant" in prompt
    assert "There exists a constant C" in prompt
    assert "A `verified` status does not by itself mean" in prompt

    report = layout.workspace_dir / result["audit_path"]
    assert report.is_file()
    assert "Delivery: `off_target`" in report.read_text(encoding="utf-8")
    decision = json.loads((layout.task_audits_dir / "worker-a.json").read_text(encoding="utf-8"))
    assert decision["delivery"] == "off_target"
    assert decision["rubric_passed"] == 1


def test_task_audit_accepts_a_delivered_worker(tmp_path):
    layout = _layout(tmp_path)
    audit_text = (
        "### Task Delivery\nDELIVERY: delivered\n\n"
        "### Rubric Check\n- [pass] Bound is exactly 2 — the Statement proves the constant 2.\n\n"
        "### Residual Obligation\nnone\n\n"
        "```text\nRUBRIC_SCORE: 1/1\nSCOPE_DRIFT: NONE\n```\n"
    )

    result = _auditor(layout, lambda _prompt: audit_text).audit(_payload(layout))

    assert result["delivery"] == "delivered"
    assert result["delivered"] is True
    assert result["scope_drift"] == ""


def test_non_mathematical_failure_is_settled_without_calling_the_auditor(tmp_path):
    """协议错误/执行异常没有可验收的陈述，不应为此花一次 LLM 调用。"""
    layout = _layout(tmp_path)
    calls: list[str] = []

    result = _auditor(layout, lambda prompt: calls.append(prompt) or "").audit(
        _payload(
            layout,
            status="rejected",
            failure_kind="generator_protocol_failure",
            blocking_obligation="Generator completed without a valid proposition.md output.",
            verified_file="",
        )
    )

    assert calls == []
    assert result["delivery"] == "not_delivered"
    assert result["rubric_total"] == 3
    assert result["rubric_passed"] == 0
    # 全 0 分时每条 rubric_check 都是同一句"没有可验收的 Statement"，对编排决策没有
    # 信息量，因此不进 orchestrator payload；完整逐条核对仍保留在落盘报告里。
    assert "rubric_checks" not in result
    assert "next_action" not in result
    assert "no Statement exists" in result["assigned_versus_delivered"]


def test_cancelled_worker_is_not_audited(tmp_path):
    layout = _layout(tmp_path)
    calls: list[str] = []

    result = _auditor(layout, lambda prompt: calls.append(prompt) or "").audit(
        _payload(layout, status="cancelled")
    )

    assert result is None
    assert calls == []


def test_auditor_failure_degrades_instead_of_raising(tmp_path):
    layout = _layout(tmp_path)

    def boom(_prompt: str) -> str:
        raise RuntimeError("auditor unavailable")

    result = _auditor(layout, boom).audit(_payload(layout))

    assert result["status"] == "failed"
    assert result["delivery"] == "unknown"
    report = layout.workspace_dir / result["audit_path"]
    assert "auditor unavailable" in report.read_text(encoding="utf-8")


def test_invalid_audit_without_delivery_line_is_marked(tmp_path):
    layout = _layout(tmp_path)

    result = _auditor(layout, lambda _prompt: "The worker did fine, I think.").audit(_payload(layout))

    assert result["status"] == "invalid_audit"
    assert result["delivery"] == "unknown"


def test_summary_flags_open_obligations_and_praises_full_delivery():
    undelivered = summarize_task_audits([
        {"worker_id": "w1", "delivery": "delivered"},
        {"worker_id": "w2", "delivery": "off_target", "scope_drift": "Bound weakened."},
        {"worker_id": "w3", "delivery": "not_delivered"},
    ])

    assert undelivered["audited"] == 3
    assert undelivered["delivered"] == 1
    assert undelivered["undelivered_worker_ids"] == ["w2", "w3"]
    assert undelivered["scope_drift_worker_ids"] == ["w2"]
    assert "the residual obligation is still open" in undelivered["instruction"]

    clean = summarize_task_audits([{"worker_id": "w1", "delivery": "delivered"}])
    assert clean["undelivered_worker_ids"] == []
    assert "Every audited worker delivered" in clean["instruction"]

    assert summarize_task_audits([]) is None
    assert summarize_task_audits([{"worker_id": "w1"}]) is None


def test_task_audit_runs_synchronously_inside_worker_collection(tmp_path):
    """短程结论必须出现在报告该 worker 的那一次 TaskOutput 上。"""
    from alphasolve.solver.orchestrator import WorkerManager
    from alphasolve.solver.worker import WorkerRunResult

    layout = _layout(tmp_path)
    audited: list[str] = []

    class Auditor:
        def audit(self, payload):
            audited.append(str(payload.get("worker_id")))
            return {"worker_id": payload.get("worker_id"), "delivery": "partial", "delivered": False}

    manager = object.__new__(WorkerManager)
    manager.layout = layout
    manager.active = {}
    manager.active_info = {}
    manager.active_info_lock = __import__("threading").Lock()
    manager.results = []
    manager.completed_backlog = []
    manager._task_output_audit_checkpoints = []
    manager.renderer = None
    manager.curator_queue = None
    manager.progress_audit_queue = None
    manager.task_auditor = Auditor()
    manager.attempt_observer = None
    manager.orchestrator_session_id = "ralph-1"
    manager.completion_handler = None
    manager.solved_result = None
    manager.solution_path = None
    manager.stop_event = __import__("threading").Event()

    class Future:
        def result(self):
            return WorkerRunResult(
                worker_id="worker-a",
                worker_dir=layout.unverified_dir / "prop-worker-a",
                status="rejected",
                summary="No bridge.",
                rubric="- The Statement proves the bridge.",
                worker_hint="Prove the bridge.",
            )

    future = Future()
    manager.active[future] = "worker-a"

    completed = manager._consume_done([future])

    assert audited == ["worker-a"]
    assert completed[0]["task_audit"]["delivery"] == "partial"
