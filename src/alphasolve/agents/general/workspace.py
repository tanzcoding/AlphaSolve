from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path


class WorkspaceError(ValueError):
    pass


READ_PAGE_DEFAULT_LINES = 30
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
