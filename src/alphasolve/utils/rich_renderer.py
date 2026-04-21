"""Rich-based terminal rendering helpers for AlphaSolve CLI.

Provides compact, kimi-cli-style live displays for:
- Reasoning/thinking content (scrolling preview with spinner)
- Tool calls (compact name + args, animated while running)
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from rich.console import Console, Group
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

if TYPE_CHECKING:
    from rich.console import RenderableType

RICH_CONSOLE = Console()

# Animated bullet frames after the "Thinking" label.
_BULLET_FRAMES = (".  ", ".. ", "...", " ..", "  .", "   ")
_BULLET_FRAME_INTERVAL = 0.13  # seconds per frame
_THINKING_PREVIEW_LINES = 4


def _bullet_frame_for(elapsed: float) -> str:
    """Select the current bullet frame from wall-clock elapsed time."""
    idx = int(elapsed / _BULLET_FRAME_INTERVAL) % len(_BULLET_FRAMES)
    return _BULLET_FRAMES[idx]


def _tail_lines(text: str, n: int) -> str:
    """Extract the last *n* lines from *text* via reverse scanning."""
    pos = len(text)
    for _ in range(n):
        pos = text.rfind("\n", 0, pos)
        if pos == -1:
            return text
    return text[pos + 1 :]


def compose_thinking_live(thinking_text: str, elapsed: float) -> RenderableType:
    """Compose the transient Live display for reasoning content.

    Shows a ``dots`` spinner with elapsed time / char count followed by
    a grey italic scrolling preview of the last few lines of raw thinking text.
    """
    frame = _bullet_frame_for(elapsed)
    header = Text.assemble(
        ("Thinking", "italic"),
        (f" {frame}", "cyan"),
        (f"  {elapsed:.1f}s", "grey50"),
        (f" · {len(thinking_text)} chars", "grey50"),
    )
    spinner = Spinner("dots", text=header)
    if not thinking_text:
        return spinner
    preview = _tail_lines(thinking_text, _THINKING_PREVIEW_LINES)
    return Group(spinner, Text(preview, style="grey50 italic"))


def compose_thinking_final(elapsed: float, char_count: int) -> Text:
    """Compose the one-line summary committed after thinking ends."""
    return Text(
        f"Thought for {elapsed:.1f}s · {char_count} chars",
        style="grey50 italic",
    )


def build_tool_using_text(name: str, arg_preview: str) -> Spinner:
    """Render an in-progress tool call: ``Using <name> (<args>)`` with spinner."""
    text = Text()
    text.append("Using ", style="")
    text.append(name, style="blue")
    if arg_preview:
        text.append(" (", style="grey50")
        text.append(arg_preview, style="grey50")
        text.append(")", style="grey50")
    return Spinner("dots", text=text)


def build_tool_used_text(name: str, arg_preview: str, is_error: bool = False) -> Text:
    """Render a finished tool call: ``● Used <name> (<args>)``.

    Bullet is green on success, dark_red on error.
    """
    bullet_style = "dark_red" if is_error else "green"
    text = Text()
    text.append("● ", style=bullet_style)
    text.append("Used ", style="")
    text.append(name, style="blue")
    if arg_preview:
        text.append(" (", style="grey50")
        text.append(arg_preview, style="grey50")
        text.append(")", style="grey50")
    if is_error:
        text.append(" error", style="dark_red")
    return text
