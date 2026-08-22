"""Short-horizon task delivery audit.

Two audits with different jobs run in AlphaSolve:

- **Short horizon (this module).** One worker finished; did it deliver the bounded task
  it was assigned?  It checks the proven Statement against the acceptance rubric, the
  hint, and the pinned target, and names scope drift (weakening, narrowing, a different
  claim).  It runs **synchronously** inside worker collection so its verdict is attached
  to the very ``TaskOutput`` that reports the worker, because the whole point is to tell
  the orchestrator whether its own instruction was carried out before it decides what to
  dispatch next.  A deferred delivery verdict would arrive after that decision.

- **Long horizon (``progress_audit``).** Every N settled outcomes, is the portfolio
  moving toward ``problem.md``?  That one is asynchronous, ignores rubrics, and judges
  research progress rather than instruction compliance.

A verifier ruling and a delivery ruling are independent: a worker can prove something
correct (``verified``) that is not what was asked (``off_target``), and that gap is
exactly what this module surfaces.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from alphasolve.agent import Agent, Workspace

from .tool_runtime import build_solver_tool_registry
from .workspace_access import RoleWorkspaceAccess

if TYPE_CHECKING:
    from .client_factory import ClientFactory
    from .logging.log_session import LogSession
    from .project import ProjectLayout


TaskAuditRunner = Callable[[str], str]

DELIVERY_VERDICTS = ("delivered", "partial", "off_target", "not_delivered")
# 只有 delivered 算交付完成；其余三种都意味着分派的义务仍然悬空。
_UNDELIVERED = {"partial", "off_target", "not_delivered"}
# 非数学性失败（协议错误、执行异常）没有可验收的交付物，跑一次 LLM 只会得到
# "没有陈述可查"，因此直接由运行时给出确定性结论。
_NON_MATHEMATICAL_FAILURES = {"generator_protocol_failure", "execution_failed"}
_SKIPPED_STATUSES = {"cancelled"}


@dataclass(frozen=True)
class TaskAuditResult:
    worker_id: str
    delivery: str
    rubric_passed: int
    rubric_total: int
    scope_drift: str
    residual_obligation: str
    assigned_versus_delivered: str
    rubric_checks: list[dict[str, str]]
    audit_path: str
    status: str = "completed"
    # 仅在 verifier 拒绝时填写：拒绝落在哪一层、还剩什么可复用、是否值得再试。
    rejection_locus: str = ""
    salvageable_content: str = ""
    retry_assessment: str = ""

    def payload(self) -> dict[str, Any]:
        """Return the bounded surface the orchestrator sees on TaskOutput."""
        result: dict[str, Any] = {
            "worker_id": self.worker_id,
            "status": self.status,
            "delivery": self.delivery,
            "delivered": self.delivery == "delivered",
            "scope_drift": self.scope_drift,
            "residual_obligation": self.residual_obligation,
            "assigned_versus_delivered": self.assigned_versus_delivered,
            "audit_path": self.audit_path,
        }
        if self.rubric_total:
            result["rubric_score"] = f"{self.rubric_passed}/{self.rubric_total}"
            result["rubric_passed"] = self.rubric_passed
            result["rubric_total"] = self.rubric_total
        if self.rubric_checks:
            result["rubric_checks"] = self.rubric_checks
        # 拒绝诊断替代 rubric 成为 orchestrator 的决策依据：定理错 vs 证明错，
        # 决定了应当换目标还是修证明。
        if self.rejection_locus:
            result["rejection_locus"] = self.rejection_locus
        if self.salvageable_content:
            result["salvageable_content"] = self.salvageable_content
        if self.retry_assessment:
            result["retry_assessment"] = self.retry_assessment
        return result


class TaskAuditor:
    """Runs one delivery audit per finished worker, synchronously."""

    def __init__(
        self,
        *,
        layout: "ProjectLayout",
        suite: Any,
        client_factory: "ClientFactory",
        log_session: "LogSession | None" = None,
        stop_event: threading.Event | None = None,
        audit_runner: TaskAuditRunner | None = None,
    ) -> None:
        self.layout = layout
        self.suite = suite
        self.client_factory = client_factory
        self.log_session = log_session
        self.stop_event = stop_event
        self.audit_runner = audit_runner
        self._lock = threading.Lock()

    def available(self) -> bool:
        if self.audit_runner is not None:
            return True
        return getattr(self.suite, "agents", {}).get("task_auditor") is not None

    def audit(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Audit one completed worker payload and persist the report.

        Returns the bounded orchestrator-facing payload, or ``None`` when this outcome
        has nothing to verify against (cancelled work, or a non-mathematical failure).
        Any internal error degrades to a recorded ``status`` rather than propagating:
        a failed delivery audit must never hide a completed worker result.
        """
        worker_id = str(payload.get("worker_id") or "").strip()
        if not worker_id:
            return None
        status = str(payload.get("status") or "").strip()
        if status in _SKIPPED_STATUSES:
            return None
        if self.stop_event is not None and self.stop_event.is_set():
            return None

        deterministic = self._deterministic_result(payload)
        if deterministic is not None:
            self._write_report(deterministic, payload, audit_text="")
            return deterministic.payload()
        if not self.available():
            return None

        criteria = _rubric_criteria(payload.get("rubric"))
        prompt = _audit_prompt(payload, self.layout, criteria=criteria)
        try:
            audit_text = self.audit_runner(prompt) if self.audit_runner else self._run_auditor(prompt)
        except Exception as exc:
            result = TaskAuditResult(
                worker_id=worker_id,
                delivery="unknown",
                rubric_passed=0,
                rubric_total=len(criteria),
                scope_drift="",
                residual_obligation="",
                assigned_versus_delivered="",
                rubric_checks=[],
                audit_path="",
                status="failed",
            )
            result = self._write_report(result, payload, audit_text=f"# Task Audit Failure\n\n{type(exc).__name__}: {exc}\n")
            return result.payload()

        parsed = _parse_task_audit(audit_text, expected_criteria=len(criteria))
        result = TaskAuditResult(
            worker_id=worker_id,
            delivery=parsed["delivery"] or "unknown",
            rubric_passed=parsed["rubric_passed"],
            rubric_total=parsed["rubric_total"] or len(criteria),
            scope_drift=parsed["scope_drift"],
            residual_obligation=parsed["residual_obligation"],
            assigned_versus_delivered=parsed["assigned_versus_delivered"],
            rubric_checks=parsed["rubric_checks"],
            audit_path="",
            status="completed" if parsed["delivery"] else "invalid_audit",
            rejection_locus=parsed["rejection_locus"],
            salvageable_content=parsed["salvageable_content"],
            retry_assessment=parsed["retry_assessment"],
        )
        result = self._write_report(result, payload, audit_text=audit_text)
        return result.payload()

    def _deterministic_result(self, payload: dict[str, Any]) -> TaskAuditResult | None:
        """Settle outcomes that carry no auditable deliverable without calling an LLM."""
        failure_kind = str(payload.get("failure_kind") or "")
        if failure_kind not in _NON_MATHEMATICAL_FAILURES:
            return None
        criteria = _rubric_criteria(payload.get("rubric"))
        reason = (
            "The generator never produced a protocol-valid proposition.md, so no Statement exists to check."
            if failure_kind == "generator_protocol_failure"
            else "The worker aborted with a runtime execution failure, so no Statement exists to check."
        )
        return TaskAuditResult(
            worker_id=str(payload.get("worker_id") or ""),
            delivery="not_delivered",
            rubric_passed=0,
            rubric_total=len(criteria),
            scope_drift="",
            residual_obligation=str(payload.get("blocking_obligation") or "").strip(),
            assigned_versus_delivered=reason,
            rubric_checks=[
                {"criterion": criterion, "verdict": "fail", "note": "No proven Statement exists."}
                for criterion in criteria
            ],
            audit_path="",
        )

    def _run_auditor(self, prompt: str) -> str:
        config = self.suite.agents.get("task_auditor")
        if config is None:
            raise RuntimeError("task_auditor agent config is required for task delivery audits")
        access = RoleWorkspaceAccess.task_auditor(Workspace(self.layout.workspace_dir))
        event_sink = None
        if self.log_session is not None:
            from .logging.event_log import compose_event_sinks

            event_sink = compose_event_sinks(
                self.log_session.token_usage_sink("task_auditor"),
                self.log_session.run_log_sink("task_auditor"),
            )
        agent = Agent(
            config=config,
            client=self.client_factory(config),
            tool_registry=build_solver_tool_registry(access),
            event_sink=event_sink,
            stop_event=self.stop_event,
        )
        return agent.run(prompt, description="Task delivery audit").final_answer

    def _write_report(
        self,
        result: TaskAuditResult,
        payload: dict[str, Any],
        *,
        audit_text: str,
    ) -> TaskAuditResult:
        directory = self.layout.task_audits_dir
        path = directory / f"{result.worker_id}.md"
        decision_path = directory / f"{result.worker_id}.json"
        rendered = _render_report(result, payload, audit_text=audit_text)
        try:
            with self._lock:
                directory.mkdir(parents=True, exist_ok=True)
                path.write_text(rendered, encoding="utf-8")
                relative = _relative(path, self.layout.workspace_dir)
                decision = {
                    "worker_id": result.worker_id,
                    "recorded_at": _now_iso(),
                    "status": result.status,
                    "delivery": result.delivery,
                    "rubric_passed": result.rubric_passed,
                    "rubric_total": result.rubric_total,
                    "scope_drift": result.scope_drift,
                    "residual_obligation": result.residual_obligation,
                    "audit_path": relative,
                }
                temporary = decision_path.with_suffix(".json.tmp")
                temporary.write_text(json.dumps(decision, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                temporary.replace(decision_path)
        except OSError:
            return result
        return TaskAuditResult(
            worker_id=result.worker_id,
            delivery=result.delivery,
            rubric_passed=result.rubric_passed,
            rubric_total=result.rubric_total,
            scope_drift=result.scope_drift,
            residual_obligation=result.residual_obligation,
            assigned_versus_delivered=result.assigned_versus_delivered,
            rubric_checks=result.rubric_checks,
            audit_path=relative,
            status=result.status,
            rejection_locus=result.rejection_locus,
            salvageable_content=result.salvageable_content,
            retry_assessment=result.retry_assessment,
        )


def summarize_task_audits(audits: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Turn per-worker delivery verdicts into one bounded instruction for the orchestrator."""
    usable = [item for item in audits if isinstance(item, dict) and item.get("delivery")]
    if not usable:
        return None
    undelivered = [item for item in usable if str(item.get("delivery")) in _UNDELIVERED]
    drifted = [item for item in usable if str(item.get("scope_drift") or "").strip()]
    summary: dict[str, Any] = {
        "audited": len(usable),
        "delivered": sum(1 for item in usable if str(item.get("delivery")) == "delivered"),
        "undelivered_worker_ids": [str(item.get("worker_id")) for item in undelivered],
    }
    if drifted:
        summary["scope_drift_worker_ids"] = [str(item.get("worker_id")) for item in drifted]
    # 被拒绝的 worker 没有可读的 rubric 分数；把拒绝落点按类别聚合，
    # 让 orchestrator 直接看到"该换目标"还是"该修证明"。
    rejected = [item for item in usable if str(item.get("rejection_locus") or "").strip()]
    if rejected:
        loci: dict[str, list[str]] = {}
        for item in rejected:
            loci.setdefault(str(item.get("rejection_locus")), []).append(str(item.get("worker_id")))
        summary["rejection_loci"] = {key: loci[key] for key in sorted(loci)}
    if undelivered:
        summary["instruction"] = (
            "A task audit compares the assigned rubric with the proven Statement. Where delivery is partial, "
            "off_target, or not_delivered, the residual obligation is still open even if a proposition was verified: "
            "do not treat it as done. Either reassign the residual obligation with a sharper rubric, or record why "
            "it is no longer worth pursuing. For a rejected worker the rubric score is uninformative; read its "
            "rejection_locus, salvageable_content, and retry_assessment instead, and note that statement_false means "
            "change the target while proof_repairable means the same target is worth another pass."
        )
    else:
        summary["instruction"] = (
            "Every audited worker delivered its assigned task. Choose the next bounded task from research evidence "
            "rather than re-running these obligations."
        )
    return summary


def _rubric_criteria(rubric: Any) -> list[str]:
    """Split an acceptance rubric into its bullet criteria."""
    text = str(rubric or "").strip()
    if not text:
        return []
    criteria: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            criterion = stripped[2:].strip()
            if criterion:
                criteria.append(criterion)
    if criteria:
        return criteria[:12]
    # 未按约定使用 bullet 时，整段当作单条验收标准，而不是当作"无标准"。
    return [" ".join(text.split())[:600]]


def _audit_prompt(payload: dict[str, Any], layout: "ProjectLayout", *, criteria: list[str]) -> str:
    workspace_dir = layout.workspace_dir
    statement = _proven_statement(payload, workspace_dir)
    rubric_block = (
        "\n".join(f"{index}. {criterion}" for index, criterion in enumerate(criteria, 1))
        if criteria
        else "(No rubric was supplied. Check the assigned hint and pinned target as a single criterion.)"
    )
    artifacts = [
        f"- {label}: `{_display(payload.get(key), workspace_dir)}`"
        for key, label in (
            ("verified_file", "Verified proposition"),
            ("proposition_file", "Candidate proposition"),
            ("review_file", "Final verifier review"),
            ("theorem_check_file", "Theorem check"),
        )
        if str(payload.get(key) or "").strip()
    ]
    handoff = payload.get("difficulty_handoff")
    obstacle = ""
    if isinstance(handoff, dict):
        obstacle = str(handoff.get("obstacle") or "").strip()
    # 被拒绝时没有可验收的 Statement，rubric 逐条核对退化为全 fail；此时真正有决策价值的
    # 是拒绝落在哪一层。把 verifier 的拒绝理由注入，让审计能区分定理错与证明错。
    rejected = str(payload.get("status") or "") == "rejected"
    rejection_block = ""
    if rejected:
        rejection_block = _read_text(_safe_audit_path(payload.get("review_file")), limit=6000) or str(
            payload.get("blocking_obligation") or ""
        )[:6000]
    return "\n".join(
        part
        for part in [
            "Audit whether this one worker delivered its assigned bounded task.",
            "",
            "## Original Problem (context only; you are not judging progress on it)",
            _read_text(workspace_dir / "problem.md", limit=4000) or "(problem.md unavailable)",
            "",
            "## Assigned Task",
            str(payload.get("worker_hint") or "(no hint was given)"),
            "",
            "### Pinned Target" if str(payload.get("pinned_target") or "").strip() else "",
            str(payload.get("pinned_target") or "").strip(),
            "",
            "### Acceptance Rubric",
            rubric_block,
            "",
            "## Execution Outcome",
            f"- Verifier status: `{payload.get('status') or 'unknown'}`",
            f"- Assigned method: `{payload.get('method_id') or '-'}`",
            f"- Canonical difficulty: `{payload.get('difficulty_id') or '(none)'}`",
            f"- Failure kind: `{payload.get('failure_kind') or '-'}`",
            f"- Runtime summary: {str(payload.get('summary') or '')[:2000]}",
            f"- Worker-reported obstacle: {obstacle}" if obstacle else "",
            "",
            "## Proven Statement",
            statement or "(No verified or candidate Statement is available.)",
            "",
            "## Why The Verifier Rejected It" if rejection_block else "",
            rejection_block,
            "",
            "## Artifacts You May Read",
            "\n".join(artifacts) if artifacts else "- None recorded.",
            "",
            (
                "This worker was REJECTED, so no Statement was accepted and every substantive criterion is unmet. "
                "Record the rubric verdicts briefly, then spend your effort on the rejection diagnosis sections: "
                "where the rejection falls, what survives, and whether a retry is worth it. That diagnosis, not the "
                "rubric score, is what the orchestrator needs."
                if rejected
                else "Judge each rubric criterion against the Proven Statement. A `verified` status does not by itself "
                "mean the assigned task was delivered."
            ),
            "Follow your required output sections exactly, and end with the trailing marker lines.",
        ]
        if part
    )


def _safe_audit_path(value: Any) -> Path:
    return Path(str(value or ""))


def _proven_statement(payload: dict[str, Any], workspace_dir: Path) -> str:
    """Extract the Statement section of whichever proposition the worker ended with."""
    for key in ("verified_file", "proposition_file"):
        raw = str(payload.get(key) or "").strip()
        if not raw:
            continue
        text = _read_text(Path(raw), limit=20000)
        if not text:
            continue
        match = re.search(r"(?ims)^##\s+Statement\s*$\s*(.*?)(?=^##\s+|\Z)", text)
        statement = (match.group(1).strip() if match else text.strip())[:6000]
        if statement:
            label = "verified" if key == "verified_file" else "candidate (not verified)"
            return f"[{label}: `{_display(raw, workspace_dir)}`]\n\n{statement}"
    return ""


def _render_report(result: TaskAuditResult, payload: dict[str, Any], *, audit_text: str) -> str:
    lines = [
        f"# Task Delivery Audit: worker `{result.worker_id}`",
        "",
        "This report judges only whether the assigned bounded task was delivered. It is not a verification of "
        "mathematical correctness and not an assessment of progress toward `problem.md`.",
        "",
        "## Verdict",
        f"- Delivery: `{result.delivery}`",
        f"- Rubric score: `{result.rubric_passed}/{result.rubric_total}`" if result.rubric_total else "- Rubric score: `n/a`",
        f"- Verifier status: `{payload.get('status') or 'unknown'}`",
        f"- Audit status: `{result.status}`",
        "",
        "## Scope Drift",
        result.scope_drift or "None reported.",
        "",
        "## Residual Obligation",
        result.residual_obligation or "None reported.",
        "",
    ]
    if result.rejection_locus or result.salvageable_content or result.retry_assessment:
        lines.extend([
            "## Rejection Diagnosis",
            f"- Locus: `{result.rejection_locus or 'unclassified'}`",
            f"- Salvageable content: {result.salvageable_content or 'None reported.'}",
            f"- Retry assessment: {result.retry_assessment or 'None reported.'}",
            "",
        ])
    lines.extend([
        "## Assigned Task",
        str(payload.get("worker_hint") or "(no hint was given)"),
        "",
        "## Acceptance Rubric",
        str(payload.get("rubric") or "(no rubric was supplied)"),
        "",
    ])
    if audit_text.strip():
        lines.extend(["## Auditor Report", audit_text.strip(), ""])
    return "\n".join(lines).rstrip() + "\n"


def _parse_task_audit(text: str, *, expected_criteria: int) -> dict[str, Any]:
    delivery_match = re.search(
        r"(?mi)^\s*DELIVERY:\s*(delivered|partial|off_target|not_delivered)\s*$", text or ""
    )
    delivery = delivery_match.group(1).lower() if delivery_match else ""
    checks = _parse_rubric_checks(text)
    passed = sum(1 for item in checks if item["verdict"] == "pass")
    total = len(checks) or expected_criteria
    score_match = re.search(r"(?mi)^\s*RUBRIC_SCORE:\s*(\d+)\s*/\s*(\d+)\s*$", text or "")
    if score_match:
        # 以自报分数为准，但夹到已核对条目数内，避免 LLM 报出超出条目数的分数。
        reported_total = max(1, int(score_match.group(2)))
        total = reported_total if not checks else total
        passed = min(int(score_match.group(1)), total)
    drift_match = re.search(r"(?mi)^\s*SCOPE_DRIFT:\s*(.+?)\s*$", text or "")
    drift = drift_match.group(1).strip() if drift_match else ""
    if drift.upper() == "NONE":
        drift = ""
    locus_match = re.search(
        r"(?mi)^\s*REJECTION_LOCUS:\s*(statement_false|statement_unproved|proof_gap|proof_repairable|not_applicable)\s*$",
        text or "",
    )
    locus = locus_match.group(1).lower() if locus_match else ""
    if locus == "not_applicable":
        locus = ""
    return {
        "delivery": delivery,
        "rubric_passed": passed,
        "rubric_total": total,
        "rubric_checks": checks,
        "scope_drift": drift[:2000],
        "residual_obligation": _section(text, "Residual Obligation"),
        "assigned_versus_delivered": _section(text, "Assigned Versus Delivered"),
        "rejection_locus": locus,
        "salvageable_content": _section(text, "Salvageable Content"),
        "retry_assessment": _section(text, "Retry Assessment"),
    }


def _parse_rubric_checks(text: str) -> list[dict[str, str]]:
    section = _section(text, "Rubric Check", limit=8000)
    checks: list[dict[str, str]] = []
    for line in section.splitlines():
        match = re.match(r"\s*[-*]\s*\[(pass|fail|unclear)\]\s*(.+?)\s*$", line, re.IGNORECASE)
        if match is None:
            continue
        body = match.group(2).strip()
        criterion, _, note = body.partition("—")
        checks.append({
            "verdict": match.group(1).lower(),
            "criterion": " ".join(criterion.split())[:400] or body[:400],
            "note": " ".join(note.split())[:600],
        })
    return checks[:12]


def _section(text: str, heading: str, *, limit: int = 4000) -> str:
    match = re.search(
        rf"(?ims)^###\s+{re.escape(heading)}\s*$\s*(.*?)(?=^###\s+|^```text|\Z)",
        text or "",
    )
    return match.group(1).strip()[:limit] if match else ""


def _read_text(path: Path, *, limit: int) -> str:
    try:
        return path.read_text(encoding="utf-8")[:limit].strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _display(raw: Any, workspace_dir: Path) -> str:
    text = str(raw or "").strip()
    if not text:
        return "-"
    return _relative(Path(text), workspace_dir)


def _relative(path: Path, workspace_dir: Path) -> str:
    try:
        return path.resolve().relative_to(workspace_dir.resolve()).as_posix()
    except (OSError, ValueError):
        return str(path)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
