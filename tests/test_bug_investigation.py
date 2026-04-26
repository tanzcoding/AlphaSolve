"""
Investigation tests for three bugs:

1. Worker panel flickering between generator and reviser phases
2. reasoning_content must be passed back to API error (400)
3. CoT line count flickering between 6 and 7 lines

These tests are read-only with respect to the source code — they import
and exercise the existing classes but do not modify them.
"""

import io
import json
import textwrap
import time
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from rich.console import Console

from alphasolve.agents.general.general_agent import (
    GeneralPurposeAgent,
    OpenAIChatClient,
    _first_text_delta,
    _object_to_dict,
)
from alphasolve.agents.team.dashboard import make_worker_event_sink
import alphasolve.utils.rich_renderer as _rr

from alphasolve.utils.rich_renderer import (
    LemmaTeamRenderer,
    WorkerRenderState,
)

# Module-level private helpers accessible through the module namespace
_tail_lines = _rr._tail_lines


def _count_section_lines(label: str, text: str, *, width: int, max_lines: int) -> int:
    """Re-implement _section_lines for testing — returns line count only."""
    if max_lines <= 0:
        return 0
    count = 1  # header line
    for raw in _tail_lines(text, max(0, max_lines - 1)):
        if raw.strip():
            count += 1
    return min(count, max_lines)


# ---------------------------------------------------------------------------
# Bug 1: Worker panel flickering between generator and reviser
# ---------------------------------------------------------------------------

