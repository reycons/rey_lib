"""A run's phases partition its elapsed time, with nothing unaccounted.

Phases are named on entry, so the timeline always knows what is running. That
is what makes termination honest: a phase that raises is closed under the name
it already had, and no successor is invented to close it.
"""

from __future__ import annotations

import pytest

from rey_lib.logs.phase_timeline import PhaseTimeline


class _Clock:
    """A monotonic clock that advances only when told to."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    ticker = _Clock()
    monkeypatch.setattr("rey_lib.logs.phase_timeline.time.monotonic", ticker)
    return ticker


def _timeline(clock: _Clock, phase: str = "bootstrap") -> PhaseTimeline:
    return PhaseTimeline(started=clock.now, phase=phase)


class TestThePartition:
    """Contiguous, exact, and closed only when the run ends."""

    def test_phases_are_contiguous_and_named(self, clock: _Clock) -> None:
        timeline = _timeline(clock)
        clock.advance(0.5)
        timeline.enter("resolve")
        clock.advance(2.0)
        timeline.enter("steps")
        clock.advance(0.25)
        timeline.finish()

        assert timeline.phases() == [
            {"phase": "bootstrap", "duration_ms": 500},
            {"phase": "resolve", "duration_ms": 2000},
            {"phase": "steps", "duration_ms": 250},
        ]

    def test_the_partition_is_exact_not_tolerated(self, clock: _Clock) -> None:
        """Durations telescope to the total, because each boundary is shared.

        Not 'within a slack window'. If this ever needs one, the boundaries
        stopped being shared and the instrument is wrong.
        """
        timeline = _timeline(clock)
        for seconds in (0.3, 1.7, 0.0, 12.5):
            clock.advance(seconds)
            timeline.enter(f"p{seconds}")
        clock.advance(0.125)
        timeline.finish()

        assert sum(e["duration_ms"] for e in timeline.phases()) == timeline.total_ms()
        assert timeline.unattributed_ms() == 0

    def test_a_zero_length_phase_is_still_a_phase(self, clock: _Clock) -> None:
        """Something that took no measurable time still happened and is named."""
        timeline = _timeline(clock, "instant")
        timeline.enter("work")
        clock.advance(1.0)
        timeline.finish()

        assert timeline.phases()[0] == {"phase": "instant", "duration_ms": 0}

    def test_repeated_names_stay_separate_intervals(self, clock: _Clock) -> None:
        """Merging would report a duration no single stretch of the run took."""
        timeline = _timeline(clock, "steps")
        clock.advance(1.0)
        timeline.enter("steps")
        clock.advance(4.0)
        timeline.finish()

        assert [e["duration_ms"] for e in timeline.phases()] == [1000, 4000]

    def test_the_total_excludes_time_after_termination(self, clock: _Clock) -> None:
        """A timeline is read after the run; the read is not part of it."""
        timeline = _timeline(clock)
        clock.advance(2.0)
        timeline.finish()
        clock.advance(30.0)

        assert timeline.total_ms() == 2000
        assert timeline.unattributed_ms() == 0


class TestTerminationIsNotATransition:
    """finish() closes what is running. It never invents a successor."""

    def test_finish_creates_no_phase(self, clock: _Clock) -> None:
        """The case that forced entry-naming: package raises, and is closed as package."""
        timeline = _timeline(clock, "summarize")
        clock.advance(1.0)
        timeline.enter("package")
        clock.advance(7.0)
        timeline.finish()          # package raised; nothing follows it

        assert [e["phase"] for e in timeline.phases()] == ["summarize", "package"]
        assert timeline.phases()[-1] == {"phase": "package", "duration_ms": 7000}

    def test_a_phase_that_raised_keeps_its_own_name(self, clock: _Clock) -> None:
        """Under exit-naming this interval would have had to borrow a label."""
        timeline = _timeline(clock, "package")
        clock.advance(3.0)
        timeline.finish()

        assert timeline.phases() == [{"phase": "package", "duration_ms": 3000}]

    def test_finishing_twice_does_not_move_the_boundary(self, clock: _Clock) -> None:
        """A finalizer may run from a finally as well as a return path."""
        timeline = _timeline(clock)
        clock.advance(2.0)
        timeline.finish()
        clock.advance(40.0)
        timeline.finish()

        assert timeline.total_ms() == 2000

    def test_only_the_first_finish_reports_terminating_the_run(
        self, clock: _Clock
    ) -> None:
        """How a caller knows whether to persist.

        Without this, a finalizer reached twice writes one run's partition
        twice, and both records claim to be the whole of it.
        """
        timeline = _timeline(clock)

        assert timeline.finish() is True
        assert timeline.finish() is False

    def test_entering_after_termination_is_refused(self, clock: _Clock) -> None:
        """It would extend a partition already reported as complete."""
        timeline = _timeline(clock)
        timeline.finish()

        with pytest.raises(ValueError, match="after the timeline has finished"):
            timeline.enter("late")


class TestWhatIsRunning:
    """The active phase is known, and is not reported as a duration early."""

    def test_the_active_phase_is_named(self, clock: _Clock) -> None:
        timeline = _timeline(clock)
        assert timeline.active_phase() == "bootstrap"
        timeline.enter("steps")
        assert timeline.active_phase() == "steps"

    def test_nothing_is_active_once_finished(self, clock: _Clock) -> None:
        timeline = _timeline(clock)
        timeline.finish()

        assert timeline.active_phase() is None

    def test_an_unfinished_run_reports_no_duration_for_what_is_running(
        self, clock: _Clock
    ) -> None:
        """Reporting one would measure when it was asked, not how long it took."""
        timeline = _timeline(clock)
        clock.advance(1.0)
        timeline.enter("steps")
        clock.advance(9.0)

        assert [e["phase"] for e in timeline.phases()] == ["bootstrap"]
        assert timeline.total_ms() == 0


class TestItCannotBeBuiltWithoutABoundary:
    """The two failures the constructor exists to prevent."""

    def test_an_unnamed_opening_phase_is_refused(self, clock: _Clock) -> None:
        with pytest.raises(ValueError, match="must be named"):
            PhaseTimeline(started=clock.now, phase="   ")

    def test_an_unnamed_transition_is_refused(self, clock: _Clock) -> None:
        timeline = _timeline(clock)

        with pytest.raises(ValueError, match="must be named"):
            timeline.enter("")

    def test_the_start_is_the_supplied_mark(self, clock: _Clock) -> None:
        """Measured from a boundary taken before the timeline was built."""
        timeline = PhaseTimeline(started=clock.now - 5.0, phase="bootstrap")
        timeline.enter("resolve")
        clock.advance(1.0)
        timeline.finish()

        assert timeline.phases() == [
            {"phase": "bootstrap", "duration_ms": 5000},
            {"phase": "resolve", "duration_ms": 1000},
        ]
