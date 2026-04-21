from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from alphasolve.agents.lemma_worker import LemmaWorkerResult
from alphasolve.utils.logger import Logger


@dataclass
class CommitDecision:
    accepted: bool
    status: str
    solved: bool
    duplicate_of: Optional[int] = None


class LemmaPool:
    def __init__(
        self,
        *,
        capacity_verified: int,
        logger: Logger,
        snapshot_path: Optional[str] = None,
        previous_snapshot_path: Optional[str] = None,
        init_from_previous: bool = True,
    ):
        self.capacity_verified = max(1, int(capacity_verified))
        self.logger = logger
        self.snapshot_path = snapshot_path ## 写到哪里
        self.previous_snapshot_path = previous_snapshot_path ## 从哪里读
        self._lock = threading.Lock()
        self.all_lemmas: list[dict] = []
        self.verified_lemmas: list[dict] = []
        self._solved = False
        self.init_from_previous = init_from_previous

        self.logger.log_print(f"previous_snapshot_path  {self.previous_snapshot_path} current_snapshot_path {self.snapshot_path}",
            module="lemma_pool",
        )

        if self.init_from_previous:
            try:
                self.init_from_snapshot()
            except Exception: ## 绝对不抛异常
                self.logger.log_print("event=lemma_pool_init_from_snapshot_failed", module="lemma_pool")


    def snapshot_verified(self) -> list[dict]:
        with self._lock:
            return [dict(l) for l in self.verified_lemmas]

    def snapshot_all(self) -> list[dict]:
        with self._lock:
            return [dict(l) for l in self.all_lemmas]

    def remaining_verified_capacity(self) -> int:
        with self._lock:
            return self.capacity_verified - len(self.verified_lemmas)

    def is_full(self) -> bool:
        return self.remaining_verified_capacity() <= 0

    def is_solved(self) -> bool:
        with self._lock:
            return self._solved

    def find_duplicate(self, lemma: dict) -> Optional[int]:
        statement = (lemma.get("statement") or "").strip().lower()
        if not statement:
            return None
        for idx, existing in enumerate(self.verified_lemmas):
            if (existing.get("statement") or "").strip().lower() == statement:
                return idx
        return None

    def commit(self, result: LemmaWorkerResult) -> CommitDecision: 
        with self._lock:
            lemma = dict(result.lemma)
            lemma["uid"] = len(self.all_lemmas)

            solved = False
            accepted = False
            duplicate_of = None

            if result.status == "verified":
                duplicate_of = self.find_duplicate(lemma)
                if duplicate_of is not None:
                    lemma["status"] = "rejected"
                    lemma["review"] = f"duplicate_of_verified:{duplicate_of}"
                elif len(self.verified_lemmas) < self.capacity_verified:
                    lemma["status"] = "verified"
                    self.verified_lemmas.append(lemma)
                    accepted = True
                    solved = bool(result.is_theorem)
                    if solved:
                        self._solved = True
                else:
                    lemma["status"] = "rejected"
                    lemma["review"] = "verified_capacity_reached"
            else:
                lemma["status"] = "rejected"

            self.all_lemmas.append(lemma)
            self.save_snapshot_latest()

            status = lemma.get("status", "rejected")
            self.logger.log_print(
                f"event=lemma_pool_commit uid={lemma['uid']} status={status} accepted={accepted} duplicate_of={duplicate_of}",
                module="lemma_pool",
            )

            return CommitDecision(
                accepted=accepted,
                status=str(status),
                solved=solved,
                duplicate_of=duplicate_of,
            )

    def save_snapshot_latest(self) -> None:
        if not self.snapshot_path:
            return

        os.makedirs(os.path.dirname(self.snapshot_path), exist_ok=True)
        payload = {
            "last_updated": datetime.now().isoformat(),
            "capacity_verified": self.capacity_verified,
            "verified_count": len(self.verified_lemmas),
            "all_count": len(self.all_lemmas),
            "solved": self._solved,
            "verified_lemmas": self.verified_lemmas,
            "all_lemmas": self.all_lemmas,
        }
        with open(self.snapshot_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def init_from_snapshot(self) -> None:
        if not self.previous_snapshot_path or not os.path.exists(self.previous_snapshot_path):
            self.logger.log_print(
                 f"previous_snapshot_path empty or not exists {self.previous_snapshot_path}",
                 module="lemma_pool",
            )
            return

        with open(self.previous_snapshot_path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        with self._lock: 
            self.verified_lemmas = payload.get("verified_lemmas", [])
            self.all_lemmas = payload.get("all_lemmas", [])
            # self._solved = payload.get("solved", False)

        self.logger.log_print(
            f"event=lemma_pool_init_from_snapshot verified={len(self.verified_lemmas)} all={len(self.all_lemmas)} solved={self._solved}",
            module="lemma_pool",
        )