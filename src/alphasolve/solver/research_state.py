from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_DIRECTION_STATUSES = {"active", "paused", "stalled", "refuted", "completed"}
_DIRECTION_HEALTH = {"unassessed", "healthy", "strong", "stalled", "under_challenge", "refuted", "completed"}
# Gap 是可递归分解的义务节点；``superseded`` 表示其祖先或 any_of sibling 已经
# 解决，``advanced`` 表示已具备继续组装/综合的证据但尚未给出父命题的证明。
_GAP_STATUSES = {"open", "advanced", "closed", "invalidated", "superseded"}
_GAP_RESOLUTION_POLICIES = {"all_of", "any_of", "manual"}
_GAP_KINDS = {"unknown", "terminal", "lemma", "subcase", "construction", "falsification", "synthesis"}
_RELATIONS = {
    "solves_target",
    "sufficient_for_target",
    "direct_advance",
    "necessary_condition_only",
    "weaker_than_target",
    "refutes_target",
    "orthogonal",
    "duplicate",
    "unrelated",
    "unknown",
}
_GAP_EFFECTS = {"closed", "advanced", "unchanged", "invalidated"}
_CONTINUATION_VALUES = {"high", "medium", "low", "none"}
# 方向类型：impossibility 类（证明"某值最优 / 某配置不可能"）带一个 standing_hypothesis
# 与一个 terminal_gap（那个 ⊥ 义务）。这类方向对 advance 判定更严格，且是"兑现失败→撞墙→
# 触发对偶探针"链条的主要对象。缺省 unknown 时按普通方向处理，完全向后兼容。
_DIRECTION_KINDS = {
    "unknown",
    "impossibility",
    "lower_bound",
    "upper_bound",
    "construction",
    "existence",
    "classification",
}
# 兑现（consolidation）/对偶尝试的三态结局：
#   solved     —— 按原样证出钉死目标；
#   blocked_gap—— 没证出，但定位到一个可攻的具体缺口（真在收敛）；
#   wall       —— 反复撞同一堵墙 / 拼不出通路（疑似目标为假）→ 触发对偶探针 + 降权。
_CONSOLIDATION_OUTCOMES = {"solved", "blocked_gap", "wall"}

# 禁忌搜索——方法失败分类：
#   method_blocked      —— 思路错：该方法无法达到目标，但目标本身可能正确。
#                          orchestrator 应保留 goal、换方法重试。
#   conclusion_refuted  —— 结论错：目标本身可能为假（找到反例/证伪）。
#                          orchestrator 应考虑 refuted 方向或换目标。
_METHOD_FAILURE_TYPES = {"method_blocked", "conclusion_refuted"}
_DISPATCH_CONSTRAINT_STATUSES = {"refuted", "needs_falsification"}
_DISPATCH_EVIDENCE_LEVELS = {"verified", "explicit_witness", "exhaustive_finite", "sampled"}
_POSITIVE_RELATIONS = {"solves_target", "sufficient_for_target", "direct_advance"}
_ROUTE_STATUSES = {"active", "paused", "completed", "refuted", "superseded"}
_ROUTE_ASSESSMENTS = {"ADVANCING", "PARTIAL", "STALLED", "REFUTED", "REDUNDANT", "INCONCLUSIVE"}
_ROUTE_LEARNING_CONFIDENCE = {"low", "medium", "high"}
# A weak/negative relation can never legitimately close the gap it was assessed against;
# only solves_target/sufficient_for_target/direct_advance may report gap_effect="closed".
_DISALLOWED_GAP_EFFECTS_FOR_RELATION: dict[str, set[str]] = {
    "necessary_condition_only": {"closed"},
    "weaker_than_target": {"closed"},
    "duplicate": {"closed"},
    "unrelated": {"closed"},
    "orthogonal": {"closed"},
    "unknown": {"closed"},
    "refutes_target": {"closed"},
}


def _clean_id(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not text or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", text):
        raise ValueError(f"{field} must be a non-empty stable id using letters, digits, '.', '_' or '-'")
    return text


def _clean_text(value: Any, *, field: str, max_length: int = 8000) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} must be non-empty")
    if len(text) > max_length:
        raise ValueError(f"{field} exceeds {max_length} characters")
    return text


def _string_list(value: Any, *, field: str, max_items: int = 50) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    if len(value) > max_items:
        raise ValueError(f"{field} exceeds {max_items} items")
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_audit_blocker_approaches(value: Any, *, occurrence_count: int) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not value or len(value) > 12:
        raise ValueError("blocker.approaches must be a non-empty array with at most 12 items")
    approaches: list[dict[str, Any]] = []
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise ValueError("blocker.approaches items must be objects")
        approaches.append({
            "direction_id": _clean_id(raw.get("direction_id"), field=f"blocker.approaches[{index}].direction_id"),
            "gap_id": _clean_id(raw.get("gap_id"), field=f"blocker.approaches[{index}].gap_id"),
            "method_id": _clean_id(raw.get("method_id"), field=f"blocker.approaches[{index}].method_id"),
            "occurrence_count": _clean_positive_int(
                raw.get("occurrence_count"), field=f"blocker.approaches[{index}].occurrence_count"
            ),
            "description": _clean_text(
                " ".join(str(raw.get("description") or "").split()),
                field=f"blocker.approaches[{index}].description",
                max_length=800,
            ),
        })
    if sum(item["occurrence_count"] for item in approaches) != occurrence_count:
        raise ValueError("blocker.approaches occurrence counts must sum to blocker.occurrence_count")
    return approaches


