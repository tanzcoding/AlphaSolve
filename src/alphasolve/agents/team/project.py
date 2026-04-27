from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ProjectLayout:
    project_root: Path
    problem_path: Path
    hint_path: Optional[Path]
    workspace_dir: Path
    logs_dir: Path
    knowledge_dir: Path
    unverified_dir: Path
    verified_dir: Path

    @classmethod
    def create(
        cls,
        project_dir: str | Path,
        *,
        problem: str | Path = "problem.md",
        hint: str | Path | None = None,
    ) -> "ProjectLayout":
        project_root = Path(project_dir).resolve()
        problem_path = _resolve_under(project_root, problem)
        hint_path = _resolve_under(project_root, hint) if hint is not None else project_root / "hint.md"
        if hint_path is not None and not hint_path.exists():
            hint_path = None

        workspace_dir = project_root / "workspace"
        return cls(
            project_root=project_root,
            problem_path=problem_path,
            hint_path=hint_path,
            workspace_dir=workspace_dir,
            logs_dir=project_root / "logs",
            knowledge_dir=workspace_dir / "knowledge",
            unverified_dir=workspace_dir / "unverified_propositions",
            verified_dir=workspace_dir / "verified_propositions",
        )

    def ensure(self) -> None:
        self.project_root.mkdir(parents=True, exist_ok=True)
        if not self.problem_path.is_file():
            raise FileNotFoundError(f"problem file not found: {self.problem_path}")
        for path in (
            self.workspace_dir,
            self.logs_dir,
            self.knowledge_dir,
            self.unverified_dir,
            self.verified_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.problem_path, self.workspace_dir / "problem.md")

    def read_problem(self) -> str:
        return self.problem_path.read_text(encoding="utf-8")

    def read_hint(self) -> str | None:
        if self.hint_path is None or not self.hint_path.is_file():
            return None
        return self.hint_path.read_text(encoding="utf-8")


def _resolve_under(root: Path, path: str | Path | None) -> Path:
    if path is None:
        raise ValueError("path must not be None")
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"path escapes project: {path}")
    return resolved
