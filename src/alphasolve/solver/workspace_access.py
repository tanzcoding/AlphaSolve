"""第三层角色级 workspace 访问层：在通用 Workspace 之上叠加角色权限检查。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alphasolve.agent import Workspace
from alphasolve.agent.workspace import READ_PAGE_DEFAULT_LINES, PagedReadResult, read_text_page


@dataclass
class RoleWorkspaceAccess:
    workspace: Workspace
    worker_rel: str | None = None
    deny_other_unverified: bool = False
    read_root_rel: str | None = None
    write_root_rel: str | None = None
    deny_read_rel: str | None = None  # deny reads under this subtree (used to block other verifier attempt dirs)
    deny_read_rels: tuple[str, ...] = ()
    deny_read_file_names: tuple[str, ...] = ()
    exact_write_rel: str | None = None
    single_proposition_file: bool = False
    allowed_extensions: tuple[str, ...] = (".md", ".py", ".lean")
    destructive_protected_file_names: tuple[str, ...] = ()
    preserve_markdown_file_names_on_rename: bool = False

    def __post_init__(self) -> None:
        self._locked_proposition_rel: str | None = None
        self._touched_paths: set[Path] = set()

    def read_text_page(
        self,
        path: str,
        *,
        line_offset: int = 1,
        n_lines: int = READ_PAGE_DEFAULT_LINES,
        read_all: bool = False,
    ) -> PagedReadResult:
        target = self._resolve_readable_file(path)
        return read_text_page(target, line_offset=line_offset, n_lines=n_lines, read_all=read_all)

    def write_text(self, path: str, content: str, *, mode: str = "overwrite") -> str:
        target = self._resolve_writable_file(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        text = str(content)
        if mode == "overwrite":
            target.write_text(text, encoding="utf-8")
        elif mode == "append":
            with target.open("a", encoding="utf-8") as handle:
                handle.write(text)
        else:
            raise ValueError("write mode must be either 'overwrite' or 'append'")
        self._record_touch(target)
        return self._rel(target)

    def edit(self, path: str, old_str: str, new_str: str) -> str:
        target = self._resolve_writable_file(path, must_exist=True)
        text = target.read_text(encoding="utf-8")
        if old_str not in text:
            raise ValueError(f"old_str not found in {path}")
        if text.count(old_str) > 1:
            raise ValueError(f"old_str matches multiple locations in {path}; make it more specific")
        target.write_text(text.replace(old_str, new_str, 1), encoding="utf-8")
        self._record_touch(target)
        return self._rel(target)

    def rename_path(self, old_path: str, new_path: str) -> dict[str, str]:
        source = self._resolve_manageable_path(old_path, must_exist=True, destructive=True)
        target = self._resolve_manageable_path(
            new_path,
            destructive=True,
            expect_dir=source.is_dir(),
        )
        if source.parent != target.parent:
            raise ValueError("Rename can only change a name within the same directory; use Move to change directories")
        old_rel = self._rel(source)
        if source == target:
            return {"old_path": old_rel, "path": self._rel(target)}
        if (
            self.preserve_markdown_file_names_on_rename
            and source.is_file()
            and source.suffix.lower() == ".md"
            and source.name != target.name
        ):
            raise ValueError("cannot rename .md files; move them without changing the file name")
        if target.exists():
            raise ValueError(f"target already exists: {new_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        self._record_touch(target)
        return {"old_path": old_rel, "path": self._rel(target)}

    def rename_item(self, directory: str, old_name: str, new_name: str) -> dict[str, str]:
        if self._has_path_separator(old_name) or self._has_path_separator(new_name):
            raise ValueError("old_name and new_name must be plain names, not paths")
        if old_name in {"", ".", ".."} or new_name in {"", ".", ".."}:
            raise ValueError("old_name and new_name must be plain names")
        parent = self._resolve_writable_directory(directory, must_exist=True)
        return self.rename_path(self._rel(parent / old_name), self._rel(parent / new_name))

    def move_file(self, path: str, destination_dir: str) -> dict[str, str]:
        source = self._resolve_writable_file(path, must_exist=True, destructive=True)
        destination = self._resolve_writable_directory(destination_dir, must_exist=True)
        target = destination / source.name
        if source == target:
            return {"old_path": self._rel(source), "path": self._rel(target)}
        if target.exists():
            raise ValueError(f"target already exists: {self._rel(target)}")
        old_rel = self._rel(source)
        source.rename(target)
        self._record_touch(target)
        new_rel = self._rel(target)
        self._update_verified_references_after_move(old_rel, new_rel)
        return {"old_path": old_rel, "path": new_rel}

    def make_dir(self, path: str) -> str:
        target = self._resolve_writable_directory(path)
        target.mkdir(parents=True, exist_ok=True)
        return self._rel(target)

    def delete_path(self, path: str) -> str:
        target = self._resolve_manageable_path(path, must_exist=True, destructive=True)
        rel = self._rel(target)
        if target.is_dir():
            if any(target.iterdir()):
                raise ValueError(f"directory is not empty: {path}")
            target.rmdir()
        else:
            target.unlink()
        return rel

    def delete_file(self, path: str) -> str:
        return self.delete_path(path)

    def touched_paths(self) -> tuple[Path, ...]:
        return tuple(sorted(self._touched_paths, key=lambda item: item.as_posix()))

    def _update_verified_references_after_move(self, old_rel: str, new_rel: str) -> None:
        prefix = "verified_propositions/"
        if not (old_rel.startswith(prefix) and new_rel.startswith(prefix)):
            return
        if not (old_rel.endswith(".md") and new_rel.endswith(".md")):
            return

        old_label = old_rel[len(prefix):-3]
        new_label = new_rel[len(prefix):-3]
        if old_label == new_label:
            return

        old_labels = {old_label, old_label.replace("/", "\\")}
        new_ref = "\\ref{" + new_label.replace("/", "\\") + "}"
        verified_root = self.workspace.resolve("verified_propositions")
        if not verified_root.is_dir():
            return

        for current, dirs, files in os.walk(verified_root):
            dirs[:] = sorted(name for name in dirs if name not in {".git", "__pycache__", ".venv", "node_modules"})
            for name in sorted(files):
                file_path = Path(current) / name
                if file_path.suffix.lower() != ".md":
                    continue
                text = file_path.read_text(encoding="utf-8")
                new_text = text
                for label in sorted(old_labels, key=len, reverse=True):
                    new_text = new_text.replace("\\ref{" + label + "}", new_ref)
                if new_text != text:
                    file_path.write_text(new_text, encoding="utf-8")
                    self._record_touch(file_path)

    def list_dir(self, path: str = ".", *, max_results: int = 200) -> list[str]:
        target = self.workspace.resolve(path)
        self._ensure_under_read_root(target)
        self._ensure_not_other_worker_path(target)
        if not target.is_dir():
            raise ValueError(f"not a directory: {path}")

        out: list[str] = []
        for child in sorted(target.iterdir(), key=lambda item: item.name.lower()):
            if self._is_other_worker_path(child):
                continue
            if self._is_denied_read_path(child):
                continue
            if self._is_denied_read_file(child):
                continue
            out.append(self._rel(child) + ("/" if child.is_dir() else ""))
            if len(out) >= max_results:
                break
        return out

    def glob(self, pattern: str, *, path: str = ".", max_results: int = 100) -> list[str]:
        """Fast file pattern matching, e.g. ``*.md`` or ``**/*.py``."""
        target = self.workspace.resolve(path)
        self._ensure_under_read_root(target)
        self._ensure_not_other_worker_path(target)
        if not target.is_dir():
            raise ValueError(f"not a directory: {path}")

        out: list[str] = []
        recursive = "**" in pattern
        match_func = target.rglob if recursive else target.glob
        for child_path in sorted(match_func(pattern)):
            if self._is_other_worker_path(child_path):
                continue
            if self._is_denied_read_file(child_path):
                continue
            if child_path.is_file() and self._extension_allowed(child_path):
                out.append(self._rel(child_path))
            elif child_path.is_dir() and not child_path.name.startswith("."):
                out.append(self._rel(child_path) + "/")
            if len(out) >= max_results:
                break
        return out

    def grep(
        self,
        pattern: str,
        *,
        path: str = ".",
        regex: bool = True,
        max_results: int = 50,
        context_lines: int = 0,
    ) -> list[dict[str, Any]]:
        if not pattern:
            raise ValueError("pattern must not be empty")
        root = self.workspace.resolve(path)
        self._ensure_under_read_root(root)
        self._ensure_not_other_worker_path(root)
        if not root.exists():
            raise ValueError(f"path does not exist: {path}")

        files = [root] if root.is_file() else self._iter_text_files(root)
        out: list[dict[str, Any]] = []
        compiled = re.compile(pattern) if regex else None
        for file_path in files:
            if self._is_other_worker_path(file_path) or not self._extension_allowed(file_path):
                continue
            if self._is_denied_read_file(file_path):
                continue
            lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
            for index, line in enumerate(lines, start=1):
                hit = bool(compiled.search(line)) if compiled else pattern in line
                if not hit:
                    continue
                start = max(1, index - int(context_lines))
                end = min(len(lines), index + int(context_lines))
                out.append(
                    {
                        "path": self._rel(file_path),
                        "line": index,
                        "text": line,
                        "context": "\n".join(
                            f"{line_no}: {lines[line_no - 1]}" for line_no in range(start, end + 1)
                        ),
                    }
                )
                if len(out) >= max_results:
                    return out
        return out

    def _resolve_readable_file(self, path: str) -> Path:
        target = self.workspace.resolve(path)
        self._ensure_under_read_root(target)
        self._ensure_not_other_worker_path(target)
        if self._is_denied_read_file(target):
            raise ValueError(f"read access to {target.name} is denied for this agent")
        if not target.is_file():
            raise ValueError(f"not a file: {path}")
        if not self._extension_allowed(target):
            raise ValueError(f"file extension is not allowed: {path}")
        return target

    def _resolve_writable_file(
        self,
        path: str,
        *,
        must_exist: bool = False,
        destructive: bool = False,
    ) -> Path:
        target = self.workspace.resolve(path)
        if self.exact_write_rel is not None:
            exact = self.workspace.resolve(self.exact_write_rel)
            if target != exact:
                raise ValueError(f"write_file can only rewrite {self.exact_write_rel}")
            return self._finalize_writable_target(
                target,
                path=path,
                must_exist=must_exist,
                destructive=destructive,
            )

        if self.single_proposition_file:
            if self.worker_rel is None:
                raise ValueError("single proposition write requires worker_rel")
            worker_dir = self.workspace.resolve(self.worker_rel)
            proposition_path = worker_dir / "proposition.md"
            if target != proposition_path:
                raise ValueError("generator can only write proposition.md in its own directory")
            self._locked_proposition_rel = self._rel(proposition_path)
            return self._finalize_writable_target(
                target,
                path=path,
                must_exist=must_exist,
                destructive=destructive,
            )

        if self.write_root_rel is None:
            raise ValueError("write_file is not enabled for this agent")
        self._ensure_under_root(target, self.write_root_rel, kind="write")
        return self._finalize_writable_target(
            target,
            path=path,
            must_exist=must_exist,
            destructive=destructive,
        )

    def _resolve_manageable_path(
        self,
        path: str,
        *,
        must_exist: bool = False,
        destructive: bool = False,
        expect_dir: bool | None = None,
    ) -> Path:
        target = self.workspace.resolve(path)
        if self.write_root_rel is None:
            raise ValueError("write_file is not enabled for this agent")
        self._ensure_under_root(target, self.write_root_rel, kind="write")
        if target == self.workspace.resolve(self.write_root_rel):
            raise ValueError("cannot rename the writable root directory")
        if destructive and target.name in set(self.destructive_protected_file_names):
            raise ValueError(f"destructive operations are not allowed on {target.name}")
        if must_exist and not target.exists():
            raise ValueError(f"path does not exist: {path}")
        if target.exists():
            if not (target.is_file() or target.is_dir()):
                raise ValueError(f"not a file or directory: {path}")
            if expect_dir is True and not target.is_dir():
                raise ValueError(f"not a directory: {path}")
            if expect_dir is False and not target.is_file():
                raise ValueError(f"not a file: {path}")
            if target.is_file() and not self._extension_allowed(target):
                raise ValueError(f"file extension is not allowed: {path}")
        elif expect_dir is not True and not self._extension_allowed(target):
            raise ValueError(f"file extension is not allowed: {path}")
        return target

    def _resolve_writable_directory(self, path: str, *, must_exist: bool = False) -> Path:
        target = self.workspace.resolve(path)
        if self.write_root_rel is None:
            raise ValueError("write_file is not enabled for this agent")
        self._ensure_under_root(target, self.write_root_rel, kind="write")
        if target == self.workspace.resolve(self.write_root_rel) and not must_exist:
            raise ValueError("cannot create the writable root directory")
        if must_exist and not target.is_dir():
            raise ValueError(f"directory does not exist: {path}")
        if target.exists() and not target.is_dir():
            raise ValueError(f"not a directory: {path}")
        return target

    @staticmethod
    def _has_path_separator(name: str) -> bool:
        return "/" in name or "\\" in name

    def _finalize_writable_target(
        self,
        target: Path,
        *,
        path: str,
        must_exist: bool,
        destructive: bool,
    ) -> Path:
        if destructive and target.name in set(self.destructive_protected_file_names):
            raise ValueError(f"destructive operations are not allowed on {target.name}")
        if must_exist and not target.is_file():
            raise ValueError(f"not a file: {path}")
        if target.exists() and not target.is_file():
            raise ValueError(f"not a file: {path}")
        if not self._extension_allowed(target):
            raise ValueError(f"file extension is not allowed: {path}")
        return target

    def _record_touch(self, path: Path) -> None:
        self._touched_paths.add(path.resolve())

    def _iter_text_files(self, root: Path):
        for current, dirs, files in os.walk(root):
            current_path = Path(current)
            dirs[:] = [
                name
                for name in dirs
                if name not in {".git", "__pycache__", ".venv", "node_modules"}
                and not self._is_other_worker_path(current_path / name)
                and not self._is_denied_read_path(current_path / name)
            ]
            for filename in files:
                file_path = current_path / filename
                if not self._is_denied_read_path(file_path) and not self._is_denied_read_file(file_path):
                    yield file_path

    def _extension_allowed(self, path: Path) -> bool:
        return path.suffix.lower() in self.allowed_extensions

    def _rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.workspace.root).as_posix()

    def _ensure_not_other_worker_path(self, path: Path) -> None:
        if self._is_other_worker_path(path):
            raise ValueError(f"access to other unverified proposition worker directories is denied: {self._rel(path)}")

    def _ensure_under_root(self, path: Path, root_rel: str, *, kind: str = "path") -> None:
        root = self.workspace.resolve(root_rel)
        if path != root and root not in path.parents:
            raise ValueError(f"{kind} path must stay under {root_rel}")

    def _ensure_under_read_root(self, path: Path) -> None:
        if self.read_root_rel is not None:
            self._ensure_under_root(path, self.read_root_rel, kind="read")
        for deny_rel in self._denied_read_roots():
            if self._is_under_rel(path, deny_rel):
                raise ValueError(f"read access to {deny_rel} is denied for this agent")

    def _is_other_worker_path(self, path: Path) -> bool:
        if not self.deny_other_unverified:
            return False
        rel = self._rel(path)
        prefix = "unverified_propositions/"
        if not (rel == "unverified_propositions" or rel.startswith(prefix)):
            return False
        if self.worker_rel is None:
            return True
        worker_rel = self.worker_rel.strip("/")
        return not (rel == worker_rel or rel.startswith(worker_rel + "/") or rel == "unverified_propositions")

    def _is_denied_read_path(self, path: Path) -> bool:
        return any(self._is_under_rel(path, deny_rel) for deny_rel in self._denied_read_roots())

    def _is_denied_read_file(self, path: Path) -> bool:
        return path.is_file() and path.name in set(self.deny_read_file_names)

    def _denied_read_roots(self) -> tuple[str, ...]:
        roots = list(self.deny_read_rels)
        if self.deny_read_rel is not None:
            roots.append(self.deny_read_rel)
        return tuple(root for root in roots if root)

    def _is_under_rel(self, path: Path, root_rel: str) -> bool:
        root = self.workspace.resolve(root_rel)
        return path == root or root in path.parents
