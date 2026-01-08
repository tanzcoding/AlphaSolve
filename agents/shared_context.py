"""Typed schema for the shared PocketFlow state.

Design goals (project invariants):
- ``shared`` is a single dict passed through PocketFlow as-is.
- Nodes MUST only read from shared in ``prep(shared)``.
- Nodes MUST only write to shared in ``post(shared, prep_res, exec_res)``.

``SharedContext`` is now a ``TypedDict`` so the schema is explicit and easier to
maintain. Runtime helpers (lemma factory / validation / dependency expansion)
are exposed as module-level functions.

Business-logic helpers (prompt building, etc.) should stay inside the
corresponding nodes. The only shared-side helper we keep is dependency
expansion for verifiers via :func:`build_reasoning_path`.
"""

from __future__ import annotations
from typing import Any, Dict, List, Optional, TypedDict, Literal
from openai.types.chat import ChatCompletionMessageParam


LemmaStatus = Literal["pending", "verified", "rejected"]


class Lemma(TypedDict):
    """Lemma schema stored in `shared['lemmas']`.

    IMPORTANT:
    - A lemma's id is its list index.
    - `dependencies` MUST only reference earlier ids (`dep < self_id`).

    Fields:
    - statement: lemma / conjecture statement
    - proof: proof text
    - dependencies: transitive assumptions by lemma id
    - status: pending|verified|rejected
    - review: verifier feedback (optional)
    - cot: model reasoning trace captured by LLMClient (optional)
    - is_theorem: whether this lemma proves the original problem
    """

    statement: str
    proof: str
    dependencies: List[int]
    status: LemmaStatus
    review: Optional[str]
    is_theorem: bool
    history_messages: List[ChatCompletionMessageParam]
    verify_round: int


class SharedContext(TypedDict):
    """Schema for the single dict passed between PocketFlow nodes."""

    problem: str
    hint: Optional[str]
    lemmas: List[Lemma]
    current_lemma_id: Optional[int]
    result_summary: Optional[str]


def build_reasoning_path(
    lemmas: List[Lemma],
    lemma_id: int,
    *,
    verified_only: bool = True,
) -> List[int]:
    """Return transitive dependencies for ``lemma_id`` as a topologically ordered list.

    Args:
        lemmas: List of Lemma objects
        lemma_id: Target lemma ID to build path for
        verified_only: If True, only include verified lemmas in the path

    Returns:
        Topologically ordered list of lemma IDs representing the reasoning path
    """
    if lemma_id < 0 or lemma_id >= len(lemmas):
        raise IndexError(f"lemma_id out of range: {lemma_id}")

    seen: set[int] = set()
    out: List[int] = []

    def ok(i: int) -> bool:
        if not verified_only:
            return True
        return lemmas[i].get("status") == "verified"

    def dfs(i: int) -> None:
        deps = lemmas[i].get("dependencies") or []
        for d in deps:
            if not isinstance(d, int):
                continue
            if d < 0 or d >= len(lemmas):
                continue
            if d >= i:
                continue
            if verified_only and not ok(d):
                continue
            if d in seen:
                continue
            seen.add(d)
            dfs(d)
            out.append(d)

    dfs(lemma_id)
    return out


def new_shared_context(*, problem: str, hint: Optional[str] = None) -> SharedContext:
    """Factory that pre-populates required shared keys."""

    return {
        "problem": problem,
        "hint": hint,
        "lemmas": [],
        "current_lemma_id": None,
        "result_summary": None,
    }


def new_lemma(
    *,
    statement: str,
    proof: str,
    dependencies: Optional[List[int]] = None,
    is_theorem: bool = False,
    status: LemmaStatus = "pending",
    review: Optional[str] = None,
    cot: Optional[str] = None,
    history_messages: Optional[List[ChatCompletionMessageParam]] = None,
    verify_round: Optional[int] = None,
) -> Lemma:
    return {
        "statement": statement,
        "proof": proof,
        "dependencies": list(dependencies or []),
        "status": status,
        "review": review,
        "cot": cot,
        "is_theorem": is_theorem,
        "history_messages": list(history_messages or []),
        "verify_round": verify_round,
    }


def validate_lemma(lemma: Dict[str, Any], *, lemma_id: Optional[int] = None) -> None:
    """Best-effort runtime validation for lemma dicts."""

    required = {
        "statement",
        "proof",
        "dependencies",
        "status",
        "is_theorem",
        "history_messages",
    }
    missing = required - set(lemma.keys())
    if missing:
        raise ValueError(f"Lemma missing required fields: {sorted(missing)}")

    if not isinstance(lemma.get("statement"), str):
        raise TypeError("Lemma.statement must be str")
    if not isinstance(lemma.get("proof"), str):
        raise TypeError("Lemma.proof must be str")
    deps = lemma.get("dependencies")
    if not isinstance(deps, list) or not all(isinstance(x, int) for x in deps):
        raise TypeError("Lemma.dependencies must be List[int]")
    st = lemma.get("status")
    if st not in ("pending", "verified", "rejected"):
        raise ValueError("Lemma.status must be one of: pending|verified|rejected")
    if not isinstance(lemma.get("is_theorem"), bool):
        raise TypeError("Lemma.is_theorem must be bool")
    if not isinstance(lemma.get("history_messages"), list):
        raise TypeError("Lemma.history_messages must be List[ChatCompletionMessageParam]")
    if not isinstance(lemma.get("verify_round"), int):
        raise TypeError("Lemma.verify_round must be int")

    if lemma_id is not None:
        for d in deps:
            if d >= lemma_id:
                raise ValueError(
                    f"Lemma.dependencies must reference earlier lemmas only: dep={d} >= self={lemma_id}"
                )
