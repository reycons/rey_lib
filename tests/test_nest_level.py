"""Tests for the shared semantic nest-level utility
(SGC_Rey_Log_Nest_Level_Phase_1).

Callers declare semantic boundaries; the utility owns the numeric level. Phase 1
maintains this only in memory on ctx and changes no record output.
"""

from __future__ import annotations

import pytest

from tests.conftest import make_run_log

from rey_lib.config.config_utils import Namespace


def _ctx():
    """A run log with no durable path: nesting state lives in memory."""
    from rey_lib.logs.run_log import RunLog

    return RunLog(app="test", run_id="r1", run_timestamp="20260101_000000")


# TEST-001 — corrected semantic level constants.
def test_semantic_base_mapping(run_log) -> None:
    """pipeline/pipeline_step/app/workflow/workflow_step resolve to fixed 1/2/3/4/5."""
    assert run_log.set_nest_level("pipeline") == 1
    assert run_log.set_nest_level("pipeline_step") == 2
    assert run_log.set_nest_level("app") == 3
    assert run_log.set_nest_level("workflow") == 4
    assert run_log.set_nest_level("workflow_step") == 5


# TEST-005 (hierarchy SGC) — pipeline step and workflow step are distinct levels.
def test_pipeline_and_workflow_step_are_distinct(run_log) -> None:
    assert run_log.set_nest_level("pipeline_step") != run_log.set_nest_level("workflow_step")


# TEST-002 (hierarchy SGC) — the full chain produces strictly increasing levels.
def test_full_hierarchy_transitions_increase(run_log) -> None:
    assert run_log.set_nest_level("pipeline") == 1
    assert run_log.set_nest_level("pipeline_step") == 2
    assert run_log.set_nest_level("app") == 3
    assert run_log.set_nest_level("workflow") == 4
    assert run_log.set_nest_level("workflow_step") == 5


# TEST-002
def test_relative_nesting_increments_and_decrements_by_one(run_log) -> None:
    run_log.set_nest_level("app")           # 3
    assert run_log.enter() == 4
    assert run_log.enter() == 5
    assert run_log.exit() == 4
    assert run_log.nest_level() == 4


# TEST-003 / AC-006/007/008
def test_semantic_base_reset_discards_deeper_nesting(run_log) -> None:
    """A new base resets, discarding a deeper level left by a prior section."""
    run_log.set_nest_level("app")
    run_log.enter()                  # 4 — a nested section entered
    run_log.enter()                  # 5 — and never left (abnormal exit)
    # The next semantic base reset is self-correcting.
    assert run_log.set_nest_level("app") == 3
    assert run_log.set_nest_level("workflow") == 4
    assert run_log.set_nest_level("pipeline") == 1


# TEST-004 / AC-005
def test_previous_never_goes_negative(run_log) -> None:
    assert run_log.nest_level() == 0      # none established
    assert run_log.exit() == 0  # clamped, not -1
    assert run_log.exit() == 0


def test_unknown_semantic_base_is_rejected(run_log) -> None:
    with pytest.raises(ValueError, match="Unknown semantic nest level"):
        run_log.set_nest_level("step")


def test_get_defaults_to_zero_without_a_base(run_log) -> None:
    assert run_log.nest_level() == 0


# TEST-005 — representative direct app execution. The relative floor is proven by
# test_nesting_contract; this covers only the base and its nested section.
def test_direct_app_execution_establishes_app_base_then_nests(run_log) -> None:
    run_log.set_nest_level("app")           # app runs directly -> base 3
    assert run_log.nest_level() == 3
    run_log.enter()                  # analysis-owned section
    assert run_log.nest_level() == 4


# TEST-006 — representative workflow execution: an app owns the workflow, which
# owns its steps (corrected hierarchy: app 3 -> workflow 4 -> workflow_step 5).
def test_app_then_workflow_then_step(run_log) -> None:
    run_log.set_nest_level("app")               # 3
    assert run_log.set_nest_level("workflow") == 4       # workflow runs inside the app
    assert run_log.set_nest_level("workflow_step") == 5  # a step inside the workflow


# TEST-007 — representative pipeline execution.
def test_pipeline_base_and_deterministic_later_resets(run_log) -> None:
    run_log.set_nest_level("pipeline")      # 1
    assert run_log.nest_level() == 1
    # A later boundary establishes its own fixed base deterministically.
    assert run_log.set_nest_level("app") == 3


def test_state_lives_on_the_run_log_not_globally() -> None:
    """Two run logs keep independent levels."""
    a, b = _ctx(), _ctx()
    a.set_nest_level("pipeline")
    b.set_nest_level("app")
    assert a.nest_level() == 1
    assert b.nest_level() == 3
