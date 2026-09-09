"""Runtime-owned startup evidence collection."""
from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .curation_records import append_event as append_curation_event
from .policy import SolverPolicy
from .project import ProjectLayout

if TYPE_CHECKING:
    from .orchestrator import WorkerManager


DEFAULT_COLD_START_VERIFIED_PROPOSITION_THRESHOLD = (
    SolverPolicy().cold_start_verified_proposition_threshold
)

# 冷启动批没有 orchestrator 指令可循（它先于 LLM 运行），因此验收标准也由运行时定义：
# 只要求一条可独立检验的、朝向原题的命题。
COLD_START_RUBRIC = (
    "- The Statement is one precise, independently checkable mathematical claim about `problem.md`\n"
    "- The Statement is self-contained: every hypothesis it needs is stated in it\n"
    "- If the attempt stalled, a precise obstacle was recorded rather than a weakened restatement"
)

ColdStartProgressCallback = Callable[[str, int, int], None]


def cold_start_verified_proposition_threshold(settings: dict[str, Any] | None) -> int:
    """Compatibility helper for callers that still pass a raw settings mapping."""
    return SolverPolicy.from_settings(settings).cold_start_verified_proposition_threshold


def verified_proposition_count(verified_dir: Path) -> int:
    if not verified_dir.exists():
        return 0
    return sum(
        1
        for path in verified_dir.rglob("*.md")
        if path.is_file() and path.name not in {"index.md", "state.md"}
    )


@dataclass
class ColdStartRuntime:
    """Run the configured direct-worker batch before orchestrator logic begins."""

    layout: ProjectLayout
    max_workers: int
    threshold: int = DEFAULT_COLD_START_VERIFIED_PROPOSITION_THRESHOLD
    session_id: str | None = None
    stop_event: threading.Event | None = None
    _prepared: bool = False
    _initial_worker_ids: set[str] = field(default_factory=set)
    _completed: list[dict[str, Any]] = field(default_factory=list)

    @property
    def verified_count(self) -> int:
        return verified_proposition_count(self.layout.verified_dir)

    @property
    def is_cold_start(self) -> bool:
        return self.verified_count < self.threshold

    def prepare(
        self,
        manager: WorkerManager,
        *,
        progress_callback: ColdStartProgressCallback | None = None,
    ) -> list[dict[str, Any]]:
        """Collect initial direct-worker evidence before the orchestrator agent starts."""
        if self._prepared:
            return list(self._completed)
        self._prepared = True
        verified_count = self.verified_count
        if verified_count >= self.threshold:
            return []
        if self.stop_event is not None and self.stop_event.is_set():
            return []

        for _ in range(self.max_workers):
            if not manager.has_available_worker_slot():
                break
            payload = manager.spawn(
                hint=None,
                difficulty_id=None,
                method_id="direct_proof",
                rubric=COLD_START_RUBRIC,
            )
            if not payload.get("spawned"):
                break
            worker_id = str(payload.get("worker_id") or "").strip()
            if worker_id:
                self._initial_worker_ids.add(worker_id)

        if not self._initial_worker_ids:
            return []

        append_curation_event(
            self.layout,
            "cold_start_direct_batch_started",
            orchestrator_session_id=self.session_id,
            worker_ids=sorted(self._initial_worker_ids),
            worker_count=len(self._initial_worker_ids),
            verified_proposition_count=verified_count,
            verified_proposition_threshold=self.threshold,
        )
        if progress_callback is not None:
            progress_callback("waiting", 0, len(self._initial_worker_ids))
        self._collect_initial_evidence(manager, progress_callback=progress_callback)
        if progress_callback is not None:
            if self.stop_event is not None and self.stop_event.is_set():
                stage = "interrupted"
            elif len(self._completed) == len(self._initial_worker_ids):
                stage = "completed"
            else:
                stage = "incomplete"
            progress_callback(stage, len(self._completed), len(self._initial_worker_ids))
        append_curation_event(
            self.layout,
            "cold_start_direct_batch_completed",
            orchestrator_session_id=self.session_id,
            worker_ids=sorted(self._initial_worker_ids),
            completed_worker_ids=sorted(
                str(item.get("worker_id") or "") for item in self._completed
            ),
            verified_proposition_count=verified_count,
            verified_proposition_threshold=self.threshold,
        )
        return list(self._completed)

    def context_for_orchestrator(self) -> str:
        """Return compact runtime-collected evidence without exposing startup policy."""
        if not self._completed:
            return ""
        evidence = [
            {
                "worker_id": item.get("worker_id"),
                "status": item.get("status"),
                "summary": str(item.get("summary") or "")[:1200],
                "proposition_file": item.get("proposition_file"),
                "difficulty_handoff": item.get("difficulty_handoff"),
            }
            for item in self._completed
        ]
        return (
            "Runtime-collected initial worker evidence is available below. Treat it as evidence, "
            "verify its claims, and decide the next step from it.\n"
            + json.dumps(evidence, ensure_ascii=False)
        )

    def _collect_initial_evidence(
        self,
        manager: WorkerManager,
        *,
        progress_callback: ColdStartProgressCallback | None = None,
    ) -> None:
        pending = set(self._initial_worker_ids)
        while pending and not (self.stop_event is not None and self.stop_event.is_set()):
            payload = manager.wait()
            completed = [
                item
                for item in payload.get("completed") or []
                if str(item.get("worker_id") or "") in pending
            ]
            self._completed.extend(completed)
            pending.difference_update(str(item.get("worker_id") or "") for item in completed)
            if completed and pending and progress_callback is not None:
                progress_callback("waiting", len(self._completed), len(self._initial_worker_ids))
            if not completed and not manager.active:
                break
