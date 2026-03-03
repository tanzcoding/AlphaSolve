from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from agents.lemma_worker import LemmaWorkerResult
from utils.logger import Logger


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
    ):
        self.capacity_verified = max(1, int(capacity_verified))
        self.logger = logger
        self.snapshot_path = snapshot_path
        # self._lock = threading.Lock()
        self.all_lemmas: list[dict] = []
        self.verified_lemmas: list[dict] = []
        self._solved = False

    def snapshot_verified(self) -> list[dict]:
        # with self._lock:
        return [dict(l) for l in self.verified_lemmas]

    def snapshot_all(self) -> list[dict]:
        # with self._lock:
        return [dict(l) for l in self.all_lemmas]

    def remaining_verified_capacity(self) -> int:
        # with self._lock:
        return self.capacity_verified - len(self.verified_lemmas)

    def is_full(self) -> bool:
        return self.remaining_verified_capacity() <= 0

    def is_solved(self) -> bool:
        #with self._lock:
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
        #with self._lock:
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

