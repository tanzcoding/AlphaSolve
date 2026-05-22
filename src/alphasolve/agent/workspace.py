from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class WorkspaceLike(Protocol):
    """第二层工具注册器接受的 workspace 鸭子类型。

    Why: 第三层 RoleWorkspaceAccess 不 inherit Workspace（它叠了一层角色权限
    检查），但通过实现这套方法满足同样的契约。第二层 build_default_tool_registry
    （Task 6 引入）接受 WorkspaceLike 后，第三层就能把 RoleWorkspaceAccess
    传进去而无需第二层知道角色概念。
    """
    @property
    def root(self) -> Any: ...
    def read_text_page(self, path: str, *, line_offset: int = ..., n_lines: int = ..., read_all: bool = ...) -> Any: ...
    def write_text(self, path: str, content: str, *, mode: str = ...) -> str: ...
    def edit(self, path: str, old_str: str, new_str: str) -> str: ...
    def glob(self, pattern: str, *, path: str = ..., max_results: int = ...) -> Any: ...
    def grep(self, pattern: str, *, path: str = ..., regex: bool = ..., max_results: int = ..., context_lines: int = ...) -> Any: ...
    def list_dir(self, path: str = ..., *, max_results: int = ...) -> Any: ...
    def make_dir(self, path: str) -> str: ...
    def rename_item(self, directory: str, old_name: str, new_name: str) -> dict[str, str]: ...
    def move_file(self, path: str, destination_dir: str) -> dict[str, str]: ...
    def delete_path(self, path: str) -> str: ...


class WorkspaceError(ValueError):
    pass


READ_PAGE_DEFAULT_LINES = 250
READ_PAGE_MAX_LINES = 1000
READ_PAGE_MAX_LINE_LENGTH = 2000
READ_PAGE_MAX_BYTES = 100 << 10


@dataclass(frozen=True)
class PagedReadResult:
    output: str
    message: str

    def to_tool_content(self) -> str:
        if not self.output:
            return f"<system>{self.message}</system>"
        return f"<system>{self.message}</system>\n{self.output}"


def _truncate_line(line: str, max_length: int, marker: str = "...") -> str:
    if len(line) <= max_length:
        return line
    match = re.search(r"[\r\n]+$", line)
    line_break = match.group(0) if match else ""
    end = marker + line_break
    max_length = max(max_length, len(end))
    return line[: max_length - len(end)] + end


