"""Progress advances on dispatch, and completes on completion.

Two counts that are easy to mistake for one, and easy to collapse into each
other by someone reading the arithmetic rather than the intent:

    current_step_number  the step the run is ON      -- STEP_START, dispatch
    completed_steps      the steps it has FINISHED   -- STEP_END

A run advances the moment it dispatches a step, so the position moves as soon as
the next step begins rather than waiting for it to finish. Deriving the position
from STEP_END instead would leave a reader watching a bar that sits still for the
whole of every step and then jumps.

The total is the plan's -- backend truth, known before the first step logs
anything -- so a bar is correct from the first paint rather than growing as the
log fills.

This is the correction the implementation looks like incidental arithmetic. It
had no direct cover in either repository before the derivation moved to
rey_lib.run.state; these pin it to the intent rather than to the expression.
"""

from __future__ import annotations

from typing import Any

from rey_lib.run import state

PLAN: list[dict[str, Any]] = [
    {"index": 1, "name": "read_source", "app": "file_operator"},
    {"index": 2, "name": "classify", "app": "file_operator"},
    {"index": 3, "name": "sanitize", "app": "file_operator"},
    {"index": 4, "name": "load", "app": "rey_loader"},
]


def _sections(*records: dict[str, Any]) -> dict[str, Any]:
    """Wrap execution records in the projected-sections shape."""
    return {"execution": {"records": list(records)}}


def _start(name: str) -> dict[str, Any]:
    return {"record_type": "STEP_START", "step_name": name, "timestamp": f"start-{name}"}


def _end(name: str, status: str = "completed") -> dict[str, Any]:
    return {"record_type": "STEP_END", "step_name": name, "status": status,
            "timestamp": f"end-{name}"}


class TestPositionTracksDispatch:
    """The run is on the step it started, not the step it last finished."""

    def test_a_dispatched_step_moves_the_position_before_it_finishes(self) -> None:
        # One step started and nothing finished: the run is on step one, and has
        # completed none. Reading the position from STEP_END would report zero
        # and the bar would not move until the step ended.
        progress = state.progress(_sections(_start("read_source")), PLAN)

        assert progress["current_step_number"] == 1
        assert progress["completed_steps"] == 0

    def test_the_position_advances_as_the_next_step_begins(self) -> None:
        progress = state.progress(
            _sections(_start("read_source"), _end("read_source"), _start("classify")),
            PLAN,
        )

        assert progress["current_step_number"] == 2, "on the second step"
        assert progress["completed_steps"] == 1, "having finished the first"

    def test_position_and_completion_are_not_the_same_number(self) -> None:
        # The distinction, stated once: mid-run they differ by the step in flight.
        progress = state.progress(
            _sections(_start("a"), _end("a"), _start("b"), _end("b"), _start("c")),
            PLAN,
        )

        assert progress["current_step_number"] == 3
        assert progress["completed_steps"] == 2


class TestTheTotalIsThePlans:
    """Backend truth, known before the log says anything."""

    def test_the_total_is_known_before_the_first_step_starts(self) -> None:
        progress = state.progress(_sections(), PLAN)

        assert progress["total_steps"] == 4
        assert progress["completed_steps"] == 0
        assert progress["remaining_steps"] == 4
        assert progress["percent"] == 0

    def test_the_log_is_the_fallback_only_when_there_is_no_plan(self) -> None:
        progress = state.progress(_sections(_start("a"), _start("b")), [])

        assert progress["total_steps"] == 2, "the projected STEP_START count"

    def test_a_run_with_no_plan_and_no_records_reports_no_percentage(self) -> None:
        # Nothing to measure against. Zero would claim a run had made no
        # progress; None says the question has no answer yet.
        progress = state.progress(_sections(), [])

        assert progress["total_steps"] == 0
        assert progress["percent"] is None


class TestCountsNeverExceedThePlan:
    """A log that says more happened than the plan holds does not overrun it."""

    def test_completion_is_clamped_to_the_plan(self) -> None:
        records = [record for name in ("a", "b", "c", "d", "e", "f")
                   for record in (_start(name), _end(name))]

        progress = state.progress(_sections(*records), PLAN)

        assert progress["completed_steps"] == 4
        assert progress["remaining_steps"] == 0
        assert progress["percent"] == 100

    def test_the_position_is_clamped_to_the_plan(self) -> None:
        progress = state.progress(
            _sections(*(_start(name) for name in ("a", "b", "c", "d", "e", "f"))),
            PLAN,
        )

        assert progress["current_step_number"] == 4

    def test_remaining_never_goes_negative(self) -> None:
        records = [record for name in ("a", "b", "c", "d", "e")
                   for record in (_start(name), _end(name))]

        assert state.progress(_sections(*records), PLAN)["remaining_steps"] == 0


class TestPercentFollowsCompletion:
    """The percentage reports work finished, not work begun."""

    def test_a_run_in_flight_reports_completed_work_only(self) -> None:
        # Two of four finished and a third in flight is 50%, not 75%: a step
        # that has started has not produced anything yet.
        progress = state.progress(
            _sections(_start("a"), _end("a"), _start("b"), _end("b"), _start("c")),
            PLAN,
        )

        assert progress["percent"] == 50

    def test_a_finished_run_reports_the_whole(self) -> None:
        records = [record for name in ("a", "b", "c", "d")
                   for record in (_start(name), _end(name))]

        assert state.progress(_sections(*records), PLAN)["percent"] == 100