def _clean_positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalize_rubric_assessment(value: Any) -> dict[str, Any] | None:
    """Store a bounded, evidence-bearing assessment for later portfolio comparison."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("rubric_assessment must be an object")
    raw_checks = value.get("checks") or []
    if not isinstance(raw_checks, list) or len(raw_checks) > 12:
        raise ValueError("rubric_assessment.checks must be an array with at most 12 items")
    checks: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_checks, start=1):
        if not isinstance(raw, dict):
            raise ValueError("rubric_assessment.checks items must be objects")
        criterion = _clean_text(raw.get("criterion"), field=f"rubric_assessment.checks[{index}].criterion", max_length=800)
        evidence = _clean_text(raw.get("evidence"), field=f"rubric_assessment.checks[{index}].evidence", max_length=1600)
        checks.append({
            "criterion": criterion,
            "passed": bool(raw.get("passed")),
            "evidence": evidence,
        })
    passed_count = sum(1 for item in checks if item["passed"])
    return {
        "checks": checks,
        "passed_count": passed_count,
        "total_checks": len(checks),
    }


class ResearchStateStore:
    """Direction-centric canonical research state with a generated Markdown view."""

    SCHEMA_VERSION = 2

    # 兑现（consolidation）时机：per-direction 硬规则已移除（不再 hard-gate 单个方向）。
    # 保留参数仅为向后兼容；consolidation_readiness 不再返回任何方向。
    CONSOLIDATION_EVERY_N_PROPS = 5
    # 全局 consolidation：当全局 verified props 总数相对于上次全局 consolidation 增量 >= 此值，
    # 自动触发一个攻击 problem.md 本身的 consolidation worker（不是 per-direction 的）。
    GLOBAL_CONSOLIDATION_EVERY_N_PROPS = 20

    def __init__(self, verified_dir: Path, *, consolidation_every_n_props: int | None = None) -> None:
        self.verified_dir = verified_dir
        self.workspace_dir = verified_dir.parent
        self.json_path = verified_dir / "research_state.json"
        self.markdown_path = verified_dir / "state.md"
        self.attempt_ledger_path = self.workspace_dir / "progress_audit_outcomes.jsonl"
        self.blocker_registry_path = self.workspace_dir / "curation_records" / "blocker_registry.json"
        # 硬规则阈值 X：优先用显式传入，其次环境变量 AS_CONSOLIDATION_EVERY_N_PROPS，
        # 最后回退类默认（5）。至少为 1。
        resolved = consolidation_every_n_props
        if resolved is None:
            env_value = os.environ.get("AS_CONSOLIDATION_EVERY_N_PROPS")
            if env_value is not None and env_value.strip():
                try:
                    resolved = int(env_value)
                except ValueError:
                    resolved = None
        if resolved is None:
            resolved = self.CONSOLIDATION_EVERY_N_PROPS
        self.consolidation_every_n_props = max(1, int(resolved))

    def load(self) -> dict[str, Any]:
        if not self.json_path.is_file():
            return self._empty_state()
        try:
            raw = json.loads(self.json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._empty_state()
        if not isinstance(raw, dict):
            return self._empty_state()
        raw.setdefault("objective", {"summary": "Resolve problem.md", "status": "unknown"})
        raw.setdefault("state_revision", 0)
        raw.setdefault("state_id", "state-0")
        raw.setdefault("directions", {})
        raw.setdefault("routes", {})
        raw.setdefault("route_learnings", [])
        raw.setdefault("dispatch_plan", [])
        raw.setdefault("impacts", {})
        raw.setdefault("outcomes", {})
        raw.setdefault("dispatch_constraints", {})
        raw.setdefault("audit_blockers", {})
        self._migrate_loaded_gap_state(raw)
        raw["schema_version"] = self.SCHEMA_VERSION
        return raw

    def _migrate_loaded_gap_state(self, state: dict[str, Any]) -> None:
        """Upgrade canonical v1 `gaps` / `plan` payloads into the v2 gap forest in memory.

        The conversion is deliberately idempotent.  It supports both list and map variants
        observed in historical state files and is persisted atomically on the next state save.
        """
        directions = state.get("directions")
        if not isinstance(directions, dict):
            state["directions"] = {}
            return
        for direction in directions.values():
            if not isinstance(direction, dict):
                continue
            steps = direction.get("steps")
            if not isinstance(steps, dict):
                migrated: dict[str, Any] = {}
                legacy_gaps = direction.get("gaps") or []
                if isinstance(legacy_gaps, dict):
                    legacy_items = [
                        {**item, "step_id": item.get("step_id") or item.get("gap_id") or gap_id}
                        for gap_id, item in legacy_gaps.items() if isinstance(item, dict)
                    ]
                elif isinstance(legacy_gaps, list):
                    legacy_items = [item for item in legacy_gaps if isinstance(item, dict)]
                else:
                    legacy_items = []
                for item in legacy_items:
                    raw_id = item.get("step_id") or item.get("gap_id")
                    if not raw_id:
                        continue
                    try:
                        gap_id = _clean_id(raw_id, field="legacy gap_id")
                    except ValueError:
                        continue
                    migrated[gap_id] = {
                        "step_id": gap_id,
                        "statement": str(item.get("statement") or item.get("goal") or gap_id).strip(),
                        "status": str(item.get("status") or "open").strip(),
                        "method": str(item.get("method") or "").strip(),
                        "results": list(item.get("results") or []),
                    }
                for index, plan_item in enumerate(direction.get("plan") or []):
                    if not isinstance(plan_item, str) or not plan_item.strip():
                        continue
                    gap_id = f"plan-{index}"
                    migrated.setdefault(gap_id, {
                        "step_id": gap_id,
                        "statement": plan_item.strip(),
                        "status": "open",
                        "method": "",
                        "results": [],
                    })
                direction["steps"] = migrated
            try:
                self._normalize_gap_tree(direction)
            except ValueError:
                # Preserve malformed legacy evidence for explicit repair rather than making
                # read-only status inspection fail; ordinary SyncResearchState will reject it.
                continue

    def _has_valid_canonical(self) -> bool:
        if not self.json_path.is_file():
            return False
        try:
            return isinstance(json.loads(self.json_path.read_text(encoding="utf-8")), dict)
        except (OSError, json.JSONDecodeError):
            return False

    def ensure_initialized(self) -> dict[str, Any]:
        """Repair a missing/blank generated view without destroying a non-empty legacy state.md."""
        markdown_blank = True
        if self.markdown_path.is_file():
            try:
                markdown_blank = not self.markdown_path.read_text(encoding="utf-8").strip()
            except OSError:
                markdown_blank = True

        if self._has_valid_canonical():
            state = self.load()
            if markdown_blank:
                self._save(state)
            return {"initialized": True, "legacy_state_preserved": False, **self.summary(state)}

        if markdown_blank:
            state = self._empty_state()
            state["updated_at"] = _now_iso()
            self._save(state)
            return {"initialized": True, "legacy_state_preserved": False, **self.summary(state)}

        # A non-empty pre-schema state.md may contain valuable prior strategy. Leave it untouched until
        # the orchestrator explicitly reconciles it through SyncResearchState.
        return {
            "initialized": False,
            "legacy_state_preserved": True,
            "state_file": str(self.markdown_path),
            "canonical_file": str(self.json_path),
            "direction_count": 0,
            "directions": [],
        }

    def has_unmigrated_legacy_state(self) -> bool:
        """Whether a non-empty legacy state.md exists without canonical JSON."""
        if self._has_valid_canonical() or not self.markdown_path.is_file():
            return False
        try:
            return bool(self.markdown_path.read_text(encoding="utf-8").strip())
        except OSError:
            return False

    def default_target(self) -> tuple[str, str] | None:
        """Return the sole active direction/open gap when assignment is unambiguous."""
        state = self.load()
        candidates: list[tuple[str, str]] = []
        for direction in state.get("directions", {}).values():
            if direction.get("status") not in {"active", "stalled"}:
                continue
            if not isinstance(direction, dict):
                continue
            self._normalize_gap_tree(direction)
            steps = direction.get("steps", {})
            children = self._gap_children(steps)
            executable = [
                step_id
                for step_id, step in steps.items()
                if step.get("status") not in {"closed", "invalidated", "superseded"}
                and (not children.get(step_id) or bool(step.get("ready_for_synthesis")))
            ]
            if len(executable) == 1:
                candidates.append((str(direction["direction_id"]), str(executable[0])))
        return candidates[0] if len(candidates) == 1 else None

    def reviewer_snapshot(self) -> dict[str, Any]:
        """Return the canonical strategic context supplied to a research reviewer.

        The reviewer receives this snapshot explicitly instead of reconstructing strategy from
        generated Markdown.  Routes and curator lessons stay attached to the state that
        motivated the next route.
        """
        state = self.load()
        return {
            "state_id": state.get("state_id", "state-0"),
            "state_revision": int(state.get("state_revision") or 0),
            "objective": deepcopy(state.get("objective") or {}),
            "directions": deepcopy(state.get("directions") or {}),
            "routes": deepcopy(state.get("routes") or {}),
            "route_learnings": deepcopy(state.get("route_learnings") or []),
            "dispatch_constraints": deepcopy(state.get("dispatch_constraints") or {}),
            "audit_blockers": deepcopy(state.get("audit_blockers") or {}),
        }

    def register_route(self, *, route_id: str, based_on_state_id: str, direction_id: str, gap_id: str,
                       route_claim: str, target: str, success_condition: str, stop_condition: str,
                       evidence_refs: list[str] | None = None) -> dict[str, Any]:
        """Persist one reviewer-proposed route before the orchestrator expands it.

        A route is intentionally coarser than a worker: the reviewer chooses the mathematical
        path, while the orchestrator chooses and compares its concrete worker branches.
        """
        rid = _clean_id(route_id, field="route_id")
        did = _clean_id(direction_id, field="direction_id")
        gid = _clean_id(gap_id, field="gap_id")
        snapshot_id = _clean_text(based_on_state_id, field="based_on_state_id", max_length=120)
        state = self.load()
        current_id = str(state.get("state_id") or "state-0")
        if snapshot_id != current_id:
            raise ValueError(
                f"route is based on stale state {snapshot_id!r}; current canonical state is {current_id!r}"
            )
        routes = state.setdefault("routes", {})
        if rid in routes:
            raise ValueError(f"route_id already exists: {rid}")
        self._ensure_target_in_state(state, direction_id=did, gap_id=gid, hint=target)
        routes[rid] = {
            "route_id": rid,
            "based_on_state_id": snapshot_id,
            "direction_id": did,
            # Route 绑定一个 gap subtree；它的 workers 可以被分派到这个 root 或任何后代叶子。
            "gap_id": gid,
            "scope_gap_id": gid,
            "route_claim": _clean_text(route_claim, field="route_claim", max_length=3000),
            "target": _clean_text(target, field="target", max_length=3000),
            "success_condition": _clean_text(success_condition, field="success_condition", max_length=2000),
            "stop_condition": _clean_text(stop_condition, field="stop_condition", max_length=2000),
            "evidence_refs": _string_list(evidence_refs, field="evidence_refs", max_items=20),
            "status": "active",
            "worker_ids": [],
            "outcomes": [],
            "assessments": [],
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        state["updated_at"] = _now_iso()
        self._save(state)
        return {"registered": True, "route": deepcopy(routes[rid]), **self.summary(state)}

    def attach_worker_to_route(self, *, route_id: str, worker_id: str, direction_id: str | None,
                               gap_id: str | None, method_id: str | None) -> dict[str, Any]:
        """Record that a concrete orchestrator branch belongs to a reviewer route."""
        rid = _clean_id(route_id, field="route_id")
        wid = _clean_id(worker_id, field="worker_id")
        state = self.load()
        route = (state.get("routes") or {}).get(rid)
        if not isinstance(route, dict):
            raise ValueError(f"unknown route_id: {rid}")
        if route.get("status") != "active":
            raise ValueError(f"route {rid} is not active")
        route_direction_id = str(route.get("direction_id") or "")
        scope_gap_id = str(route.get("scope_gap_id") or route.get("gap_id") or "")
        if direction_id and str(direction_id) != route_direction_id:
            raise ValueError("worker direction_id must match its route")
        if gap_id:
            direction = (state.get("directions") or {}).get(route_direction_id)
            if not isinstance(direction, dict) or not self._descendant_or_self(
                direction, ancestor_id=scope_gap_id, gap_id=str(gap_id)
            ):
                raise ValueError("worker gap_id must be the route gap or one of its descendants")
        worker_ids = route.setdefault("worker_ids", [])
        if wid not in worker_ids:
            worker_ids.append(wid)
            route["updated_at"] = _now_iso()
            state["updated_at"] = _now_iso()
            self._save(state)
        return {"attached": True, "route_id": rid, "worker_id": wid}

    def assess_route(self, *, route_id: str, verdict: str, summary: str,
                     evidence_paths: list[str] | None = None, close_route: bool = False) -> dict[str, Any]:
        """Store the orchestrator's comparison of a route against the current portfolio."""
        rid = _clean_id(route_id, field="route_id")
        normalized_verdict = str(verdict or "").strip().upper()
        if normalized_verdict not in _ROUTE_ASSESSMENTS:
            raise ValueError(f"invalid route verdict: {normalized_verdict}")
        state = self.load()
        route = (state.get("routes") or {}).get(rid)
        if not isinstance(route, dict):
            raise ValueError(f"unknown route_id: {rid}")
        assessment = {
            "verdict": normalized_verdict,
            "summary": _clean_text(summary, field="summary", max_length=4000),
            "evidence_paths": _string_list(evidence_paths, field="evidence_paths", max_items=20),
            "recorded_at": _now_iso(),
        }
        route.setdefault("assessments", []).append(assessment)
        if close_route:
            route["status"] = "refuted" if normalized_verdict == "REFUTED" else "completed"
        route["updated_at"] = _now_iso()
        state["updated_at"] = _now_iso()
        self._save(state)
        return {"assessed": True, "route": deepcopy(route), **self.summary(state)}

    def commit_route_learning(self, *, learning_id: str, route_ids: list[str], finding: str,
                              confidence: str, evidence_paths: list[str] | None = None,
                              policy_effect: str = "") -> dict[str, Any]:
        """Persist curator's cross-route lesson as structured long-term memory."""
        lid = _clean_id(learning_id, field="learning_id")
        normalized_confidence = str(confidence or "").strip().lower()
        if normalized_confidence not in _ROUTE_LEARNING_CONFIDENCE:
            raise ValueError(f"invalid learning confidence: {normalized_confidence}")
        normalized_routes = [_clean_id(item, field="route_ids item") for item in _string_list(
            route_ids, field="route_ids", max_items=12
        )]
        if not normalized_routes:
            raise ValueError("route_ids must contain at least one route")
        state = self.load()
        routes = state.get("routes") or {}
        missing = [rid for rid in normalized_routes if rid not in routes]
        if missing:
            raise ValueError(f"unknown route_ids: {missing}")
        learnings = state.setdefault("route_learnings", [])
        record = {
            "learning_id": lid,
            "route_ids": normalized_routes,
            "finding": _clean_text(finding, field="finding", max_length=4000),
            "confidence": normalized_confidence,
            "evidence_paths": _string_list(evidence_paths, field="evidence_paths", max_items=20),
            "policy_effect": str(policy_effect or "").strip()[:2000],
            "recorded_at": _now_iso(),
        }
        existing = next((item for item in learnings if item.get("learning_id") == lid), None)
        if existing is None:
            learnings.append(record)
        else:
            existing.update(record)
        del learnings[:-100]
        state["updated_at"] = _now_iso()
        self._save(state)
        return {"committed": True, "learning": record, **self.summary(state)}

    def sync(self, args: dict[str, Any]) -> dict[str, Any]:
        objective_summary = _clean_text(args.get("objective_summary"), field="objective_summary")
        objective_status = str(args.get("objective_status") or "unknown").strip()
        raw_directions = args.get("directions")
        if not isinstance(raw_directions, list) or not raw_directions:
            raise ValueError("directions must be a non-empty array")

        current = self.load()
        current_directions = current.get("directions", {})
        directions: dict[str, Any] = {}
        for raw in raw_directions:
            if not isinstance(raw, dict):
                raise ValueError("each direction must be an object")
            direction = self._normalize_direction(raw, current_directions.get(str(raw.get("direction_id") or "")))
            directions[direction["direction_id"]] = direction
        # Omission is not deletion: retain previously recorded directions so one favored route cannot
        # accidentally erase competing or refuted research history. Include a direction explicitly to pause it.
        for direction_id, direction in current_directions.items():
            directions.setdefault(direction_id, deepcopy(direction))

        state = {
            "schema_version": self.SCHEMA_VERSION,
            "objective": {"summary": objective_summary, "status": objective_status},
            "directions": directions,
            # 路线与 curator 的学习结论是运行期积累的证据；一次同步计划不能悄悄抹去它们。
            "routes": current.get("routes", {}),
            "route_learnings": current.get("route_learnings", []),
            "dispatch_plan": _string_list(args.get("dispatch_plan"), field="dispatch_plan", max_items=20),
            "impacts": current.get("impacts", {}),
            "outcomes": current.get("outcomes", {}),
            "dispatch_constraints": current.get("dispatch_constraints", {}),
            "audit_blockers": current.get("audit_blockers", {}),
            "global_consolidation": current.get("global_consolidation", {}),
            "updated_at": _now_iso(),
        }
        self._save(state)
        return self.summary(state)

    def ensure_target(
        self,
        *,
        direction_id: str | None,
        gap_id: str | None,
        hint: str | None,
    ) -> None:
        """Standalone load/modify/save wrapper. Only call this outside another transaction —
        `record_impact` uses `_ensure_target_in_state` instead so both operate on one `state`."""
        if not direction_id:
            return
        state = self.load()
        changed = self._ensure_target_in_state(state, direction_id=direction_id, gap_id=gap_id, hint=hint)
        if changed:
            state["updated_at"] = _now_iso()
            self._save(state)

    def _ensure_target_in_state(
        self,
        state: dict[str, Any],
        *,
        direction_id: str | None,
        gap_id: str | None,
        hint: str | None,
    ) -> bool:
        """Mutate `state["directions"]` in place to guarantee `direction_id`/`gap_id` exist.
        Never loads or saves; the caller owns the single load/modify/save transaction."""
        if not direction_id:
            return False
        did = _clean_id(direction_id, field="direction_id")
        directions = state.setdefault("directions", {})
        changed = False
        if did not in directions:
            directions[did] = self._normalize_direction(
                {
                    "direction_id": did,
                    "title": did,
                    "goal": hint or "Explore this research direction",
                    "status": "active",
                    "health": "unassessed",
                    "steps": [],
                },
                None,
            )
            changed = True
        if gap_id:
            gid = _clean_id(gap_id, field="gap_id")
            steps = directions[did].setdefault("steps", {})
            if gid not in steps:
                steps[gid] = {
                    "step_id": gid,
                    "statement": (hint or "Open worker target").strip(),
                    "status": "open",
                    "method": "",
                    "results": [],
                    "parent_gap_id": None,
                    "child_gap_ids": [],
                    "resolution_policy": "manual",
                    "gap_kind": "unknown",
                    "ready_for_synthesis": False,
                }
                changed = True
        if did in directions:
            changed = self._normalize_gap_tree(directions[did]) or changed
        return changed

    @staticmethod
    def _gap_children(steps: dict[str, Any]) -> dict[str, list[str]]:
        children = {str(step_id): [] for step_id in steps}
        for step_id, step in steps.items():
            if not isinstance(step, dict):
                continue
            parent_id = step.get("parent_gap_id")
            if parent_id:
                children.setdefault(str(parent_id), []).append(str(step_id))
        return children

    def _normalize_gap_tree(self, direction: dict[str, Any]) -> bool:
        """Validate and materialize one direction's gap forest in place.

        ``steps`` deliberately remains a flat id-index for compatible atomic updates.  The
        explicit parent links and derived child indexes give it tree semantics without
        forcing fragile nested JSON rewrites.
        """
        steps = direction.setdefault("steps", {})
        if not isinstance(steps, dict):
            raise ValueError("direction.steps must be an object after normalization")
        changed = False
        for step_id, step in steps.items():
            if not isinstance(step, dict):
                raise ValueError(f"step {step_id!r} must be an object")
            sid = _clean_id(step.get("step_id") or step_id, field="step_id")
            if sid != step_id:
                raise ValueError(f"step key {step_id!r} must match step_id {sid!r}")
            parent_raw = step.get("parent_gap_id")
            parent_id = _clean_id(parent_raw, field=f"step {sid}.parent_gap_id") if parent_raw else None
            if parent_id == sid:
                raise ValueError(f"step {sid!r} cannot be its own parent")
            if parent_id and parent_id not in steps:
                raise ValueError(f"step {sid!r} references unknown parent_gap_id {parent_id!r}")
            policy = str(step.get("resolution_policy") or "manual").strip()
            if policy not in _GAP_RESOLUTION_POLICIES:
                raise ValueError(
                    f"invalid resolution_policy for step {sid!r}: {policy!r}; "
                    f"must be one of {sorted(_GAP_RESOLUTION_POLICIES)}"
                )
            kind = str(step.get("gap_kind") or "unknown").strip()
            if kind not in _GAP_KINDS:
                raise ValueError(f"invalid gap_kind for step {sid!r}: {kind!r}")
            desired = {
                "step_id": sid,
                "parent_gap_id": parent_id,
                "resolution_policy": policy,
                "gap_kind": kind,
                "child_gap_ids": [],
                "ready_for_synthesis": bool(step.get("ready_for_synthesis")),
            }
            for key, value in desired.items():
                if step.get(key) != value:
                    step[key] = value
                    changed = True

        # A parent chain must terminate at a root.  This detects every directed cycle.
        for sid in steps:
            seen: set[str] = set()
            cursor: str | None = sid
            while cursor:
                if cursor in seen:
                    raise ValueError(f"cycle detected in gap hierarchy at {cursor!r}")
                seen.add(cursor)
                parent = steps[cursor].get("parent_gap_id")
                cursor = str(parent) if parent else None

        children = self._gap_children(steps)
        for sid, step in steps.items():
            child_ids = sorted(children.get(sid, []))
            if step.get("child_gap_ids") != child_ids:
                step["child_gap_ids"] = child_ids
                changed = True
        return changed

    def _descendant_or_self(self, direction: dict[str, Any], *, ancestor_id: str, gap_id: str) -> bool:
        self._normalize_gap_tree(direction)
        steps = direction.get("steps") or {}
        cursor: str | None = gap_id
        seen: set[str] = set()
        while cursor:
            if cursor == ancestor_id:
                return True
            if cursor in seen or cursor not in steps:
                return False
            seen.add(cursor)
            parent = steps[cursor].get("parent_gap_id")
            cursor = str(parent) if parent else None
        return False

    def gap_is_within_scope(self, *, direction_id: str, scope_gap_id: str, gap_id: str) -> bool:
        state = self.load()
        did = _clean_id(direction_id, field="direction_id")
        scope = _clean_id(scope_gap_id, field="scope_gap_id")
        gid = _clean_id(gap_id, field="gap_id")
        direction = (state.get("directions") or {}).get(did)
        if not isinstance(direction, dict) or scope not in (direction.get("steps") or {}) or gid not in (direction.get("steps") or {}):
            return False
        return self._descendant_or_self(direction, ancestor_id=scope, gap_id=gid)

    def _supersede_descendants(self, steps: dict[str, Any], root_gap_id: str) -> bool:
        changed = False
        children = self._gap_children(steps)
        stack = list(children.get(root_gap_id, []))
        while stack:
            sid = stack.pop()
            step = steps[sid]
            if step.get("status") not in {"closed", "invalidated", "superseded"}:
                step["status"] = "superseded"
                step["ready_for_synthesis"] = False
                changed = True
            stack.extend(children.get(sid, []))
        return changed

    def _propagate_gap_tree(self, direction: dict[str, Any]) -> bool:
        """Derive parent states after an impact without fabricating a proof assembly.

        ``any_of`` may close a parent because a child is explicitly an alternative proof.
        ``all_of`` and ``manual`` only mark the parent ready for synthesis when their
        children are closed; a worker must still prove the parent-level combination.
        """
        changed = self._normalize_gap_tree(direction)
        steps = direction.get("steps") or {}
        children = self._gap_children(steps)
        terminal_statuses = {"closed", "invalidated", "superseded"}
        ordered = sorted(steps, key=lambda sid: self._gap_depth(steps, sid), reverse=True)
        for sid in ordered:
            step = steps[sid]
            child_ids = children.get(sid, [])
            if not child_ids:
                continue
            if step.get("status") == "closed":
                changed = self._supersede_descendants(steps, sid) or changed
                continue
            if step.get("status") in {"invalidated", "superseded"}:
                continue
            policy = step.get("resolution_policy", "manual")
            child_statuses = [str(steps[child].get("status") or "open") for child in child_ids]
            if policy == "any_of":
                if "closed" in child_statuses:
                    step["status"] = "closed"
                    step["ready_for_synthesis"] = False
                    changed = True
                    changed = self._supersede_descendants(steps, sid) or changed
                elif all(status in {"invalidated", "superseded"} for status in child_statuses):
                    step["status"] = "invalidated"
                    step["ready_for_synthesis"] = False
                    changed = True
            elif policy == "all_of":
                if any(status in {"invalidated", "superseded"} for status in child_statuses):
                    step["status"] = "invalidated"
                    step["ready_for_synthesis"] = False
                    changed = True
                elif all(status == "closed" for status in child_statuses):
                    if step.get("status") != "advanced" or not step.get("ready_for_synthesis"):
                        step["status"] = "advanced"
                        step["ready_for_synthesis"] = True
                        changed = True
            elif all(status == "closed" for status in child_statuses):
                if step.get("status") != "advanced" or not step.get("ready_for_synthesis"):
                    step["status"] = "advanced"
                    step["ready_for_synthesis"] = True
                    changed = True
        return changed

    @staticmethod
    def _gap_depth(steps: dict[str, Any], gap_id: str) -> int:
        depth = 0
        cursor = gap_id
        seen: set[str] = set()
        while cursor in steps and cursor not in seen:
            seen.add(cursor)
            parent = steps[cursor].get("parent_gap_id")
            if not parent:
                break
            depth += 1
            cursor = str(parent)
        return depth

    def record_dispatch_constraint(
        self,
        *,
        direction_id: str,
        gap_id: str,
        claim: str,
        status: str,
        evidence_level: str,
        evidence: str,
        source_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """Persist a reviewer/probe finding that controls future targeted dispatch.

        A refuted claim needs an explicit witness, exhaustive finite check, or verified proof;
        sampled evidence can require falsification first but never hard-refute a target.
        """
        did = _clean_id(direction_id, field="direction_id")
        gid = _clean_id(gap_id, field="gap_id")
        normalized_status = str(status or "").strip()
        normalized_level = str(evidence_level or "").strip()
        if normalized_status not in _DISPATCH_CONSTRAINT_STATUSES:
            raise ValueError(
                f"invalid dispatch constraint status: {normalized_status}. "
                f"Must be one of {sorted(_DISPATCH_CONSTRAINT_STATUSES)}"
            )
        if normalized_level not in _DISPATCH_EVIDENCE_LEVELS:
            raise ValueError(
                f"invalid dispatch evidence_level: {normalized_level}. "
                f"Must be one of {sorted(_DISPATCH_EVIDENCE_LEVELS)}"
            )
        if normalized_status == "refuted" and normalized_level not in {
            "verified", "explicit_witness", "exhaustive_finite",
        }:
            raise ValueError(
                "a refuted dispatch constraint requires verified, explicit_witness, or exhaustive_finite evidence"
            )

        state = self.load()
        self._ensure_target_in_state(state, direction_id=did, gap_id=gid, hint=claim)
        paths = _string_list(source_paths, field="source_paths", max_items=12)
        key = f"{did}/{gid}"
        constraint = {
            "direction_id": did,
            "gap_id": gid,
            "claim": _clean_text(claim, field="claim", max_length=2000),
            "status": normalized_status,
            "evidence_level": normalized_level,
            "evidence": _clean_text(evidence, field="evidence", max_length=4000),
            "source_paths": paths,
            "recorded_at": _now_iso(),
        }
        state.setdefault("dispatch_constraints", {})[key] = constraint
        if normalized_status == "refuted":
            state["directions"][did]["steps"][gid]["status"] = "invalidated"
            state["directions"][did]["steps"][gid]["ready_for_synthesis"] = False
            self._propagate_gap_tree(state["directions"][did])
        state["updated_at"] = _now_iso()
        self._save(state)
        return {"recorded": True, "constraint": constraint, **self.summary(state)}

    def materialize_audit_blocker(
        self,
        *,
        checkpoint_id: str,
        direction_id: str,
        gap_id: str,
        statement: str,
        audit_path: str,
        occurrence_count: int | None = None,
        approaches: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Turn a repeated avoided obligation into the one active gap for its direction."""
        did = _clean_id(direction_id, field="direction_id")
        gid = _clean_id(gap_id, field="gap_id")
        normalized_count = 0 if occurrence_count is None else _clean_positive_int(
            occurrence_count, field="blocker.occurrence_count"
        )
        normalized_approaches = _normalize_audit_blocker_approaches(
            approaches, occurrence_count=normalized_count
        ) if approaches is not None else []
        state = self.load()
        self._ensure_target_in_state(state, direction_id=did, gap_id=gid, hint=statement)
        direction = state["directions"][did]
        step = direction["steps"][gid]
        step["statement"] = _clean_text(statement, field="blocker.statement", max_length=2000)
        step["status"] = "open"
        blocker = {
            "checkpoint_id": _clean_text(checkpoint_id, field="checkpoint_id", max_length=120),
            "direction_id": did,
            "gap_id": gid,
            "statement": step["statement"],
            "occurrence_count": normalized_count,
            "approaches": normalized_approaches,
            "audit_path": str(audit_path or "").strip()[:1000],
            "status": "active",
            "materialized_at": _now_iso(),
        }
        existing = (state.setdefault("audit_blockers", {}) or {}).get(did)
        if isinstance(existing, dict) and existing.get("checkpoint_id") == blocker["checkpoint_id"]:
            blocker["materialized_at"] = existing.get("materialized_at") or blocker["materialized_at"]
        state["audit_blockers"][did] = blocker
        state["updated_at"] = _now_iso()
        self._save(state)
        return {"materialized": True, "blocker": blocker, **self.summary(state)}

    def active_audit_blocker(self, direction_id: str | None) -> dict[str, Any] | None:
        if not direction_id:
            return None
        return (self.load().get("audit_blockers") or {}).get(str(direction_id)) or None

    def dispatch_preflight(
        self,
        *,
        direction_id: str | None,
        gap_id: str | None,
        method_id: str | None,
        blocker_override_reason: str | None = None,
    ) -> dict[str, Any]:
        """Return a deterministic dispatch decision from persisted counterexample evidence."""
        if not direction_id or not gap_id:
            return {"allowed": True}
        did = _clean_id(direction_id, field="direction_id")
        gid = _clean_id(gap_id, field="gap_id")
        state = self.load()
        direction = (state.get("directions") or {}).get(did) or {}
        if isinstance(direction, dict):
            self._normalize_gap_tree(direction)
        steps = direction.get("steps") or {}
        step = steps.get(gid) or {}
        blocker = (state.get("audit_blockers") or {}).get(did)
        override = str(blocker_override_reason or "").strip()
        if isinstance(blocker, dict) and blocker.get("status") == "active" and blocker.get("gap_id") != gid and not override:
            return {
                "allowed": False,
                "reason": "active_audit_blocker",
                "blocker": blocker,
                "message": (
                    "Process audit identified a repeated avoided obligation for this direction. Dispatch its blocker "
                    "gap directly, or provide blocker_override_reason with concrete evidence for an explicit pivot."
                ),
            }
        constraint = (state.get("dispatch_constraints") or {}).get(f"{did}/{gid}")
        if isinstance(constraint, dict):
            status = str(constraint.get("status") or "")
            if status == "refuted":
                return {
                    "allowed": False,
                    "reason": "target_refuted",
                    "constraint": constraint,
                    "message": (
                        "This exact target has reproducible refutation evidence. Create a new gap for the revised "
                        "claim; do not re-dispatch the refuted claim."
                    ),
                }
            if status == "needs_falsification" and method_id != "falsification":
                return {
                    "allowed": False,
                    "reason": "falsification_required",
                    "constraint": constraint,
                    "message": (
                        "This target is supported only by sampled or incomplete finite evidence. Run a dedicated "
                        "falsification worker (method_id='falsification') before a proof attempt."
                    ),
                }
        step_status = str(step.get("status") or "open")
        if step_status in {"closed", "superseded"}:
            return {
                "allowed": False,
                "reason": "gap_not_schedulable",
                "message": f"This gap is {step_status}; dispatch an open descendant or create a revised gap instead.",
            }
        if step_status == "invalidated":
            return {
                "allowed": False,
                "reason": "gap_invalidated",
                "message": "This gap is invalidated in research_state; create a new target before dispatching.",
            }
        child_ids = self._gap_children(steps).get(gid, [])
        if child_ids:
            if bool(step.get("ready_for_synthesis")):
                if method_id != "consolidation":
                    return {
                        "allowed": False,
                        "reason": "gap_synthesis_required",
                        "gap_id": gid,
                        "child_gap_ids": child_ids,
                        "message": (
                            "All required child gaps are resolved. Run a consolidation worker on this parent to prove "
                            "the explicit assembly; do not reopen its children."
                        ),
                    }
            else:
                open_children = [
                    child_id for child_id in child_ids
                    if str(steps[child_id].get("status") or "open") not in {"closed", "invalidated", "superseded"}
                ]
                return {
                    "allowed": False,
                    "reason": "gap_has_open_children",
                    "gap_id": gid,
                    "child_gap_ids": child_ids,
                    "open_child_gap_ids": open_children,
                    "message": (
                        "This is a decomposed parent gap. Dispatch an executable child gap first; only dispatch the "
                        "parent with consolidation=true after it is ready for synthesis."
                    ),
                }
        for failure in direction.get("failed_methods") or []:
            if (
                failure.get("failure_type") == "conclusion_refuted"
                and (not failure.get("gap_id") or failure.get("gap_id") == gid)
            ):
                return {
                    "allowed": False,
                    "reason": "conclusion_refuted",
                    "failure": failure,
                    "message": (
                        "A counterexample previously refuted this target. Create a revised gap instead of retrying "
                        "the same conclusion."
                    ),
                }
        return {"allowed": True}

    def _append_route_outcome(
        self,
        state: dict[str, Any],
        *,
        route_id: str | None,
        worker_id: str,
        status: str,
        summary: str,
        relation: str | None = None,
        gap_effect: str | None = None,
    ) -> None:
        if not route_id:
            return
        rid = _clean_id(route_id, field="route_id")
        route = (state.get("routes") or {}).get(rid)
        if not isinstance(route, dict):
            raise ValueError(f"unknown route_id: {rid}")
        outcomes = route.setdefault("outcomes", [])
        if any(item.get("worker_id") == worker_id for item in outcomes if isinstance(item, dict)):
            return
        outcomes.append({
            "worker_id": worker_id,
            "status": status,
            "relation_to_target": relation,
            "gap_effect": gap_effect,
            "summary": summary[:2000],
            "recorded_at": _now_iso(),
        })
        route["updated_at"] = _now_iso()

    def record_worker_outcome(
        self,
        *,
        worker_id: str,
        direction_id: str,
        gap_id: str | None,
        status: str,
        failure_kind: str | None,
        summary: str,
        method_id: str | None = None,
        route_id: str | None = None,
    ) -> dict[str, Any]:
        """持久记录未进入 ResearchImpact 的失败/拒绝结果，并更新方向止损计数。"""
        wid = _clean_id(worker_id, field="worker_id")
        did = _clean_id(direction_id, field="direction_id")
        state = self.load()
        outcomes = state.setdefault("outcomes", {})
        if wid in outcomes or wid in state.setdefault("impacts", {}):
            return {"recorded": False, "reason": "worker outcome already recorded", **self.summary(state)}
        self._ensure_target_in_state(state, direction_id=did, gap_id=gap_id, hint=summary or status)
        direction = state["directions"][did]
        progress = direction.setdefault("progress", self._empty_progress())
        progress["attempts"] = int(progress.get("attempts", 0)) + 1
        progress["failed_outputs"] = int(progress.get("failed_outputs", 0)) + 1
        protocol_failure = failure_kind == "generator_protocol_failure"
        if protocol_failure:
            progress["protocol_failures"] = int(progress.get("protocol_failures", 0)) + 1
        progress["last_impact"] = summary[:4000]
        # 失败也追加到 step 的 results，让 orchestrator 能看到"这个 step 试过了但失败了"
        gid = _clean_id(gap_id, field="gap_id") if gap_id else None
        if gid:
            steps = direction.setdefault("steps", {})
            if gid not in steps:
                steps[gid] = {"step_id": gid, "statement": (summary or status).strip(), "status": "open", "method": str(method_id or "").strip(), "results": []}
            steps[gid]["results"].append({
                "worker_id": wid,
                "relation": "execution_failed",
                "summary": str(summary or "")[:2000],
                "failure_kind": str(failure_kind or "failed"),
            })
        record = {
            "worker_id": wid,
            "direction_id": did,
            "gap_id": (_clean_id(gap_id, field="gap_id") if gap_id else None),
            "method_id": str(method_id or "").strip() or None,
            "status": str(status or "failed"),
            "failure_kind": str(failure_kind or "failed"),
            "summary": str(summary or "")[:4000],
            "recorded_at": _now_iso(),
        }
        outcomes[wid] = record
        self._append_route_outcome(
            state,
            route_id=route_id,
            worker_id=wid,
            status=record["status"],
            summary=record["summary"],
        )
        state["updated_at"] = _now_iso()
        self._save(state)
        return {"recorded": True, "outcome": record, **self.summary(state)}

    # 禁忌搜索：记录方法失败，区分"思路错"vs"结论错"。
    # 由编排层在 worker 失败后判断调用——不是自动的，需要 orchestrator 的语义判断。
    def record_method_failure(
        self,
        *,
        direction_id: str,
        method_family: str,
        failure_type: str,
        summary: str,
        gap_id: str | None = None,
    ) -> dict[str, Any]:
        """记录一次方法失败到方向的 ``failed_methods`` 禁忌列表。

        - ``method_blocked``: 该方法无法达到目标（思路错），但目标可能正确。
          → 保留 goal，后续 spawn 必须用不同方法族。
        - ``conclusion_refuted``: 目标本身可能为假（结论错）。
          → 编排层应考虑标记方向 refuted 或更换目标。

        该方法**不修改 goal**——禁忌搜索的核心原则是 goal 可以不变，只换方法。
        编排层随后可通过 ``SyncResearchState`` refine ``plan``（方法路线），
        同时保留 ``goal`` 不变。
        """
        did = _clean_id(direction_id, field="direction_id")
        if failure_type not in _METHOD_FAILURE_TYPES:
            raise ValueError(
                f"invalid failure_type: {failure_type}. Must be one of {sorted(_METHOD_FAILURE_TYPES)}"
            )
        state = self.load()
        self._ensure_target_in_state(state, direction_id=did, gap_id=gap_id, hint=summary)
        direction = state["directions"][did]
        failed_methods = direction.setdefault("failed_methods", [])
        # 去重：同一 method_family 已有记录则更新 summary，不重复追加。
        existing = next((fm for fm in failed_methods if fm.get("method_family") == method_family), None)
        entry = {
            "method_family": method_family,
            "failure_type": failure_type,
            "summary": str(summary)[:2000],
            "gap_id": (_clean_id(gap_id, field="gap_id") if gap_id else None),
            "recorded_at": _now_iso(),
        }
        if existing:
            existing.update(entry)
        else:
            failed_methods.append(entry)
        direction["failed_methods"] = failed_methods
        state["updated_at"] = _now_iso()
        self._save(state)
        return {
            "recorded": True,
            "direction_id": did,
            "method_family": method_family,
            "failure_type": failure_type,
            "failed_methods_count": len(failed_methods),
            **self.summary(state),
        }

    def record_impact(
        self,
        *,
        worker_id: str,
        direction_id: str,
        gap_id: str | None,
        impact: dict[str, Any],
        route_id: str | None = None,
    ) -> dict[str, Any]:
        wid = _clean_id(worker_id, field="worker_id")
        did = _clean_id(direction_id, field="direction_id")
        relation = str(impact.get("relation_to_target") or "").strip()
        gap_effect = str(impact.get("gap_effect") or "").strip()
        continuation = str(impact.get("continuation_value") or "").strip()
        if relation not in _RELATIONS:
            raise ValueError(f"invalid relation_to_target: {relation}")
        if gap_effect not in _GAP_EFFECTS:
            raise ValueError(f"invalid gap_effect: {gap_effect}")
        if continuation not in _CONTINUATION_VALUES:
            raise ValueError(f"invalid continuation_value: {continuation}")
        # 兑现/对偶尝试的可选结局；普通 impact 不带此字段，行为不变。
        consolidation_outcome = str(impact.get("consolidation_outcome") or "").strip() or None
        if consolidation_outcome is not None and consolidation_outcome not in _CONSOLIDATION_OUTCOMES:
            raise ValueError(f"invalid consolidation_outcome: {consolidation_outcome}")
        # 该结果是否真正卸载了不可能性方向的 standing_hypothesis（诚实 advance 判据）。
        discharges_hypothesis = bool(impact.get("discharges_standing_hypothesis"))
        disallowed = _DISALLOWED_GAP_EFFECTS_FOR_RELATION.get(relation, set())
        if gap_effect in disallowed:
            raise ValueError(
                f"gap_effect={gap_effect!r} is not consistent with relation_to_target={relation!r}; "
                f"a {relation} result cannot report gap_effect in {sorted(disallowed)}"
            )
        summary = _clean_text(impact.get("summary"), field="summary", max_length=4000)
        rubric_assessment = _normalize_rubric_assessment(impact.get("rubric_assessment"))
        closed_gap_ids = [_clean_id(item, field="closed_gap_ids item") for item in _string_list(
            impact.get("closed_gap_ids"), field="closed_gap_ids"
        )]
        if closed_gap_ids and relation not in _POSITIVE_RELATIONS:
            raise ValueError(
                f"closed_gap_ids requires relation_to_target in {sorted(_POSITIVE_RELATIONS)}, got {relation!r}"
            )
        remaining_gaps = _string_list(impact.get("remaining_gaps"), field="remaining_gaps")
        new_gaps = impact.get("new_gaps") or []
        if not isinstance(new_gaps, list):
            raise ValueError("new_gaps must be an array")
        child_resolution_policy = str(impact.get("child_resolution_policy") or "").strip() or None
        if child_resolution_policy is not None and child_resolution_policy not in _GAP_RESOLUTION_POLICIES:
            raise ValueError(
                f"invalid child_resolution_policy: {child_resolution_policy!r}; "
                f"must be one of {sorted(_GAP_RESOLUTION_POLICIES)}"
            )

        # Single load/modify/save transaction: everything below operates on this one `state`
        # object and its nested dicts by reference, then a single `_save(state)` persists all
        # of it atomically. Do NOT re-`load()` partway through — a second load() returns a
        # fresh dict, and writes made to references taken before that point (e.g. `impacts`)
        # would silently target a stale object that is never saved.
        state = self.load()
        impacts = state.setdefault("impacts", {})
        if wid in impacts:
            return {"recorded": False, "reason": "worker impact already recorded", **self.summary(state)}

        # global-problem-attack 是无状态的"诚实探针"：它的价值就是给编排层一个不带方法框架
        # 偏见的 outer-loop 反馈（当前累积的验证成果是否已经能拿下整题），不是一个需要追踪
        # dual_probe_required / deprioritized / stalled 的持久研究方向。任何撞墙结果都只是
        # "还没到"的信息，不应触发下面的对偶探针强制门控——否则会把这个诚实反馈机制自己变成
        # 一个必须反复重打、且被 GLOBAL_ATTACK_HINT 方法中性覆盖锁死无法真正执行对偶探针的死循环。
        if did == "global-problem-attack":
            impacts[wid] = {
                "worker_id": wid,
                "direction_id": did,
                "relation_to_target": relation,
                "consolidation_outcome": consolidation_outcome,
                "summary": summary[:2000],
                "recorded_at": _now_iso(),
            }
            state["global_consolidation"] = {
                "last_verified_count": self._count_verified_props(),
                "last_worker_id": wid,
                "last_attempted_at": _now_iso(),
                "last_relation_to_target": relation,
            }
            state["updated_at"] = _now_iso()
            self._save(state)
            return {"recorded": True, "direction_id": did, "global_attack": True, **self.summary(state)}

        self._ensure_target_in_state(state, direction_id=did, gap_id=gap_id, hint=summary)
        direction = state["directions"][did]
        self._normalize_gap_tree(direction)
        steps = direction.setdefault("steps", {})
        gid = _clean_id(gap_id, field="gap_id") if gap_id else None
        if gid and gid not in steps:
            raise ValueError(f"worker target gap does not exist: {gid}")

        result_entry = {
            "worker_id": wid,
            "relation": relation,
            "summary": summary[:2000],
        }
        for closed_id in closed_gap_ids:
            if closed_id not in steps:
                raise ValueError(f"closed_gap_ids references unknown gap {closed_id!r}")
            steps[closed_id]["status"] = "closed"
            steps[closed_id]["ready_for_synthesis"] = False
            steps[closed_id]["results"].append(result_entry)
        if gid:
            steps[gid]["status"] = gap_effect if gap_effect in _GAP_STATUSES else "open"
            steps[gid]["ready_for_synthesis"] = False
            steps[gid]["results"].append(result_entry)

        # Newly discovered obligations are children of the worker's target by default.
        # A worker may explicitly attach a reusable obligation elsewhere in the same
        # direction, but cycles and unknown parents are rejected by _normalize_gap_tree.
        for index, raw_gap in enumerate(new_gaps, start=1):
            if isinstance(raw_gap, dict):
                new_id = _clean_id(raw_gap.get("gap_id") or raw_gap.get("step_id"), field="new_gaps.step_id")
                statement = _clean_text(raw_gap.get("statement"), field="new_gaps.statement")
                parent_raw = raw_gap.get("parent_gap_id")
                parent_id = _clean_id(parent_raw, field="new_gaps.parent_gap_id") if parent_raw else gid
                policy = str(raw_gap.get("resolution_policy") or "manual").strip()
                kind = str(raw_gap.get("gap_kind") or "unknown").strip()
            else:
                new_id = f"{did}-new-{len(steps) + index}"
                statement = _clean_text(raw_gap, field="new_gaps item")
                parent_id = gid
                policy = "manual"
                kind = "unknown"
            if new_id in steps:
                raise ValueError(f"new_gaps reuses existing gap id {new_id!r}")
            if parent_id and parent_id not in steps:
                raise ValueError(f"new_gaps references unknown parent_gap_id {parent_id!r}")
            if policy not in _GAP_RESOLUTION_POLICIES:
                raise ValueError(f"invalid new_gaps resolution_policy: {policy!r}")
            if kind not in _GAP_KINDS:
                raise ValueError(f"invalid new_gaps gap_kind: {kind!r}")
            steps[new_id] = {
                "step_id": new_id,
                "statement": statement,
                "status": "open",
                "method": "",
                "results": [],
                "parent_gap_id": parent_id,
                "child_gap_ids": [],
                "resolution_policy": policy,
                "gap_kind": kind,
                "ready_for_synthesis": False,
            }
        if gid and new_gaps and child_resolution_policy is not None:
            steps[gid]["resolution_policy"] = child_resolution_policy
        self._propagate_gap_tree(direction)

        # ---- 诚实 advance 校验（仅 impossibility 方向）----
        # 一个"仍挂着 standing_hypothesis、既没卸载它、也没闭合 terminal_gap"的结果，
        # 不能算正向进展——它只是又刨出一条必要条件。强制降级为 necessary_condition_only，
        # 让 weak_streak 如实累加并触发既有的 stalled 机制（这正是本类死线此前被漏判的根因）。
        downgraded = False
        if direction.get("direction_kind") == "impossibility" and relation in {
            "sufficient_for_target",
            "direct_advance",
        }:
            terminal_gid = str(direction.get("terminal_gap_id") or "").strip()
            closes_terminal = bool(terminal_gid) and (
                terminal_gid in closed_gap_ids or (gid == terminal_gid and gap_effect == "closed")
            )
            if not (discharges_hypothesis or closes_terminal):
                relation = "necessary_condition_only"
                downgraded = True

        progress = direction.setdefault("progress", self._empty_progress())
        progress["attempts"] += 1
        if bool(impact.get("verified_output", True)):
            progress["verified_outputs"] = int(progress.get("verified_outputs", 0)) + 1
        if relation == "refutes_target":
            progress["refutations"] = int(progress.get("refutations", 0)) + 1
        elif relation == "solves_target":
            progress["target_solves"] = int(progress.get("target_solves", 0)) + 1
        progress["last_impact"] = summary
        progress["remaining_gaps"] = remaining_gaps

        # A worker relation is relative to its assigned gap, not necessarily the whole
        # direction.  Reduce only the terminal/root node after recursive propagation; a
        # solved or refuted child must leave sibling subproblems schedulable.
        terminal_gap_id = str(direction.get("terminal_gap_id") or "").strip()
        roots = sorted(step_id for step_id, step in steps.items() if not step.get("parent_gap_id"))
        terminal_gap_id = terminal_gap_id or (roots[0] if len(roots) == 1 else "")
        if terminal_gap_id in steps:
            terminal_status = str(steps[terminal_gap_id].get("status") or "open")
            if terminal_status == "closed":
                direction["status"] = "completed"
                direction["health"] = "completed"
            elif terminal_status == "invalidated":
                direction["status"] = "refuted"
                direction["health"] = "refuted"

        # ---- 兑现（consolidation）/对偶尝试的三态反馈 ----
        # solved     由 relation=solves_target 走上面的 completed 分支即可；
        # blocked_gap 定位到可攻缺口（通常已随 new_gaps 落库），视为正常推进，不额外降权；
        # wall       反复撞墙/拼不出通路 → 疑似目标为假：置 dual_probe_required 并降权，
        #            强制编排层先派一个对偶/证伪探针（找见证），其回报若 refutes_target 则
        #            整条方向 refuted 并触发全线复盘；若未证伪则赦免恢复。
        if consolidation_outcome is not None:
            # 回填送审日志：按 worker_id 找到 pending 的送审记录填入返回；找不到则补一条 return-only。
            log = direction.setdefault("consolidation_log", [])
            matched = None
            for existing in reversed(log):
                if existing.get("worker_id") and existing.get("worker_id") == wid and existing.get("outcome") == "pending":
                    matched = existing
                    break
            if matched is None:
                matched = {
                    "worker_id": wid,
                    "kind": "consolidation",
                    "gap_id": gid,
                    "pinned_target": None,
                    "submitted_at": "",
                }
                log.append(matched)
                del log[:-self._CONSOLIDATION_LOG_MAX]
            matched["outcome"] = consolidation_outcome
            matched["relation_to_target"] = relation
            matched["returned_summary"] = summary[:600]
            matched["returned_at"] = _now_iso()
            if consolidation_outcome == "wall":
                direction["dual_probe_required"] = True
                direction["deprioritized"] = True
                if direction["status"] not in {"refuted", "completed"}:
                    direction["status"] = "stalled"
                    direction["health"] = "under_challenge"
            else:
                # 兑现/对偶尝试已返回且未撞墙(solved/blocked_gap)：解除待办、恢复预算，避免死循环。
                direction["dual_probe_required"] = False
                direction["deprioritized"] = False
        # 对偶探针一旦证伪，方向已在上面被置 refuted；顺带清掉待办标志。
        if relation == "refutes_target":
            direction["dual_probe_required"] = False

        record = {
            "worker_id": wid,
            "direction_id": did,
            "gap_id": gid,
            "relation_to_target": relation,
            "gap_effect": gap_effect,
            "closed_gap_ids": closed_gap_ids,
            "remaining_gaps": remaining_gaps,
            "new_gaps": deepcopy(new_gaps),
            "child_resolution_policy": child_resolution_policy,
            "continuation_value": continuation,
            "requires_direction_review": bool(impact.get("requires_direction_review")),
            "rubric_assessment": rubric_assessment,
            "consolidation_outcome": consolidation_outcome,
            "discharges_standing_hypothesis": discharges_hypothesis,
            "downgraded_to_necessary_only": downgraded,
            "summary": summary,
            "recorded_at": _now_iso(),
        }
        impacts[wid] = record
        self._append_route_outcome(
            state,
            route_id=route_id,
            worker_id=wid,
            status="assessed",
            relation=relation,
            gap_effect=gap_effect,
            summary=summary,
        )
        blocker = (state.get("audit_blockers") or {}).get(did)
        if isinstance(blocker, dict) and blocker.get("gap_id") == gid and (
            gap_effect == "closed" or relation == "refutes_target"
        ):
            blocker["status"] = "resolved"
            blocker["resolved_by_worker_id"] = wid
            blocker["resolved_at"] = _now_iso()
        state["updated_at"] = _now_iso()
        self._save(state)
        return {"recorded": True, "impact": record, **self.summary(state)}

    def sync_invalidated_from_knowledge(
        self, knowledge_dir: Path
    ) -> dict[str, Any]:
        """扫描 knowledge/ 目录中标注了 INVALIDATED / false / refuted 的文件，
        自动将引用了这些死路文件的 gap 的 status 设为 invalidated。

        这解决了 knowledge 里的死路标注无法反馈到 gap status 的问题——
        之前 worker 会继续在已废方向上工作，因为 orchestrator 看到 gap 还是 open。

        匹配策略：扫描 knowledge 文件中的 direction_id / gap_id 引用，
        以及文件名关键词与 gap.statement 的模糊匹配。
        """
        if not knowledge_dir.is_dir():
            return {"synced": False, "reason": "knowledge_dir not found"}

        state = self.load()
        directions = state.get("directions", {})
        invalidated_gaps: list[str] = []

        # 收集所有 knowledge 文件中的死路标注
        dead_end_gap_ids: set[str] = set()
        dead_end_direction_ids: set[str] = set()

        for root, _dirs, files in os.walk(knowledge_dir):
            for fname in files:
                if not fname.endswith(".md"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, encoding="utf-8") as fh:
                        content = fh.read(16384)
                except (OSError, UnicodeDecodeError):
                    continue
                content_lower = content.lower()

                is_dead = any(
                    marker in content_lower
                    for marker in (
                        "⚠️ critical",
                        "invalidated",
                        "is false",
                        "refuted",
                        "❌",
                    )
                )
                if not is_dead:
                    continue

                # 尝试从文件内容中提取 direction_id / gap_id
                for pattern in (
                    r'direction[_-]id["\']?\s*[:=]\s*["\']?([A-Za-z0-9._-]+)',
                    r'gap[_-]id["\']?\s*[:=]\s*["\']?([A-Za-z0-9._-]+)',
                ):
                    for m in re.finditer(pattern, content):
                        ident = m.group(1).strip()
                        if ident and not ident.startswith("__"):
                            dead_end_gap_ids.add(ident)
                            dead_end_direction_ids.add(ident)

        if not dead_end_gap_ids and not dead_end_direction_ids:
            return {"synced": False, "reason": "no invalidated markers with IDs found"}

        # 同步 gap status
        changed = False
        for did, direction in directions.items():
            if did in dead_end_direction_ids:
                # 整个 direction 标记为 invalidated
                if direction.get("status") == "active":
                    direction["status"] = "paused"
                    direction["health"] = "stalled"
                    changed = True
                    invalidated_gaps.append(f"direction:{did}")
            for sid, step in direction.get("steps", {}).items():
                if sid in dead_end_gap_ids and step.get("status") not in (
                    "closed",
                    "invalidated",
                ):
                    step["status"] = "invalidated"
                    changed = True
                    invalidated_gaps.append(f"{did}/{sid}")

        if changed:
            state["updated_at"] = _now_iso()
            self._save(state)

        return {
            "synced": changed,
            "invalidated": invalidated_gaps,
            "dead_end_ids_found": sorted(dead_end_gap_ids | dead_end_direction_ids),
        }

    def summary(self, state: dict[str, Any] | None = None) -> dict[str, Any]:
        value = state or self.load()
        directions = value.get("directions", {})
        routes = value.get("routes") if isinstance(value.get("routes"), dict) else {}
        return {
            "state_id": value.get("state_id", "state-0"),
            "state_revision": int(value.get("state_revision") or 0),
            "state_file": str(self.markdown_path),
            "canonical_file": str(self.json_path),
            "direction_count": len(directions),
            "active_route_count": sum(1 for route in routes.values() if route.get("status") == "active"),
            "directions": [
                {
                    "direction_id": item["direction_id"],
                    "status": item["status"],
                    "health": item["health"],
                    "direction_kind": item.get("direction_kind", "unknown"),
                    "dual_probe_required": bool(item.get("dual_probe_required")),
                    "deprioritized": bool(item.get("deprioritized")),
                    "open_step_count": sum(1 for step in item.get("steps", {}).values() if step.get("status") != "closed"),
                    "progress": item.get("progress", {}),
                }
                for item in directions.values()
            ],
            "dual_probe_required_directions": [
                item["direction_id"] for item in directions.values() if item.get("dual_probe_required")
            ],
            "consolidation_ready_directions": self.consolidation_readiness(value),
            "global_consolidation": self._global_consolidation_status(value),
        }

    def _global_consolidation_status(self, state: dict[str, Any] | None = None) -> dict[str, Any]:
        """全局 consolidation 状态：当 verified props 总数自上次全局 consolidation 以来
        增长超过阈值时，建议 orchestrator 发起一次直接攻击 problem.md 的 consolidation。"""
        value = state or self.load()
        current_total = self._count_verified_props()
        global_state = value.get("global_consolidation", {})
        last_count = int(global_state.get("last_verified_count", 0))
        last_worker_id = str(global_state.get("last_worker_id") or "").strip() or None
        last_at = str(global_state.get("last_attempted_at") or "").strip() or None
        delta = max(0, current_total - last_count)
        ready = delta >= self.GLOBAL_CONSOLIDATION_EVERY_N_PROPS
        return {
            "ready": ready,
            "current_verified_props": current_total,
            "last_consolidation_count": last_count,
            "delta": delta,
            "threshold": self.GLOBAL_CONSOLIDATION_EVERY_N_PROPS,
            "last_worker_id": last_worker_id,
            "last_attempted_at": last_at,
        }

    def record_global_consolidation(self, worker_id: str) -> None:
        """记录一次全局 consolidation 已完成（或已提交），更新基线计数。"""
        state = self.load()
        current = self._count_verified_props()
        state["global_consolidation"] = {
            "last_verified_count": current,
            "last_worker_id": str(worker_id).strip(),
            "last_attempted_at": _now_iso(),
        }
        state["updated_at"] = _now_iso()
        self._save(state)

    _CONSOLIDATION_LOG_MAX = 20

    def _count_verified_props(self) -> int:
        """全局已验证 props 总数（排除 index.md / state.md）。跨进程/跨轮持久，因此
        "自上次送审到现在积累了多少"能正确认下已有的存量，而不是从 0 重新数。"""
        try:
            if not self.verified_dir.exists():
                return 0
            return sum(
                1
                for path in self.verified_dir.rglob("*.md")
                if path.is_file() and path.name not in {"index.md", "state.md"}
            )
        except OSError:
            return 0

    def log_consolidation_submission(
        self,
        *,
        direction_id: str | None,
        gap_id: str | None = None,
        worker_id: str | None = None,
        pinned_target: str | None = None,
        dual_probe: bool = False,
    ) -> None:
        """记录一次"送审"（发起兑现/对偶尝试）。独立 load/modify/save，供编排层在
        SpawnWorker(consolidation=true) 成功后调用；返回结果由 record_impact 按 worker_id 回填。"""
        if not direction_id:
            return
        did = _clean_id(direction_id, field="direction_id")
        state = self.load()
        self._ensure_target_in_state(state, direction_id=did, gap_id=gap_id, hint=pinned_target)
        direction = state["directions"][did]
        log = direction.setdefault("consolidation_log", [])
        entry = {
            "worker_id": (str(worker_id).strip() or None) if worker_id else None,
            "kind": "dual_probe" if dual_probe else "consolidation",
            "gap_id": (_clean_id(gap_id, field="gap_id") if gap_id else None),
            "pinned_target": (str(pinned_target or "").strip()[:600] or None),
            "submitted_at": _now_iso(),
            "outcome": "pending",
            "returned_summary": "",
            "returned_at": "",
        }
        log.append(entry)
        del log[:-self._CONSOLIDATION_LOG_MAX]
        state["updated_at"] = _now_iso()
        self._save(state)

    def consolidation_readiness(self, state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """兑现（consolidation）时机：per-direction 硬规则已移除。

        之前会在单个方向累计 >= threshold verified props 时 hard-gate 该方向，
        阻止 orchestrator 探索其他方向。现在只保留全局 consolidation（见
        ``_global_consolidation_status`` / ``GLOBAL_CONSOLIDATION_EVERY_N_PROPS``）。
        本方法始终返回空列表。
        """
        return []

    def _normalize_direction(self, raw: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
        did = _clean_id(raw.get("direction_id"), field="direction_id")
        status = str(raw.get("status") or "active").strip()
        health = str(raw.get("health") or "unassessed").strip()
        if status not in _DIRECTION_STATUSES:
            raise ValueError(f"invalid direction status: {status}")
        if health not in _DIRECTION_HEALTH:
            raise ValueError(f"invalid direction health: {health}")
        direction_kind = str(raw.get("direction_kind") or (previous or {}).get("direction_kind") or "unknown").strip()
        if direction_kind not in _DIRECTION_KINDS:
            raise ValueError(f"invalid direction_kind: {direction_kind}")
        # steps 合并了原来的 gaps + plan：每个 step 既是计划步骤又是待证 gap。
        # orchestrator 通过 SyncResearchState 定义/refine steps；RecordResearchImpact 更新状态和追加 results。
        steps: dict[str, Any] = {}
        raw_steps = raw.get("steps")
        # 向后兼容：如果传了旧的 gaps + plan，合并成 steps
        if raw_steps is None:
            raw_gaps = raw.get("gaps") or []
            raw_plan = raw.get("plan") or []
            raw_steps = []
            for g in raw_gaps:
                if isinstance(g, dict):
                    raw_steps.append(g)
            for idx, p in enumerate(raw_plan):
                if isinstance(p, str) and p.strip():
                    raw_steps.append({"step_id": f"plan-{idx}", "statement": p, "status": "open"})
        if not isinstance(raw_steps, list):
            raise ValueError("direction.steps must be an array")
        for raw_step in raw_steps:
            if not isinstance(raw_step, dict):
                raise ValueError("each step must be an object")
            sid = _clean_id(raw_step.get("step_id") or raw_step.get("gap_id"), field="step_id")
            step_status = str(raw_step.get("status") or "open").strip()
            if step_status not in _GAP_STATUSES:
                raise ValueError(f"invalid step status: {step_status}")
            parent_raw = raw_step.get("parent_gap_id")
            policy = str(raw_step.get("resolution_policy") or "manual").strip()
            if policy not in _GAP_RESOLUTION_POLICIES:
                raise ValueError(f"invalid resolution_policy for step {sid!r}: {policy}")
            kind = str(raw_step.get("gap_kind") or "unknown").strip()
            if kind not in _GAP_KINDS:
                raise ValueError(f"invalid gap_kind for step {sid!r}: {kind}")
            steps[sid] = {
                "step_id": sid,
                "statement": _clean_text(raw_step.get("statement"), field="step.statement"),
                "status": step_status,
                "method": str(raw_step.get("method") or "").strip(),
                "results": list(raw_step.get("results") or []),
                "parent_gap_id": _clean_id(parent_raw, field=f"step {sid}.parent_gap_id") if parent_raw else None,
                "child_gap_ids": [],
                "resolution_policy": policy,
                "gap_kind": kind,
                "ready_for_synthesis": bool(raw_step.get("ready_for_synthesis")),
            }
        if previous:
            # 保留 previous 中已闭合/进阶的 step（orchestrator 可能省略它们）
            prev_steps = previous.get("steps", {})
            # 也兼容旧格式 previous.get("gaps")
            if not prev_steps:
                prev_gaps = previous.get("gaps", {})
                prev_plan = previous.get("plan", [])
                if isinstance(prev_gaps, dict):
                    previous_gap_items = prev_gaps.items()
                elif isinstance(prev_gaps, list):
                    previous_gap_items = (
                        (str(item.get("step_id") or item.get("gap_id") or ""), item)
                        for item in prev_gaps if isinstance(item, dict)
                    )
                else:
                    previous_gap_items = []
                for gid, g in previous_gap_items:
                    if gid and isinstance(g, dict):
                        prev_steps[gid] = {**g, "step_id": gid, "method": "", "results": []}
                for idx, p in enumerate(prev_plan):
                    sid = f"plan-{idx}"
                    if sid not in prev_steps:
                        prev_steps[sid] = {"step_id": sid, "statement": p, "status": "open", "method": "", "results": []}
            for sid, step in prev_steps.items():
                if sid not in steps or step.get("status") in {"advanced", "closed", "invalidated", "superseded"}:
                    steps[sid] = deepcopy(step)
        normalized_direction = {"steps": steps}
        self._normalize_gap_tree(normalized_direction)
        steps = normalized_direction["steps"]
        return {
            "direction_id": did,
            "title": _clean_text(raw.get("title"), field="direction.title", max_length=500),
            "goal": _clean_text(raw.get("goal"), field="direction.goal", max_length=2000),
            "status": status,
            "health": health,
            "direction_kind": direction_kind,
            "standing_hypothesis": str(
                raw.get("standing_hypothesis") or (previous or {}).get("standing_hypothesis") or ""
            ).strip(),
            "terminal_gap_id": str(
                raw.get("terminal_gap_id") or (previous or {}).get("terminal_gap_id") or ""
            ).strip(),
            # 兑现失败撞墙后置位，提示编排层：必须先派一个对偶/证伪探针，且在其回报前
            # 冻结对该方向的 exploit；探针未证伪则赦免，证伪则整条方向 refuted。
            "dual_probe_required": bool(
                raw.get("dual_probe_required")
                if raw.get("dual_probe_required") is not None
                else (previous or {}).get("dual_probe_required", False)
            ),
            "deprioritized": bool(
                raw.get("deprioritized")
                if raw.get("deprioritized") is not None
                else (previous or {}).get("deprioritized", False)
            ),
            # 运行期维护的兑现送审/返回日志，SyncResearchState 不覆盖它，只从上一份状态承接。
            "consolidation_log": list((previous or {}).get("consolidation_log", [])),
            "steps": steps,
            "progress": deepcopy((previous or {}).get("progress") or self._empty_progress()),
            "progress_summary": str(raw.get("progress_summary") or "").strip(),
            "stop_condition": str(raw.get("stop_condition") or "").strip(),
            # 禁忌搜索：记录在此方向上已失败的方法及其失败分类。
            #   method_blocked    —— 思路错：该方法无法达到目标，但目标本身可能正确。
            #                        → 保留 goal，换方法重试（新 direction 或同 direction 新 method）。
            #   conclusion_refuted —— 结论错：目标本身可能为假（找到了反例/证伪）。
            #                        → 考虑 refuted 方向或换目标。
            # SyncResearchState 可显式传入以覆盖；缺省时从 previous 承接（运行期维护）。
            "failed_methods": list(
                raw.get("failed_methods")
                if raw.get("failed_methods") is not None
                else (previous or {}).get("failed_methods", [])
            ),
            "evidence_refs": _string_list(
                raw.get("evidence_refs") if raw.get("evidence_refs") is not None
                else (previous or {}).get("evidence_refs"),
                field="direction.evidence_refs", max_items=30,
            ),
            "knowledge_refs": _string_list(
                raw.get("knowledge_refs") if raw.get("knowledge_refs") is not None
                else (previous or {}).get("knowledge_refs"),
                field="direction.knowledge_refs", max_items=30,
            ),
        }

    @staticmethod
    def _empty_progress() -> dict[str, Any]:
        return {
            "attempts": 0,
            "verified_outputs": 0,
            "failed_outputs": 0,
            "protocol_failures": 0,
            "target_solves": 0,
            "refutations": 0,
            "last_impact": "",
            "remaining_gaps": [],
        }

    def _empty_state(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "state_revision": 0,
            "state_id": "state-0",
            "objective": {"summary": "Resolve problem.md", "status": "unknown"},
            "directions": {},
            "routes": {},
            "route_learnings": [],
            "dispatch_plan": [],
            "impacts": {},
            "outcomes": {},
            "dispatch_constraints": {},
            "global_consolidation": {},
            "updated_at": "",
        }

    def _save(self, state: dict[str, Any]) -> None:
        self.verified_dir.mkdir(parents=True, exist_ok=True)
        # Curator writes route learning from a background thread. Merge append-only learning
        # memory and newly created routes from the latest disk image before replacing it, so a
        # stale orchestrator transaction cannot erase a concurrent curator commit.
        disk_state: dict[str, Any] = {}
        if self.json_path.is_file():
            try:
                loaded = json.loads(self.json_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    disk_state = loaded
            except (OSError, json.JSONDecodeError):
                disk_state = {}
        routes = state.setdefault("routes", {})
        for route_id, route in (disk_state.get("routes") or {}).items():
            routes.setdefault(route_id, deepcopy(route))
        learning_by_id = {
            str(item.get("learning_id")): deepcopy(item)
            for item in disk_state.get("route_learnings") or []
            if isinstance(item, dict) and item.get("learning_id")
        }
        for item in state.get("route_learnings") or []:
            if isinstance(item, dict) and item.get("learning_id"):
                learning_by_id[str(item["learning_id"])] = deepcopy(item)
        state["route_learnings"] = list(learning_by_id.values())[-100:]
        revision = max(
            0,
            int(state.get("state_revision") or 0),
            int(disk_state.get("state_revision") or 0),
        ) + 1
        state["state_revision"] = revision
        state["state_id"] = f"state-{revision}"
        json_text = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
        markdown_text = self._render_markdown(state)
        self._atomic_write(self.json_path, json_text)
        self._atomic_write(self.markdown_path, markdown_text)

    def refresh_markdown_view(self) -> None:
        """Refresh the generated view after external attempt/blocker ledgers change."""
        self.verified_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write(self.markdown_path, self._render_markdown(self.load()))

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(content, encoding="utf-8")
        temp.replace(path)

    def _attempts_by_direction(self, *, limit_per_direction: int = 6) -> dict[str, list[dict[str, Any]]]:
        try:
            raw_lines = self.attempt_ledger_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return {}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for line in raw_lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            direction_id = str(item.get("direction_id") or "").strip()
            if not direction_id:
                continue
            grouped.setdefault(direction_id, []).append(item)
        for direction_id, attempts in grouped.items():
            attempts.sort(key=lambda item: int(item.get("sequence") or 0))
            grouped[direction_id] = attempts[-max(1, int(limit_per_direction)):]
        return grouped

    def _active_persistent_blockers(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self.blocker_registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        blockers = payload.get("blockers") if isinstance(payload, dict) else None
        if not isinstance(blockers, dict):
            return []
        return [
            item for item in blockers.values()
            if isinstance(item, dict) and item.get("status") == "active"
        ]

    @staticmethod
    def _difficulty_candidates(attempts_by_direction: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for attempts in attempts_by_direction.values():
            for attempt in attempts:
                handoff = attempt.get("difficulty_handoff")
                if not isinstance(handoff, dict) or handoff.get("disposition") != "active_candidate":
                    continue
                if not str(handoff.get("blocking_obligation") or "").strip():
                    continue
                candidates.append({
                    "sequence": int(attempt.get("sequence") or 0),
                    "direction_id": str(attempt.get("direction_id") or ""),
                    "gap_id": str(attempt.get("gap_id") or ""),
                    "method_id": str(attempt.get("method_id") or ""),
                    "handoff": handoff,
                })
        candidates.sort(key=lambda item: item["sequence"], reverse=True)
        return candidates[:8]

    def _render_markdown(self, state: dict[str, Any]) -> str:
        objective = state.get("objective", {})
        directions = list(state.get("directions", {}).values())
        attempts_by_direction = self._attempts_by_direction()
        difficulty_candidates = self._difficulty_candidates(attempts_by_direction)
        persistent_blockers = self._active_persistent_blockers()
        lines = [
            "# Verified Propositions State",
            "",
            "## Objective Frontier",
            f"- Status: {objective.get('status', 'unknown')}",
            f"- Objective: {objective.get('summary', 'Resolve problem.md')}",
            "- Direction goals below are research hypotheses or programs, not verified facts unless their evidence references say so.",
            "",
        ]
        # 兑现/对偶待办（surface）：把"该送审 / 该对偶"直接写到 state.md 顶部，供 orchestrator 一眼看到。
        dual_ids = [d["direction_id"] for d in directions if d.get("dual_probe_required")]
        ready = self.consolidation_readiness(state)
        if dual_ids or ready:
            lines.append("## Action Required (consolidation / dual probe)")
            if dual_ids:
                lines.append(
                    "- ⚠ Dual-probe required (a consolidation hit a wall — spawn a dual/falsification probe, "
                    "`SpawnWorker consolidation=true` pinning the NEGATION / a witness, small cases first, "
                    "BEFORE any normal work on these directions): "
                    + ", ".join(f"`{d}`" for d in dual_ids)
                )
            for entry in ready:
                lines.append(
                    f"- Consolidation ready: `{entry['direction_id']}` "
                    f"({entry['reason']}, {entry.get('verified_props_total', 0)} verified outputs "
                    f"≥ threshold {entry.get('threshold')}). "
                    "Spawn `SpawnWorker consolidation=true` pinning its terminal goal (no weakening)."
                )
            lines.append("")
        if difficulty_candidates:
            lines.extend([
                "## Difficulty Watch (awaiting portfolio comparison)",
                "- These are worker-level candidates. Compare exact obligations and evidence before treating any as the same blocker.",
            ])
            for item in difficulty_candidates:
                handoff = item["handoff"]
                lines.append(
                    f"- Attempt #{item['sequence']} — `{item['direction_id']}/{item['gap_id']}` "
                    f"[{item['method_id'] or 'unknown method'}]: {handoff.get('blocking_obligation')}"
                )
                last_step = str(handoff.get("last_verified_step") or "").strip()
                if last_step:
                    lines.append(f"  - Last verified step: {last_step}")
                suggested = str(handoff.get("suggested_attack") or "").strip()
                if suggested:
                    lines.append(f"  - Suggested attack: {suggested}")
            lines.append("")
        if persistent_blockers:
            lines.extend([
                "## Active Persistent Blockers", 
                "- Curator-confirmed repeated obligations. Targeted work must attack the blocker gate or provide an evidence-based pivot.",
            ])
            for blocker in persistent_blockers:
                gate = blocker.get("gate") if isinstance(blocker.get("gate"), dict) else {}
                approaches = blocker.get("approaches") if isinstance(blocker.get("approaches"), list) else []
                lines.append(
                    f"- `{blocker.get('blocker_id')}` → `{gate.get('direction_id', '?')}/{gate.get('gap_id', '?')}`: "
                    f"{blocker.get('statement') or 'No statement recorded.'} "
                    f"(observed {blocker.get('occurrence_count') or 0} time(s) across {len(approaches)} approach(es))"
                )
            lines.append("")
        audit_blockers = list((state.get("audit_blockers") or {}).values())
        active_blockers = [item for item in audit_blockers if item.get("status") == "active"]
        if active_blockers:
            lines.extend(["## Active Audit Blockers", "- Targeted workers in these directions must attack the named blocker or provide an explicit, evidence-based pivot reason."])
            for blocker in active_blockers:
                approaches = blocker.get("approaches") if isinstance(blocker.get("approaches"), list) else []
                lines.append(
                    f"- `{blocker.get('direction_id')}/{blocker.get('gap_id')}`: "
                    f"{blocker.get('statement') or 'No statement recorded.'} "
                    f"(observed {blocker.get('occurrence_count') or 0} time(s) across {len(approaches)} approach(es); "
                    f"audit: `{blocker.get('audit_path') or 'unknown'}`)"
                )
                for approach in approaches:
                    if not isinstance(approach, dict):
                        continue
                    lines.append(
                        f"  - `{approach.get('direction_id')}/{approach.get('gap_id')}/{approach.get('method_id')}` "
                        f"({approach.get('occurrence_count')}): {approach.get('description') or 'No description recorded.'}"
                    )
            lines.append("")
        constraints = list((state.get("dispatch_constraints") or {}).values())
        if constraints:
            lines.extend(["## Dispatch Constraints", "- These constraints are enforced before targeted `SpawnWorker` calls."])
            for constraint in constraints:
                did = constraint.get("direction_id", "?")
                gid = constraint.get("gap_id", "?")
                status = constraint.get("status", "?")
                level = constraint.get("evidence_level", "?")
                claim = (constraint.get("claim") or "").replace("\n", " ")
                lines.append(f"- `{did}/{gid}` [{status}; {level}]: {claim}")
            lines.append("")
        routes = state.get("routes") if isinstance(state.get("routes"), dict) else {}
        if routes:
            lines.extend(["## Research Routes", "- Each route is proposed from a versioned reviewer state; workers are branches explored by the orchestrator."])
            for route in routes.values():
                if not isinstance(route, dict):
                    continue
                assessments = route.get("assessments") if isinstance(route.get("assessments"), list) else []
                latest = assessments[-1] if assessments else {}
                lines.append(
                    f"- `{route.get('route_id', '?')}` [{route.get('status', 'unknown')}] → "
                    f"`{route.get('direction_id', '?')}/{route.get('gap_id', '?')}`; "
                    f"state `{route.get('based_on_state_id', '?')}`, workers={len(route.get('worker_ids') or [])}."
                )
                lines.append(f"  - Claim: {route.get('route_claim') or 'not stated'}")
                if latest:
                    lines.append(f"  - Latest assessment: `{latest.get('verdict', '?')}` — {latest.get('summary') or ''}")
            lines.append("")
        learnings = state.get("route_learnings") if isinstance(state.get("route_learnings"), list) else []
        if learnings:
            lines.extend(["## Curated Route Learnings", "- Curator-derived lessons are strategy evidence, not mathematical proofs."])
            for learning in learnings[-12:]:
                if not isinstance(learning, dict):
                    continue
                lines.append(
                    f"- `{learning.get('learning_id', '?')}` [{learning.get('confidence', 'unknown')}] "
                    f"on {', '.join(str(item) for item in learning.get('route_ids') or [])}: {learning.get('finding') or ''}"
                )
            lines.append("")
        impacts = state.get("impacts") if isinstance(state.get("impacts"), dict) else {}
        lines.append("## Active Directions")
        active = [item for item in directions if item.get("status") not in {"paused", "refuted", "completed"}]
        if not active:
            lines.append("- None recorded.")
        for direction in active:
            lines.extend(
                self._render_direction(
                    direction,
                    attempts=attempts_by_direction.get(str(direction.get("direction_id") or ""), []),
                    impacts=impacts,
                )
            )

        lines.extend(["", "## Paused, Refuted, Or Completed Directions"])
        inactive = [item for item in directions if item.get("status") in {"paused", "refuted", "completed"}]
        if not inactive:
            lines.append("- None recorded.")
        for direction in inactive:
            lines.extend(
                self._render_direction(
                    direction,
                    attempts=attempts_by_direction.get(str(direction.get("direction_id") or ""), []),
                    impacts=impacts,
                )
            )

        lines.extend(["", "## Dispatch Plan"])
        plan = state.get("dispatch_plan") or []
        lines.extend([f"- {item}" for item in plan] or ["- Not recorded."])
        lines.extend([
            "",
            "## State Metadata",
            f"- Generated from `research_state.json` at {state.get('updated_at') or 'unknown time'}.",
            "- Update strategic state with `SyncResearchState`; update proposition facts separately in `index.md`.",
            "",
        ])
        return "\n".join(lines)

    def _render_direction(
        self,
        direction: dict[str, Any],
        *,
        attempts: list[dict[str, Any]] | None = None,
        impacts: dict[str, Any] | None = None,
    ) -> list[str]:
        progress = direction.get("progress", {})
        attempts = attempts or []
        impacts = impacts or {}
        lines = [
            "",
            f"### {direction['direction_id']} — {direction['title']}",
            f"- Goal: {direction['goal']}",
            f"- Status / health: {direction['status']} / {direction['health']}",
            (
                "- Progress counters: "
                f"attempts={progress.get('attempts', 0)}, "
                f"verified={progress.get('verified_outputs', 0)}, "
                f"failed={progress.get('failed_outputs', 0)}"
            ),
        ]
        kind = direction.get("direction_kind", "unknown")
        if kind and kind != "unknown":
            extra = f"- Kind: {kind}"
            if direction.get("standing_hypothesis"):
                extra += f" (standing hypothesis: {direction['standing_hypothesis']})"
            if direction.get("terminal_gap_id"):
                extra += f"; terminal gap: `{direction['terminal_gap_id']}`"
            lines.append(extra)
        if direction.get("dual_probe_required"):
            lines.append(
                "- ⚠ Dual-probe required: consolidation hit a wall — spawn a dual/falsification probe "
                "(construct a witness for the negation) BEFORE any further exploit on this direction."
            )
        if direction.get("deprioritized"):
            lines.append("- Deprioritized: lower this direction's worker budget until the dual probe reports.")
        failed_methods = direction.get("failed_methods") or []
        if failed_methods:
            lines.append("- Failed methods (tabu list — do NOT reuse these method families for the same goal):")
            for fm in failed_methods:
                ft = fm.get("failure_type", "method_blocked")
                mf = fm.get("method_family", "?")
                fm_summary = (fm.get("summary") or "").strip()
                marker = "conclusion likely wrong" if ft == "conclusion_refuted" else "method exhausted, goal may still hold"
                lines.append(f"  - `{mf}` [{ft}]: {marker}. {fm_summary}")
        consolidation_log = direction.get("consolidation_log") or []
        if consolidation_log:
            lines.append("- Consolidation log (submitted → returned):")
            for entry in consolidation_log:
                kind = entry.get("kind", "consolidation")
                submitted = entry.get("submitted_at") or "?"
                worker = entry.get("worker_id") or "?"
                target = entry.get("pinned_target")
                target_note = f" target=\"{target[:80]}\"" if target else ""
                outcome = entry.get("outcome", "pending")
                if outcome == "pending":
                    lines.append(f"  - [{submitted}] {kind} submitted (worker {worker}){target_note} → pending")
                else:
                    returned = entry.get("returned_at") or "?"
                    rel = entry.get("relation_to_target") or ""
                    rel_note = f", relation={rel}" if rel else ""
                    ret_summary = (entry.get("returned_summary") or "").strip()
                    if ret_summary:
                        ret_summary = " — " + ret_summary
                    lines.append(
                        f"  - [{submitted}] {kind} (worker {worker}){target_note} → "
                        f"{outcome} [{returned}]{rel_note}{ret_summary}"
                    )
        summary = progress.get("last_impact") or direction.get("progress_summary")
        if summary:
            lines.append(f"- Latest progress: {summary}")
        lines.append("- Gap tree:")
        steps = direction.get("steps", {})
        if steps:
            self._normalize_gap_tree(direction)
            steps = direction.get("steps", {})
            children = self._gap_children(steps)

            def render_gap(gap_id: str, depth: int) -> None:
                step = steps[gap_id]
                prefix = "  " * (depth + 1)
                policy = step.get("resolution_policy", "manual")
                kind = step.get("gap_kind", "unknown")
                ready = "; ready for synthesis" if step.get("ready_for_synthesis") else ""
                lines.append(
                    f"{prefix}- `{gap_id}` [{step.get('status', 'open')}; {policy}; {kind}{ready}]: "
                    f"{step['statement']}"
                )
                if step.get("method"):
                    lines.append(f"{prefix}  method: {step['method']}")
                for r in step.get("results", []):
                    lines.append(f"{prefix}  result ({r.get('relation', '?')}): {r.get('summary', '')[:200]}")
                for child_id in children.get(gap_id, []):
                    render_gap(child_id, depth + 1)

            roots = sorted(gap_id for gap_id, step in steps.items() if not step.get("parent_gap_id"))
            for root_id in roots:
                render_gap(root_id, 0)
        else:
            lines.append("  - No gap recorded.")
        if attempts:
            lines.append("- Recent worker attempts (settled outcome ledger):")
            for attempt in attempts:
                sequence = attempt.get("sequence", "?")
                status = attempt.get("status", "unknown")
                gap_id = attempt.get("gap_id") or "-"
                method_id = attempt.get("method_id") or "-"
                lines.append(f"  - Attempt #{sequence} [{status}; `{gap_id}`; `{method_id}`]")
                verified_file = str(attempt.get("verified_file") or "").strip()
                proposition_file = str(attempt.get("proposition_file") or "").strip()
                if verified_file:
                    lines.append(f"    output: verified proposition `{verified_file}`")
                elif proposition_file:
                    lines.append(f"    output: candidate proposition `{proposition_file}`")
                else:
                    summary = " ".join(str(attempt.get("summary") or "").split())
                    if summary:
                        lines.append(f"    output: {summary[:300]}")
                impact = impacts.get(str(attempt.get("worker_id") or ""))
                if isinstance(impact, dict):
                    lines.append(
                        "    impact: "
                        f"{impact.get('relation_to_target') or 'unknown'} / "
                        f"{impact.get('gap_effect') or 'unknown'} — "
                        f"{str(impact.get('summary') or '')[:300]}"
                    )
                handoff = attempt.get("difficulty_handoff")
                if isinstance(handoff, dict) and handoff.get("disposition") == "active_candidate":
                    obligation = " ".join(str(handoff.get("blocking_obligation") or "").split())
                    if obligation:
                        lines.append(f"    difficulty: {obligation[:400]}")
                    last_step = " ".join(str(handoff.get("last_verified_step") or "").split())
                    if last_step:
                        lines.append(f"    last verified step: {last_step[:300]}")
        if direction.get("stop_condition"):
            lines.append(f"- Stop condition: {direction['stop_condition']}")
        if direction.get("evidence_refs"):
            lines.append("- Evidence: " + ", ".join(f"`{ref}`" for ref in direction["evidence_refs"]))
        if direction.get("knowledge_refs"):
            lines.append("- Knowledge: " + ", ".join(f"`{ref}`" for ref in direction["knowledge_refs"]))
        return lines
