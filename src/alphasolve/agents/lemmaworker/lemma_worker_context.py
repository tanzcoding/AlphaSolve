from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, List

from alphasolve.agents.shared_context import Lemma


@dataclass
class LemmaWorkerContext:
    problem: str
    hint: Optional[str]
    verified_snapshot: List[Lemma]
    remaining_capacity: int
    run_id: str
    worker_id: int

