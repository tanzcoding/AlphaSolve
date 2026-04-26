from __future__ import annotations

import re
from pathlib import Path

from .project import ProjectLayout

REF_PATTERN = re.compile(r"\\ref\{([^{}]+)\}")


def write_solution(layout: ProjectLayout, final_proposition: Path) -> Path:
    final_path = final_proposition.resolve()
    verified_dir = layout.verified_dir.resolve()
    if final_path.parent != verified_dir:
        raise ValueError(f"final proposition must be in verified_propositions: {final_proposition}")

    ordered: list[Path] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(path: Path) -> None:
        stem = path.stem
        if stem in visited:
            return
        if stem in visiting:
            raise ValueError(f"cyclic proposition reference detected: {stem}")
        visiting.add(stem)
        text = path.read_text(encoding="utf-8")
        for ref in _extract_refs(text):
            dependency = verified_dir / f"{ref}.md"
            if not dependency.is_file():
                raise ValueError(f"missing verified proposition reference: \\ref{{{ref}}}")
            visit(dependency)
        visiting.remove(stem)
        visited.add(stem)
        ordered.append(path)

    visit(final_path)

    out: list[str] = [
        "# Solution",
        "",
        "## Problem",
        "",
        layout.read_problem().strip(),
        "",
        "## Verified Proposition Chain",
        "",
    ]
    for index, prop_path in enumerate(ordered, start=1):
        out.extend(
            [
                f"### {index}. {prop_path.stem}",
                "",
                prop_path.read_text(encoding="utf-8").strip(),
                "",
            ]
        )

    solution_path = layout.problem_path.parent / "solution.md"
    solution_path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    return solution_path


def _extract_refs(text: str) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for match in REF_PATTERN.finditer(text):
        ref = match.group(1).strip()
        if ref and ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return refs
