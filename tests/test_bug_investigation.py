"""保留时间线 UI 的阶段切换、推理摘要和刷新节流回归测试。"""

import io
import time
from rich.console import Console

from alphasolve.solver.ui.team_renderer import PropositionTeamRenderer


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
