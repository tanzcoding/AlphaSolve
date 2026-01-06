"""Shared state container used as `shared` in PocketFlow.

Design goals (project invariants):
- `shared` is a single object passed through PocketFlow as-is.
- Nodes MUST only read from shared in `prep(shared)`.
- Nodes MUST only write to shared in `post(shared, prep_res, exec_res)`.

This module implements SharedContext as a dict subclass with:
- fixed key set (no adding/removing keys)
- values are mutable/replaceable
- both `shared['x']` and `shared.x` access styles

The business logic helpers (prompt building, etc.) should live in nodes.
The only shared-side helper we keep is dependency expansion for verifiers:
`build_reasoning_path()`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, TypedDict, Literal
from utils.logger import Logger


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
    cot: Optional[str]
    is_theorem: bool


class SharedContext(dict):
    """A dict-like shared state with an immutable key set.

    Keys are fixed at initialization time. You may update existing keys' values,
    but you may NOT add or delete keys.
    """

    # NOTE: keep key strings stable; they are part of the project's public runtime schema.
    FIXED_KEYS: Set[str] = {
        "problem",
        "hint",
        "solver_round_remaining",
        "verify_refine_round_remaining",
        "lemmas",
        "current_lemma_id",  # Optional[int]
        "result_summary",
        # reserved for future (queued): message trace for diff-style refinement
        "messages_for_refiner",
    }

    def __init__(
        self,
        *,
        problem: str,
        solver_round_remaining: int,
        verify_refine_round_remaining: int,
        hint: Optional[str] = None,
        logger: Logger = None,
    ):
        super().__init__()
        # internal flag for attribute assignment
        object.__setattr__(self, "_frozen", False)

        super().__setitem__("problem", problem)
        super().__setitem__("hint", hint)
        super().__setitem__("solver_round_remaining", solver_round_remaining)
        super().__setitem__("verify_refine_round_remaining", verify_refine_round_remaining)
        super().__setitem__("lemmas", [])
        super().__setitem__("current_lemma_id", None)
        super().__setitem__("result_summary", None)
        super().__setitem__("messages_for_refiner", [])

        # freeze key set
        object.__setattr__(self, "_frozen", True)

        missing = self.FIXED_KEYS - set(self.keys())
        extra = set(self.keys()) - self.FIXED_KEYS
        if missing or extra:
            raise RuntimeError(f"SharedContext schema mismatch: missing={missing}, extra={extra}")

    # -------------------------
    # Fixed-key dict enforcement
    # -------------------------
    def __setitem__(self, key: str, value: Any) -> None:
        if getattr(self, "_frozen", False) and key not in self:
            raise KeyError(f"SharedContext key is fixed; cannot add new key: {key!r}")
        return super().__setitem__(key, value)

    def __delitem__(self, key: str) -> None:
        raise KeyError("SharedContext key set is fixed; deletion is forbidden")

    def pop(self, key: str, default: Any = None):  # type: ignore[override]
        raise KeyError("SharedContext key set is fixed; pop() is forbidden")

    def popitem(self):  # type: ignore[override]
        raise KeyError("SharedContext key set is fixed; popitem() is forbidden")

    def clear(self) -> None:  # type: ignore[override]
        raise KeyError("SharedContext key set is fixed; clear() is forbidden")

    def update(self, *args, **kwargs) -> None:  # type: ignore[override]
        # only allow updates to existing keys
        data = dict(*args, **kwargs)
        for k in data:
            if k not in self:
                raise KeyError(f"SharedContext key is fixed; cannot add new key via update(): {k!r}")
        for k, v in data.items():
            super().__setitem__(k, v)

    def setdefault(self, key: str, default: Any = None):  # type: ignore[override]
        raise KeyError("SharedContext key set is fixed; setdefault() is forbidden")

    # -------------------------
    # Attribute-style access
    # -------------------------
    def __getattr__(self, name: str) -> Any:
        if name in self:
            return self[name]
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in ("_frozen",):
            object.__setattr__(self, name, value)
            return
        if name in self:
            self[name] = value
            return
        raise AttributeError(f"SharedContext has fixed keys; cannot set attribute {name!r}")

    # -------------------------
    # Lemma helpers
    # -------------------------
    @staticmethod
    def new_lemma(
        *,
        statement: str,
        proof: str,
        dependencies: Optional[List[int]] = None,
        is_theorem: bool = False,
        status: str = "pending",
        review: Optional[str] = None,
        cot: Optional[str] = None,
    ) -> Lemma:
        return {
            "statement": statement,
            "proof": proof,
            "dependencies": list(dependencies or []),
            "status": status,
            "review": review,
            "cot": cot,
            "is_theorem": is_theorem,
        }

    @staticmethod
    def validate_lemma(lemma: Dict[str, Any], *, lemma_id: Optional[int] = None) -> None:
        """Best-effort runtime validation for lemma dicts.

        This exists so the lemma schema is fully discoverable *in code*.
        It may be called in post() blocks right before mutating shared.
        """

        required = {"statement", "proof", "dependencies", "status", "is_theorem"}
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

        if lemma_id is not None:
            for d in deps:
                if d >= lemma_id:
                    raise ValueError(f"Lemma.dependencies must reference earlier lemmas only: dep={d} >= self={lemma_id}")

    def build_reasoning_path(self, lemma_id: int, *, verified_only: bool = True) -> List[int]:
        """Return transitive dependencies for lemma_id as a topologically ordered list.

        The returned list does NOT include lemma_id itself.
        If verified_only=True, only dependencies with status=='verified' are included,
        and recursion continues only through verified lemmas.
        """

        lemmas: List[Lemma] = self["lemmas"]
        if lemma_id < 0 or lemma_id >= len(lemmas):
            raise IndexError(f"lemma_id out of range: {lemma_id}")

        seen: Set[int] = set()
        out: List[int] = []

        def ok(i: int) -> bool:
            if not verified_only:
                return True
            return (lemmas[i].get("status") == "verified")

        def dfs(i: int) -> None:
            deps = lemmas[i].get("dependencies") or []
            for d in deps:
                if not isinstance(d, int):
                    continue
                if d < 0 or d >= len(lemmas):
                    continue
                # enforce "only depends on earlier lemmas" best-effort
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