def read_text_page(
    path: Path,
    *,
    line_offset: int = 1,
    n_lines: int = READ_PAGE_DEFAULT_LINES,
    read_all: bool = False,
) -> PagedReadResult:
    if line_offset < 1:
        raise WorkspaceError(f"line_offset must be >= 1: {line_offset}")
    if n_lines < 1:
        raise WorkspaceError(f"n_lines must be >= 1: {n_lines}")
    if not read_all and n_lines > READ_PAGE_MAX_LINES:
        raise WorkspaceError(f"n_lines must be <= {READ_PAGE_MAX_LINES}: {n_lines}")

    lines: list[str] = []
    truncated_line_numbers: list[int] = []
    requested_lines_reached = False
    max_bytes_reached = False
    n_bytes = 0
    current_line_no = 0
    stopped_collecting = False

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            current_line_no += 1
            if stopped_collecting or current_line_no < line_offset:
                continue
            truncated = _truncate_line(line, READ_PAGE_MAX_LINE_LENGTH)
            if truncated != line:
                truncated_line_numbers.append(current_line_no)
            lines.append(truncated)
            n_bytes += len(truncated.encode("utf-8"))
            if not read_all and len(lines) >= n_lines:
                requested_lines_reached = True
                stopped_collecting = True
            elif not read_all and n_bytes >= READ_PAGE_MAX_BYTES:
                max_bytes_reached = True
                stopped_collecting = True

    numbered_lines = [
        f"{line_no:6d}\t{line}"
        for line_no, line in zip(range(line_offset, line_offset + len(lines)), lines, strict=True)
    ]
    output = "".join(numbered_lines)

    if lines:
        message = f"{len(lines)} lines read from file starting from line {line_offset}. File has {current_line_no} total lines."
    else:
        message = f"No lines read from file. File has {current_line_no} total lines."
    if requested_lines_reached and line_offset + len(lines) - 1 < current_line_no:
        message += f" Requested n_lines={n_lines} reached; more lines remain."
    elif max_bytes_reached:
        message += f" Max {READ_PAGE_MAX_BYTES} bytes reached."
    else:
        message += " End of file reached."
    if truncated_line_numbers:
        message += f" Lines {truncated_line_numbers} were truncated."
    return PagedReadResult(output=output, message=message)


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

    def _rel(self, target: Path) -> str:
        # 内部辅助：把绝对路径转为 workspace 相对 POSIX 字符串；根目录返回 "."。
        return target.relative_to(self.root).as_posix() if target != self.root else "."

    def read_text_page(
        self,
        path: str | Path,
        *,
        line_offset: int = 1,
        n_lines: int = READ_PAGE_DEFAULT_LINES,
        read_all: bool = False,
    ) -> PagedReadResult:
        target = self.resolve(path)
        if not target.exists():
            raise WorkspaceError(f"path does not exist: {path}")
        if not target.is_file():
            raise WorkspaceError(f"not a file: {path}")
        return read_text_page(target, line_offset=line_offset, n_lines=n_lines, read_all=read_all)

    def write_text(self, path: str | Path, content: str, *, mode: str = "overwrite") -> str:
        target = self.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        text = str(content)
        if mode == "overwrite":
            target.write_text(text, encoding="utf-8")
        elif mode == "append":
            with target.open("a", encoding="utf-8") as handle:
                handle.write(text)
        else:
            raise WorkspaceError("write mode must be either 'overwrite' or 'append'")
        return self._rel(target)

    def edit(self, path: str, old_str: str, new_str: str) -> str:
        target = self.resolve(path)
        if not target.is_file():
            raise WorkspaceError(f"not a file: {path}")
        text = target.read_text(encoding="utf-8")
        if old_str not in text:
            raise WorkspaceError(f"old_str not found in {path}")
        if text.count(old_str) > 1:
            raise WorkspaceError(f"old_str matches multiple locations in {path}; make it more specific")
        target.write_text(text.replace(old_str, new_str, 1), encoding="utf-8")
        return self._rel(target)

    def make_dir(self, path: str) -> str:
        target = self.resolve(path)
        target.mkdir(parents=True, exist_ok=True)
        return self._rel(target)

    def rename_item(self, directory: str, old_name: str, new_name: str) -> dict[str, str]:
        # old_name / new_name 必须是纯文件/目录名，不允许包含路径分隔符，避免越权。
        if "/" in old_name or "\\" in old_name or "/" in new_name or "\\" in new_name:
            raise WorkspaceError("old_name and new_name must be plain names, not paths")
        if old_name in {"", ".", ".."} or new_name in {"", ".", ".."}:
            raise WorkspaceError("old_name and new_name must be plain names")
        parent = self.resolve(directory)
        if not parent.is_dir():
            raise WorkspaceError(f"not a directory: {directory}")
        source = parent / old_name
        target = parent / new_name
        if not source.exists():
            raise WorkspaceError(f"source does not exist: {old_name}")
        if target.exists():
            raise WorkspaceError(f"target already exists: {new_name}")
        source.rename(target)
        return {"old_path": self._rel(source), "path": self._rel(target)}

    def move_file(self, path: str, destination_dir: str) -> dict[str, str]:
        source = self.resolve(path)
        if not source.is_file():
            raise WorkspaceError(f"not a file: {path}")
        destination = self.resolve(destination_dir)
        if not destination.is_dir():
            raise WorkspaceError(f"not a directory: {destination_dir}")
        target = destination / source.name
        if source == target:
            return {"old_path": self._rel(source), "path": self._rel(target)}
        if target.exists():
            raise WorkspaceError(f"target already exists: {self._rel(target)}")
        old_rel = self._rel(source)
        source.rename(target)
        return {"old_path": old_rel, "path": self._rel(target)}

    def delete_path(self, path: str) -> str:
        target = self.resolve(path)
        if not target.exists():
            raise WorkspaceError(f"path does not exist: {path}")
        rel = self._rel(target)
        if target.is_dir():
            if any(target.iterdir()):
                raise WorkspaceError(f"directory is not empty: {path}")
            target.rmdir()
        else:
            target.unlink()
        return rel

    def list_dir(self, path: str | Path = ".", *, max_results: int = 200) -> list[str]:
        target = self.resolve(path)
        if not target.is_dir():
            raise WorkspaceError(f"not a directory: {path}")
        out: list[str] = []
        for child in sorted(target.iterdir(), key=lambda item: item.name.lower()):
            out.append(child.name + ("/" if child.is_dir() else ""))
            if len(out) >= max_results:
                break
        return out

    def glob(self, pattern: str, *, path: str = ".", max_results: int = 100) -> list[str]:
        target = self.resolve(path)
        if not target.is_dir():
            raise WorkspaceError(f"not a directory: {path}")
        out: list[str] = []
        recursive = "**" in pattern
        match_func = target.rglob if recursive else target.glob
        for child_path in sorted(match_func(pattern)):
            if child_path.is_file():
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
            raise WorkspaceError("pattern must not be empty")
        root = self.resolve(path)
        if not root.exists():
            raise WorkspaceError(f"path does not exist: {path}")
        if root.is_file():
            files: list[Path] = [root]
        else:
            files = []
            for current, dirs, file_names in os.walk(root):
                dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", ".venv", "node_modules"}]
                for filename in file_names:
                    files.append(Path(current) / filename)
        out: list[dict[str, Any]] = []
        compiled = re.compile(pattern) if regex else None
        for file_path in files:
            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeDecodeError):
                continue
            lines = text.splitlines()
            for index, line in enumerate(lines, start=1):
                hit = bool(compiled.search(line)) if compiled else pattern in line
                if not hit:
                    continue
                start = max(1, index - int(context_lines))
                end = min(len(lines), index + int(context_lines))
                out.append({
                    "path": self._rel(file_path),
                    "line": index,
                    "text": line,
                    "context": "\n".join(
                        f"{line_no}: {lines[line_no - 1]}" for line_no in range(start, end + 1)
                    ),
                })
                if len(out) >= max_results:
                    return out
        return out

    def search_files(self, pattern: str, *, path: str | Path = ".", max_results: int = 50) -> list[str]:
        """已废弃：保留为兼容旧测试。请使用 glob。"""
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
