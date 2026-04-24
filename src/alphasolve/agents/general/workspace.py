from __future__ import annotations

import fnmatch
import os
from pathlib import Path


class WorkspaceError(ValueError):
    pass


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def resolve(self, path: str | Path = ".") -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise WorkspaceError(f"path escapes workspace: {path}")
        return resolved

    def read_text(self, path: str | Path, *, max_chars: int = 20000) -> str:
        target = self.resolve(path)
        if not target.is_file():
            raise WorkspaceError(f"not a file: {path}")
        text = target.read_text(encoding="utf-8")
        if max_chars > 0 and len(text) > max_chars:
            return text[:max_chars] + "\n[truncated]"
        return text

    def write_text(self, path: str | Path, content: str) -> str:
        target = self.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return str(target)

    def list_dir(self, path: str | Path = ".") -> list[str]:
        target = self.resolve(path)
        if not target.is_dir():
            raise WorkspaceError(f"not a directory: {path}")
        return sorted(child.name + ("/" if child.is_dir() else "") for child in target.iterdir())

    def search_files(self, pattern: str, *, path: str | Path = ".", max_results: int = 50) -> list[str]:
        root = self.resolve(path)
        if not root.is_dir():
            raise WorkspaceError(f"not a directory: {path}")

        matches: list[str] = []
        for current, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", ".venv", "node_modules"}]
            for filename in files:
                file_path = Path(current) / filename
                rel = file_path.relative_to(self.root).as_posix()
                if fnmatch.fnmatch(filename, pattern) or fnmatch.fnmatch(rel, pattern):
                    matches.append(rel)
                    if len(matches) >= max_results:
                        return matches
        return matches
