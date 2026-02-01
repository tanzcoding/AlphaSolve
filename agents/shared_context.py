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
from multiprocessing import Manager

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
    
    ## 这部分信息全局共享（所有的进程) 
    problem: str
    lemmas: List[Lemma]

    ##  这部分信息在某一个 alphasolve 里使用, 用来同步 solver/verifier/refiner 之间的信息
    hint: Optional[str]
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


def new_shared_context(*, problem: str, hint: Optional[str] = None, lemma_pool: Optional[List] = None) -> SharedContext:
    """Factory that pre-populates required shared keys."""
 
    ## 在实现的时候, 这个 lemma_pool 会用 manager.list(), 用来确保可以在多个进程之间共享
    ## 剩下的字段一半来说, 每个进程的 working memory, 不管

    lemmas = lemma_pool
    if not lemma_pool:
        lemmas = [ ] 
    
    return {
        "problem": problem,
        "hint": hint,
        "lemmas": lemmas,
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


def save_snapshot(shared: SharedContext, node_name: str, status: str = "normal"):
    """Save a snapshot of shared context for visualization.
    
    Args:
        shared: The shared context to save
        node_name: Name of the node that just executed
        status: Status/result of the node execution
    """
    import json
    import os
    from datetime import datetime
    
    # Create progress directory if needed
    os.makedirs("progress", exist_ok=True)
    
    # Create a serializable snapshot
    snapshot = {
        "timestamp": datetime.now().isoformat(),
        "node": node_name,
        "status": status,
        "problem": shared.get("problem", ""),
        "current_lemma_id": shared.get("current_lemma_id"),
        "lemmas": []
    }
    
    # Process lemmas to make them serializable
    for idx, lemma in enumerate(shared.get("lemmas", [])):
        lemma_data = {
            "id": idx,
            "statement": lemma.get("statement", ""),
            "proof": lemma.get("proof", ""),
            "dependencies": lemma.get("dependencies", []),
            "status": lemma.get("status", "pending"),
            "review": lemma.get("review"),
            "is_theorem": lemma.get("is_theorem", False),
            "verify_round": lemma.get("verify_round", 0)
        }
        snapshot["lemmas"].append(lemma_data)
    
    # Load existing snapshots
    progress_file = "progress/shared_state.json"
    snapshots = []
    if os.path.exists(progress_file):
        try:
            with open(progress_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                snapshots = data.get("snapshots", [])
        except:
            pass
    
    # Append new snapshot
    snapshots.append(snapshot)
    
    # Save all snapshots
    output = {
        "snapshots": snapshots,
        "last_updated": datetime.now().isoformat()
    }
    
    with open(progress_file, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
