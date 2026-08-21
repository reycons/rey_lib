"""Progress is measured against what a run does, not against what exists.

Both runners derived their total from the full definition regardless of the
scope the run was launched with. Running to the fourth step of seven reported
seven: the bar understated progress for the whole run and never reached
completion, and the step counter read a total the run would never touch.

The narrowing is identical for a pipeline and a workflow, so it is one function
and these pin it once.

Ported from rey_console's test_runner_scoped_plan when the derivation moved to
rey_lib.run.state. The assertions are unchanged: this is the cover the
correction has always had, now pointed at the module that owns it.
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.run import state

PLAN: list[dict[str, Any]] = [
    {"index": 1, "name": "read_source", "app": "file_operator"},
    {"index": 2, "name": "classify", "app": "file_operator"},
    {"index": 3, "name": "sanitize", "app": "file_operator"},
    {"index": 4, "name": "load", "app": "rey_loader"},
    {"index": 5, "name": "verify", "app": "rey_analyzer"},
    {"index": 6, "name": "report", "app": "rey_analyzer"},
    {"index": 7, "name": "archive", "app": "file_operator"},
]


def _names(plan: list[dict[str, Any]]) -> list[str]:
    """Return the step names of a plan, in order."""
    return [str(entry["name"]) for entry in plan]


def test_running_to_a_step_counts_only_the_steps_it_runs() -> None:
    """The reported defect: to_step on the fourth of seven read seven."""
    scoped = state.scoped_plan(PLAN, "to_step", "load")

    assert len(scoped) == 4, "a run to the fourth step is four steps of work"
    assert _names(scoped) == ["read_source", "classify", "sanitize", "load"]


def test_running_from_a_step_counts_the_remainder() -> None:
    """A run from the fourth step is the fourth step and everything after it."""
    scoped = state.scoped_plan(PLAN, "from_step", "load")

    assert len(scoped) == 4
    assert _names(scoped) == ["load", "verify", "report", "archive"]


def test_running_one_step_counts_one() -> None:
    """A single-step run is one step of work."""
    scoped = state.scoped_plan(PLAN, "step", "sanitize")

    assert _names(scoped) == ["sanitize"]


def test_a_full_run_counts_everything() -> None:
    """An unscoped run is the whole plan, however the absence is expressed."""
    assert state.scoped_plan(PLAN, "full", "") == PLAN
    assert state.scoped_plan(PLAN, None, None) == PLAN


@pytest.mark.parametrize(
    ("mode", "step"),
    [
        ("to_step", "not_in_this_plan"),
        ("nonsense_mode", "load"),
        ("to_step", ""),
    ],
)
def test_it_refuses_to_narrow_on_a_guess(mode: str, step: str) -> None:
    """An overstated total is wrong; a fabricated one is worse.

    A step that is not in the plan, or a mode nothing recognises, leaves the
    plan alone rather than inventing a boundary.
    """
    assert state.scoped_plan(PLAN, mode, step) == PLAN


def test_it_does_not_hand_back_the_caller_s_own_list() -> None:
    """The plan is also shipped to the browser, so narrowing must not mutate it."""
    scoped = state.scoped_plan(PLAN, "to_step", "load")
    scoped.append({"index": 99, "name": "injected"})

    assert len(PLAN) == 7


def test_an_empty_plan_stays_empty() -> None:
    """Nothing to narrow is not a reason to invent a boundary."""
    assert state.scoped_plan([], "to_step", "load") == []
