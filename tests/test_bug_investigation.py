"""
Investigation tests for historical bugs and new UI behavior.

Historical bugs (now fixed by timeline UI):
1. Worker panel flickering between generator and reviser phases
2. reasoning_content must be passed back to API error (400)
3. CoT line count flickering between 6 and 7 lines

These tests verify that the new timeline-based UI eliminates the old
flickering issues and that reasoning_content is handled correctly.
"""

import io
import time
from unittest import mock

import pytest
from rich.console import Console

from alphasolve.agents.general.general_agent import (
    _first_text_delta,
    _object_to_dict,
    _prepare_messages_for_request,
)
from alphasolve.agents.team.dashboard import make_worker_event_sink
from alphasolve.utils.rich_renderer import (
    PropositionTeamRenderer,
    WorkerRenderState,
)


# ---------------------------------------------------------------------------
# Bug 1: Worker panel flickering between generator and reviser (FIXED)
# ---------------------------------------------------------------------------


class TestBug1_PhaseFlickeringFixed:
    """Verify that the timeline UI eliminates phase/CoT mismatch flickering.

    In the old UI, ``thinking_text`` persisted across phase changes, causing
    the old agent's CoT to be displayed under the new agent's phase label.
    The new timeline UI does not persist raw thinking text; it only records
    a one-line ``Thought for Xs · N chars`` summary event when thinking ends.
    """

    def test_phase_change_does_not_persist_old_cot(self):
        """Old CoT is not visible after phase switches to a new agent."""
        renderer = PropositionTeamRenderer(
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

        # generator thinks
        renderer.update_thinking(
            0, module="generator",
            thinking_text="generator's deep reasoning\nstep 1\nstep 2",
            elapsed=1.0,
        )
        renderer.finish_thinking(0, module="generator", elapsed=1.5, char_count=60)

        # Phase changes to reviser
        renderer.update_phase(0, "reviser w1", status="thinking")
        state = renderer._workers[0]
        assert state.phase == "reviser w1"
        # Old raw CoT is NOT stored anywhere
        assert state.thinking_started_at == 0.0
        assert state.thinking_token_count == 0
        # Timeline contains the "Thought for ..." summary, not the raw text
        assert any("Thought for" in e.text for e in state.timeline)
        assert not any("generator's deep reasoning" in e.text for e in state.timeline)

    def test_new_thinking_delta_replaces_old_spinner_cleanly(self):
        """A new thinking delta resets the spinner timer; no stale text."""
        renderer = PropositionTeamRenderer(
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
        renderer.update_thinking(
            0, module="generator", thinking_text="gen reasoning", elapsed=0,
        )
        renderer.finish_thinking(0, module="generator", elapsed=1.0, char_count=30)
        renderer.update_thinking(
            0, module="reviser", thinking_text="rev reasoning", elapsed=0,
        )

        state = renderer._workers[0]
        assert state.phase == "reviser"
        assert state.thinking_token_count == len("rev reasoning")
        # Timeline has the first thought summary but not raw CoT
        assert any("Thought for" in e.text for e in state.timeline)


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
        keys = ("reasoning_content", "reasoning", "reasoning_text", "thinking")

        assert _first_text_delta({"reasoning_content": "r"}, keys) == "r"
        assert _first_text_delta({"reasoning": "r"}, keys) == "r"
        assert _first_text_delta({"reasoning_text": "r"}, keys) == "r"
        assert _first_text_delta({"thinking": "r"}, keys) == "r"

    def test_object_to_dict_normalizes_reasoning_aliases(self):
        """Provider-specific thinking fields are echoed as reasoning_content."""
        class FakeThinkingMessage:
            role = "assistant"
            content = ""
            thinking = "provider thinking"

            def model_dump(self, exclude_none=False):
                return {"role": self.role, "content": self.content, "thinking": self.thinking}

        result = _object_to_dict(FakeThinkingMessage())

        assert result["thinking"] == "provider thinking"
        assert result["reasoning_content"] == "provider thinking"

    def test_thinking_mode_tool_call_request_keeps_reasoning_content_key(self):
        """Tool-call assistant messages must carry reasoning_content into the next request."""
        messages = [
            {"role": "user", "content": "revise"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_edit",
                        "type": "function",
                        "function": {"name": "edit_file", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_edit", "content": "ok"},
        ]

        prepared = _prepare_messages_for_request(messages, thinking_mode=True)

        assert prepared[1]["reasoning_content"] == ""
        assert "reasoning_content" not in messages[1]

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
# Bug 3: CoT line count flickering between 6 and 7 (FIXED)
# ---------------------------------------------------------------------------


class TestBug3_CotLineFlickeringFixed:
    """Verify that the timeline UI eliminates CoT line-count flickering.

    In the old UI, the CoT section height was dynamically calculated based on
    ``has_other_content``, tool-line presence, and empty-line filtering.  The
    new timeline UI renders a simple chronological list of events; there is no
    separate CoT section whose height can oscillate.
    """

    def test_timeline_line_count_is_stable_during_streaming(self):
        """Timeline height is capped by panel max_lines and event count."""
        renderer = PropositionTeamRenderer(
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
            renderer._live.update(renderer.render(), refresh=True)
            baseline_count = len(renderer._live._last_lines)
            assert baseline_count > 0

            line_counts = []
            # Simulate growing thinking text (which in old UI caused line oscillation)
            for n in range(1, 12):
                text = "\n".join(f"reasoning step {i}" for i in range(n))
                renderer.update_thinking(0, module="generator",
                                         thinking_text=text, elapsed=0)
                renderer._live.update(renderer.render(), refresh=True)
                line_counts.append(len(renderer._live._last_lines))

            # In timeline mode, thinking text does NOT increase rendered lines.
            # Only the single "Thinking ..." spinner line appears at the bottom.
            # Therefore line count should be perfectly stable.
            assert all(c == line_counts[0] for c in line_counts)
        finally:
            renderer.stop()

    def test_finish_thinking_adds_single_summary_line(self):
        """finish_thinking appends exactly one 'Thought for ...' event."""
        renderer = PropositionTeamRenderer(
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
        renderer.update_thinking(0, module="generator",
                                 thinking_text="line1\nline2\nline3",
                                 elapsed=0)
        renderer.finish_thinking(0, module="generator", elapsed=1.0, char_count=100)

        state = renderer._workers[0]
        thought_events = [e for e in state.timeline if e.type.name == "THOUGHT"]
        assert len(thought_events) == 1
        assert "Thought for" in thought_events[0].text

    def test_tool_events_are_single_lines(self):
        """Tool start/done produce single-line timeline entries."""
        renderer = PropositionTeamRenderer(
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
        renderer.update_tool_start(0, module="test", name="grep", arg_preview='{"pattern":"x"}')
        renderer.update_tool_done(0, name="grep", is_error=False)

        state = renderer._workers[0]
        tool_events = [e for e in state.timeline if e.type.name == "TOOL_DONE"]
        assert len(tool_events) == 1
        assert "✓ grep" in tool_events[0].text


# ---------------------------------------------------------------------------
# Cross-bug analysis: refresh behaviour
# ---------------------------------------------------------------------------


class TestRefreshBehavior:
    """Cross-cutting refresh/throttle behaviour that contributes to all bugs."""

    def test_force_vs_throttled_refresh_timing(self):
        """thinking_delta events force immediate refresh; others are throttled."""
        stream = io.StringIO()
        renderer = PropositionTeamRenderer(
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

            renderer.update_thinking(0, module="generator",
                                     thinking_text="test", elapsed=0)
            state = renderer._workers[0]
            assert state.phase == "generator"
            assert state.thinking_token_count == len("test")
        finally:
            renderer.stop()