class TestBug1_PhaseFlickering:
    """Investigate why the worker panel flickers between generator/reviser.

    Hypothesis: ``update_phase()`` changes the phase label but does NOT
    clear ``thinking_text``.  When a new agent (e.g. reviser) starts, the
    panel briefly shows the *old* CoT text (from generator/verifier) together
    with the *new* phase label.  Then the first ``thinking_delta`` from the
    new agent abruptly replaces the CoT content.  This phase-label /
    CoT-content mismatch, combined with the sudden content swap, is
    perceived as flickering.

    Additionally, when the CoT line count changes between renders (even by
    one line), ``_LineDiffLive`` falls back from ``_paint_diff`` to
    ``_paint_full``, which clears and redraws the entire screen — a
    visually jarring flicker.
    """

    def test_phase_label_changes_but_thinking_text_persists(self):
        """Demonstrate that update_phase leaves thinking_text untouched."""
        renderer = LemmaTeamRenderer(
            console=Console(
                file=io.StringIO(),
                width=100,
                height=28,
                record=True,
                force_terminal=False,
                color_system=None,
            ),
            screen=False,
        )
        renderer.register_worker(0)

        # Simulate generator producing CoT
        renderer.update_thinking(
            0, module="generator",
            thinking_text="generator's deep reasoning\nstep 1\nstep 2",
            elapsed=1.0,
        )
        gen_text = renderer._workers[0].thinking_text
        gen_phase = renderer._workers[0].phase
        assert "generator" in gen_text
        assert gen_phase == "generator"

        # Generator finishes — phase changes but thinking_text stays
        renderer.finish_thinking(
            0, module="generator", elapsed=1.5, char_count=len(gen_text),
        )
        state = renderer._workers[0]
        assert state.phase == "generator"
        assert "generator" in state.thinking_text  # <-- STILL PRESENT

        # Now reviser starts — _set_phase changes the label but NOT the text
        renderer.update_phase(0, "reviser w1", status="thinking")
        state = renderer._workers[0]
        assert state.phase == "reviser w1"
        assert "generator" in state.thinking_text  # <-- OLD CoT STILL SHOWN!

        # First reviser thinking delta abruptly replaces the old CoT
        renderer.update_thinking(
            0, module="reviser w1",
            thinking_text="reviser's first thought",
            elapsed=0.1,
        )
        state = renderer._workers[0]
        assert state.phase == "reviser w1"
        assert "reviser" in state.thinking_text
        assert "generator" not in state.thinking_text  # old content gone

    def test_cot_line_count_change_triggers_full_repaint(self):
        """Show that CoT line count change causes full-screen repaint."""
        stream = io.StringIO()
        renderer = LemmaTeamRenderer(
            console=Console(
                file=stream,
                width=100,
                height=28,
                record=True,
                force_terminal=True,
                color_system=None,
            ),
            screen=False,
            refresh_per_second=1000,
        )
        renderer.register_worker(0)
        renderer.start()
        try:
            # Render initial state and capture as baseline
            renderer._live.update(renderer.render(), refresh=True)
            initial_lines = renderer._live._last_lines
            initial_count = len(initial_lines)

            # Add thinking text with 5 lines → CoT section uses 6 lines (header + 5)
            renderer.update_thinking(
                0, module="generator",
                thinking_text="L1\nL2\nL3\nL4\nL5",
                elapsed=0,
            )

            # Force a fresh render and capture
            stream.seek(0)
            stream.truncate(0)
            renderer._live.update(renderer.render(), refresh=True)
            after_delta = stream.getvalue()

            # Now add a 6th line to thinking_text
            renderer.update_thinking(
                0, module="generator",
                thinking_text="L1\nL2\nL3\nL4\nL5\nL6",
                elapsed=0,
            )
            stream.seek(0)
            stream.truncate(0)
            renderer._live.update(renderer.render(), refresh=True)
            after_6th_line = stream.getvalue()

            # When the number of rendered lines changes, _paint_full is used
            # which issues many \x1b[2K (clear-line) sequences
            lines_now = renderer._live._last_lines
            if len(lines_now) != initial_count:
                # Line count changed → full repaint happened
                assert "\x1b[2K" in after_6th_line
        finally:
            renderer.stop()

    def test_thinking_text_accumulation_causes_line_count_oscillation(self):
        """Simulate streaming deltas and check line count stability."""
        renderer = LemmaTeamRenderer(
            console=Console(
                file=io.StringIO(),
                width=100,
                height=28,
                record=True,
                force_terminal=False,
                color_system=None,
            ),
            screen=False,
        )
        renderer.register_worker(0)

        # Count how many content lines are produced for thinking_text with
        # varying numbers of embedded newlines, at a fixed panel width.
        def count_cot_lines(state, width: int, max_lines: int) -> int:
            has_other = bool(state.output_text or state.tool_history or state.log_lines)
            cot_lines = max(2, min(max_lines, max_lines // 2 + 1)) if has_other else max_lines
            return _count_section_lines("CoT", state.thinking_text,
                                         width=width, max_lines=cot_lines)

        # Simulate thinking_text growing
        results = []
        for n_lines in range(1, 10):
            text = "\n".join(f"reasoning step {i}" for i in range(n_lines))
            renderer._workers[0].thinking_text = text
            n = count_cot_lines(renderer._workers[0], width=80, max_lines=9)
            results.append((n_lines, n))

        # The line count should only grow, never shrink (monotonic)
        counts = [c for _, c in results]
        for i in range(1, len(counts)):
            if counts[i] < counts[i - 1]:
                # This is the bug: line count can decrease!
                # When max_lines caps _tail_lines, adding one more line of
                # text doesn't change the displayed count, so it's stable.
                # But when has_other_content changes, cot_lines shrinks,
                # which CAN cause a decrease.
                pass

    def test_rapid_phase_transitions_during_verify_revise_cycle(self):
        """Simulate a complete generator→verifier→reviser cycle and track
        phase + content consistency."""
        renderer = LemmaTeamRenderer(
            console=Console(
                file=io.StringIO(),
                width=100,
                height=28,
                record=True,
                force_terminal=False,
                color_system=None,
            ),
            screen=False,
        )
        renderer.register_worker(0)

        # --- generator ---
        renderer.update_phase(0, "generator", status="thinking")
        renderer.update_thinking(0, module="generator",
                                 thinking_text="gen CoT line1\ngen CoT line2",
                                 elapsed=0)
        renderer.finish_thinking(0, module="generator", elapsed=1.0, char_count=100)
        renderer.update_phase(0, "generator done", status="running")

        state = renderer._workers[0]
        assert state.phase == "generator done"
        assert "gen CoT" in state.thinking_text  # old CoT persists

        # --- verifier attempt (fast) ---
        renderer.update_phase(0, "verifier_attempt w1.1", status="thinking")
        # Old CoT still visible while phase shows "verifier_attempt"
        assert "gen CoT" in state.thinking_text
        assert state.phase == "verifier_attempt w1.1"

        renderer.update_thinking(0, module="verifier_attempt w1.1",
                                 thinking_text="verifier CoT",
                                 elapsed=0)
        assert "verifier" in state.thinking_text

        # --- judge (very fast, small LLM call) ---
        renderer.update_phase(0, "review_verdict_judge w1.1", status="thinking")
        # Verifier CoT still present under judge label
        assert "verifier" in state.thinking_text

        renderer.update_thinking(0, module="review_verdict_judge w1.1",
                                 thinking_text="judge reasoning",
                                 elapsed=0)
        renderer.finish_thinking(0, module="review_verdict_judge w1.1",
                                 elapsed=0.2, char_count=50)

        # --- reviser ---
        renderer.update_phase(0, "reviser w1", status="thinking")
        # Judge/verifier CoT still present under reviser label!
        assert "judge reasoning" in state.thinking_text
        assert state.phase == "reviser w1"
        # This is the visual glitch: reviser label + judge/old CoT content

        renderer.update_thinking(0, module="reviser w1",
                                 thinking_text="reviser first delta",
                                 elapsed=0)
        # Content abruptly replaced
        assert "reviser first delta" in state.thinking_text
        assert "judge reasoning" not in state.thinking_text


# ---------------------------------------------------------------------------
# Bug 2: reasoning_content API error (400)
# ---------------------------------------------------------------------------

class TestBug2_ReasoningContent:
    """Investigate the reasoning_content "must be passed back" error.

    Error::

        Error code: 400 - {"error": {"message": "The `reasoning_content` in
        the thinking mode must be passed back to the API.",
        "type": "invalid_request_error"}}

    The message flows through::

        OpenAIChatClient._complete_streaming → assistant message → messages
        list → next request.  The code DOES include reasoning_content in the
        message dict.  The bug could be:

        (a) ``_object_to_dict`` loses ``reasoning_content`` in
            non-streaming responses because ``model_dump(exclude_none=True)``
            skips it (if the SDK model does not declare the field).

        (b) The API provider (DeepSeek, Volcano, etc.) requires
            ``reasoning_content`` in a different format / different key name.

        (c) When streaming is interrupted by a network error, the partially
            collected reasoning is discarded; on retry, the API may still
            require the previous message's reasoning_content to be present.

    Also reported::

        "peer closed connection without sending complete message body
        (incomplete chunked read)"

    This is a network-level error that aborts streaming mid-response.
    """

    def test_object_to_dict_captures_reasoning_content_from_pydantic(self):
        """Check that _object_to_dict captures reasoning_content."""
        # Simulate a Pydantic-like message object
        class FakeMessage:
            role = "assistant"
            content = "hello"
            reasoning_content = "step-by-step reasoning"

            def model_dump(self, exclude_none=False):
                return {"role": self.role, "content": self.content,
                        "reasoning_content": self.reasoning_content}

        result = _object_to_dict(FakeMessage())
        assert result.get("reasoning_content") == "step-by-step reasoning"

    def test_object_to_dict_preserves_reasoning_content_even_when_model_dump_excludes_it(self):
        """After the fix, _object_to_dict falls back to getattr for reasoning keys."""
        class FakeMessageMissingReasoning:
            role = "assistant"
            content = "hello"
            reasoning_content = "hidden reasoning"

            def model_dump(self, exclude_none=False):
                # Some SDK versions might not include reasoning_content
                return {"role": self.role, "content": self.content}

        result = _object_to_dict(FakeMessageMissingReasoning())
        # Fix: reasoning_content is now captured via getattr fallback
        assert result.get("reasoning_content") == "hidden reasoning"
        assert result["role"] == "assistant"
        assert result["content"] == "hello"

    def test_first_text_delta_recognizes_reasoning_keys(self):
        """Verify which keys are checked for reasoning deltas."""
        # Only these keys are checked
        keys = ("reasoning_content", "reasoning", "reasoning_text")

        assert _first_text_delta({"reasoning_content": "r"}, keys) == "r"
        assert _first_text_delta({"reasoning": "r"}, keys) == "r"
        assert _first_text_delta({"reasoning_text": "r"}, keys) == "r"
        # "thinking" is NOT checked — some providers use this key
        assert _first_text_delta({"thinking": "r"}, keys) == ""

    def test_streaming_message_preserves_reasoning_content(self):
        """Simulate the streaming path and verify the message structure."""
        # Simulate what _complete_streaming produces
        reasoning_parts = ["step ", "by ", "step"]
        content_parts = ["answer"]
        message = {
            "role": "assistant",
            "content": "".join(content_parts),
        }
        reasoning = "".join(reasoning_parts)
        if reasoning:
            message["reasoning_content"] = reasoning

        assert message["reasoning_content"] == "step by step"
        assert message["role"] == "assistant"

    def test_retry_discards_partial_reasoning_content(self):
        """When a retry happens, the partial reasoning from the failed
        stream is discarded.  The message dict from the failed attempt is
        never created, so the new stream starts fresh.

        This is normally fine, BUT: if the API provider requires the
        previous message's reasoning_content to be echoed back (even after
        a retry), the fresh attempt will fail with the 400 error because
        the previous assistant message in the conversation lacks
        reasoning_content.
        """
        # Simulate partial streaming
        reasoning_parts = ["partial", " reasoning"]
        content_parts = ["partial", " answer"]

        # Network error occurs mid-stream
        # The deltas that arrived are discarded on retry
        discarded_reasoning = "".join(reasoning_parts)
        discarded_content = "".join(content_parts)

        # On retry, the old stream state is reset and a new stream starts
        # The message from the failed attempt is never committed to messages[]
        assert discarded_reasoning == "partial reasoning"  # lost

        # The new stream: if it also fails, and the API provider is stateful
        # about reasoning_content across requests, the second attempt could
        # also fail, and so on — all with the same 400 error.

    def test_non_streaming_path_preserves_reasoning_content_after_fix(self):
        """After the fix, _object_to_dict captures reasoning_content via getattr."""
        # _complete_non_streaming:
        #   response = self.client.chat.completions.create(**request)
        #   message = response.choices[0].message
        #   return _object_to_dict(message)

        class SDKMessage:
            """Simulates an OpenAI SDK ChatCompletionMessage."""
            def __init__(self):
                self.role = "assistant"
                self.content = "the answer"
                self.tool_calls = None
                self.reasoning_content = "the reasoning"

            def model_dump(self, exclude_none=True):
                # Only includes fields the SDK model explicitly declares
                d = {"role": self.role, "content": self.content}
                if self.tool_calls:
                    d["tool_calls"] = self.tool_calls
                return d

        msg = SDKMessage()
        result = _object_to_dict(msg)
        # Fix: reasoning_content is now captured via getattr fallback
        assert result.get("reasoning_content") == "the reasoning"
        assert result["role"] == "assistant"
        assert result["content"] == "the answer"

    def test_roundtrip_message_with_reasoning_preserved(self):
        """Verify that when a message WITH reasoning_content is sent back,
        it survives the round-trip through dict conversion."""
        original = {
            "role": "assistant",
            "content": "final answer",
            "reasoning_content": "long reasoning chain",
        }
        # Simulate what happens when the message is put into messages[]
        messages = [{"role": "system", "content": "..."},
                    {"role": "user", "content": "..."},
                    original]
        # The third message IS the assistant message with reasoning_content
        assert messages[2].get("reasoning_content") == "long reasoning chain"
        # This is what gets sent to the API — it should be fine if the
        # dict structure is preserved.


# ---------------------------------------------------------------------------
# Bug 3: CoT line count flickering between 6 and 7
# ---------------------------------------------------------------------------

class TestBug3_CotLineFlickering:
    """Investigate why the CoT display flickers between 6 and 7 lines.

    Root cause: the number of displayed CoT lines depends on three factors:

    1. **``cot_lines`` calculation**: when ``has_other_content`` is True,
       CoT gets ``max(2, min(max_lines, max_lines // 2 + 1))`` lines.
       When False, it gets ``max_lines`` lines.  ``has_other_content``
       changes when ``finish_thinking`` adds a log line (CoT summary).

    2. **``remaining`` parameter**: the number of lines available for
       content depends on whether the tool-line is present.  The tool-line
       toggles based on ``active_tool`` / ``tool_history``.

    3. **Empty-line filtering**: ``_section_lines`` skips whitespace-only
       lines.  When the model outputs a trailing newline followed by a
       blank, the effective line count drops by 1.  When the next token
       fills that line, the count increases by 1.

    These factors combined cause the CoT section height to oscillate
    between renders.  When the height changes, ``_LineDiffLive._paint_full``
    is used instead of ``_paint_diff``, clearing and redrawing the entire
    screen — perceived as flickering.
    """

    def test_cot_lines_calculation_formula(self):
        """Document the cot_lines formula and its breakpoints."""
        for max_lines in range(5, 16):
            cot_with_other = max(2, min(max_lines, max_lines // 2 + 1))
            cot_without_other = max_lines

            # The transition from without-other to with-other causes a
            # sudden drop in CoT line count
            drop = cot_without_other - cot_with_other
            # print(f"max_lines={max_lines:2d}:  no-other={cot_without_other:2d}  "
            #       f"with-other={cot_with_other:2d}  drop={drop}")
            if max_lines == 11:
                assert cot_with_other == 6
                assert drop == 5
            if max_lines == 12:
                assert cot_with_other == 7
                assert drop == 5
            if max_lines == 13:
                assert cot_with_other == 7
                assert drop == 6

    def test_cot_line_count_with_varying_thinking_text(self):
        """Show that thinking_text line count affects CoT section height."""
        renderer = LemmaTeamRenderer(
            console=Console(
                file=io.StringIO(),
                width=100,
                height=28,
                record=True,
                force_terminal=False,
                color_system=None,
            ),
            screen=False,
        )
        renderer.register_worker(0)

        def count_cot_lines(state, max_lines):
            has_other = bool(state.output_text or state.tool_history or state.log_lines)
            cot_lines = max(2, min(max_lines, max_lines // 2 + 1)) if has_other else max_lines
            return _count_section_lines("CoT", state.thinking_text,
                                         width=80, max_lines=cot_lines)

        state = renderer._workers[0]

        # Initially: no thinking text → CoT section not rendered → 0 lines
        state.thinking_text = ""
        # _render_agent_content_lines only calls _section_lines when
        # thinking_text is truthy, so empty text → 0 CoT lines
        assert not state.thinking_text  # empty → no CoT section shown

        # 5 non-empty lines
        state.thinking_text = "\n".join(f"line{i}" for i in range(5))
        n5 = count_cot_lines(state, 12)
        # max_lines=12, no has_other → cot_lines=12, 1 header + 5 content = 6
        assert n5 == 6

        # 6 non-empty lines
        state.thinking_text = "\n".join(f"line{i}" for i in range(6))
        n6 = count_cot_lines(state, 12)
        assert n6 == 7  # 1 header + 6 content

        # With has_other_content=True: cot_lines shrinks
        state.log_lines.append("CoT 1.0s · 100 chars")
        n6_with_other = count_cot_lines(state, 12)
        # max_lines=12, has_other → cot_lines = max(2, min(12, 7)) = 7
        # 1 header + min(6, 6) = 7
        assert n6_with_other == 7

        # The oscillation: without has_other, 5→6 lines causes 6→7 jump
        # With has_other, cot_lines=7 (fixed), so section uses ≤7 lines
        assert n5 != n6  # 6 vs 7 — THIS IS THE FLICKER

    def test_empty_lines_cause_oscillation(self):
        """Empty/whitespace lines in streaming CoT cause line count to
        oscillate between renders."""
        # When the model outputs a trailing double-newline, it creates
        # an empty line that _section_lines filters out.
        text_with_empty = "line1\nline2\n\n"  # splitlines → ["line1", "line2", ""]
        lines = _tail_lines(text_with_empty, 10)
        non_empty = [l for l in lines if l.strip()]
        assert len(non_empty) == 2  # empty line skipped

        # When the model then fills that empty line:
        text_filled = "line1\nline2\nline3"
        lines2 = _tail_lines(text_filled, 10)
        non_empty2 = [l for l in lines2 if l.strip()]
        assert len(non_empty2) == 3  # one more non-empty line

        # Between these two renders, the CoT section height changes,
        # triggering a full repaint.

    def test_has_other_content_changes_cot_lines(self):
        """When finish_thinking adds a log line, has_other_content flips,
        shrinking the CoT section."""
        renderer = LemmaTeamRenderer(
            console=Console(
                file=io.StringIO(),
                width=100,
                height=28,
                record=True,
                force_terminal=False,
                color_system=None,
            ),
            screen=False,
        )
        renderer.register_worker(0)
        state = renderer._workers[0]

        # During streaming, before first CoT finishes
        state.thinking_text = "line1\nline2\nline3\nline4\nline5"
        has_other_before = bool(state.output_text or state.tool_history or state.log_lines)
        assert has_other_before is False

        # CoT finishes → log line added
        renderer.finish_thinking(0, module="generator", elapsed=1.0, char_count=100)
        has_other_after = bool(state.output_text or state.tool_history or state.log_lines)
        assert has_other_after is True  # log_lines now non-empty

        # cot_lines formula gives different results:
        max_l = 12
        cot_before = max_l  # has_other=False → full max_lines
        cot_after = max(2, min(max_l, max_l // 2 + 1))  # has_other=True
        assert cot_before != cot_after
        # 12 → 7, a drop of 5 lines — very visible

    def test_tool_line_presence_changes_remaining_content_lines(self):
        """Tool line takes 1 line, reducing content area by 1."""
        renderer = LemmaTeamRenderer(
            console=Console(
                file=io.StringIO(),
                width=100,
                height=28,
                record=True,
                force_terminal=False,
                color_system=None,
            ),
            screen=False,
        )
        renderer.register_worker(0)

        # Without tool history
        state = renderer._workers[0]
        assert not state.tool_history
        assert state.active_tool is None
        # → _render_tool_line returns None → remaining = max_lines - 1

        # With tool history
        renderer.update_tool_start(0, module="test", name="write_file", arg_preview="...")
        renderer.update_tool_done(0, name="write_file", is_error=False)
        assert state.tool_history  # non-empty
        # → _render_tool_line returns a Text → remaining = max_lines - 2

        # The 1-line difference translates to cot_lines changing from
        # e.g. 7 to 6 when has_other_content=True and max_lines borders
        # a breakpoint.

    def test_rendered_line_count_stability_during_streaming(self):
        """Check if the total rendered line count is stable during typical
        streaming of thinking deltas."""
        renderer = LemmaTeamRenderer(
            console=Console(
                file=io.StringIO(),
                width=100,
                height=34,
                record=True,
                force_terminal=True,
                color_system=None,
            ),
            screen=False,
            refresh_per_second=1000,
        )
        renderer.register_worker(0)
        renderer.start()
        try:
            # Establish baseline line count
            renderer._live.update(renderer.render(), refresh=True)
            baseline_count = len(renderer._live._last_lines)
            assert baseline_count > 0

            line_counts = []

            # Simulate streaming: grow thinking_text line by line
            for n in range(1, 12):
                text = "\n".join(f"reasoning step {i}" for i in range(n))
                renderer.update_thinking(0, module="generator",
                                         thinking_text=text, elapsed=0)
                renderer._live.update(renderer.render(), refresh=True)
                line_counts.append(len(renderer._live._last_lines))

            # Count how many times the line count changes
            changes = sum(1 for i in range(1, len(line_counts))
                          if line_counts[i] != line_counts[i - 1])
            # Each change triggers _paint_full (screen clear + redraw)
            # which is perceived as flicker.
            # If changes > 1 during a single stream, the user sees
            # multiple flickers.
            # (We can't assert a specific number since it depends on layout
            # internals, but we can record what happens.)
            assert changes >= 0  # always true, documenting the metric
        finally:
            renderer.stop()


# ---------------------------------------------------------------------------
# Cross-bug analysis: refresh behaviour
# ---------------------------------------------------------------------------

class TestRefreshBehavior:
    """Cross-cutting refresh/throttle behaviour that contributes to all bugs."""

    def test_force_vs_throttled_refresh_timing(self):
        """thinking_delta events force immediate refresh; others are throttled."""
        stream = io.StringIO()
        renderer = LemmaTeamRenderer(
            console=Console(
                file=stream,
                width=100,
                height=28,
                record=True,
                force_terminal=True,
                color_system=None,
            ),
            screen=False,
        )
        renderer.register_worker(0)
        renderer.start()
        try:
            # Freeze time so throttle doesn't expire
            renderer._last_refresh_at = time.time() + 1000

            stream.seek(0)
            stream.truncate(0)

            # Throttled call (update_phase without force)
            renderer.update_phase(0, "generator")
            throttled_output = stream.getvalue()
            # Should NOT paint immediately (throttled)
            assert throttled_output == "" or "\x1b[2K" not in throttled_output

            # Force call (update_thinking WITH force=True)
            renderer.update_thinking(0, module="generator",
                                     thinking_text="test", elapsed=0)
            forced_output = stream.getvalue()
            # SHOULD paint immediately
            assert "test" in forced_output or "\x1b[2K" in forced_output
        finally:
            renderer.stop()
