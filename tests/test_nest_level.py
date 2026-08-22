"""Tests for the shared semantic nest-level utility
(SGC_Rey_Log_Nest_Level_Phase_1).

Callers declare semantic boundaries; the utility owns the numeric level. Phase 1
maintains this only in memory on ctx and changes no record output.
"""

from __future__ import annotations

import pytest

from tests.conftest import make_run_log

from rey_lib.config.config_utils import Namespace
from rey_lib.logs import (
    get_nest_level,
    next_nest_level,
    previous_nest_level,
    set_nest_level,
)


def _ctx():
    """A run log with no durable path: nesting state lives in memory."""
    from rey_lib.logs.run_log import RunLog

    return RunLog(app="test", run_id="r1", run_timestamp="20260101_000000")


# TEST-001 — corrected semantic level constants.
def test_semantic_base_mapping(run_log) -> None:
    """pipeline/pipeline_step/app/workflow/workflow_step resolve to fixed 1/2/3/4/5."""
    assert set_nest_level(run_log, "pipeline") == 1
    assert set_nest_level(run_log, "pipeline_step") == 2
    assert set_nest_level(run_log, "app") == 3
    assert set_nest_level(run_log, "workflow") == 4
    assert set_nest_level(run_log, "workflow_step") == 5


# TEST-005 (hierarchy SGC) — pipeline step and workflow step are distinct levels.
def test_pipeline_and_workflow_step_are_distinct(run_log) -> None:
    assert set_nest_level(run_log, "pipeline_step") != set_nest_level(run_log, "workflow_step")


# TEST-002 (hierarchy SGC) — the full chain produces strictly increasing levels.
def test_full_hierarchy_transitions_increase(run_log) -> None:
    assert set_nest_level(run_log, "pipeline") == 1
    assert set_nest_level(run_log, "pipeline_step") == 2
    assert set_nest_level(run_log, "app") == 3
    assert set_nest_level(run_log, "workflow") == 4
    assert set_nest_level(run_log, "workflow_step") == 5


# TEST-002
def test_relative_nesting_increments_and_decrements_by_one(run_log) -> None:
    set_nest_level(run_log, "app")           # 3
    assert next_nest_level(run_log) == 4
    assert next_nest_level(run_log) == 5
    assert previous_nest_level(run_log) == 4
    assert get_nest_level(run_log) == 4


# TEST-003 / AC-006/007/008
def test_semantic_base_reset_discards_deeper_nesting(run_log) -> None:
    """A new base resets, discarding a deeper level left by a prior section."""
    set_nest_level(run_log, "app")
    next_nest_level(run_log)                  # 4 — a nested section entered
    next_nest_level(run_log)                  # 5 — and never left (abnormal exit)
    # The next semantic base reset is self-correcting.
    assert set_nest_level(run_log, "app") == 3
    assert set_nest_level(run_log, "workflow") == 4
    assert set_nest_level(run_log, "pipeline") == 1


# TEST-004 / AC-005
def test_previous_never_goes_negative(run_log) -> None:
    assert get_nest_level(run_log) == 0      # none established
    assert previous_nest_level(run_log) == 0  # clamped, not -1
    assert previous_nest_level(run_log) == 0


def test_unknown_semantic_base_is_rejected(run_log) -> None:
    with pytest.raises(ValueError, match="Unknown semantic nest level"):
        set_nest_level(run_log, "step")


def test_get_defaults_to_zero_without_a_base(run_log) -> None:
    assert get_nest_level(run_log) == 0


# TEST-005 — representative direct app execution. The relative floor is proven by
# test_nesting_contract; this covers only the base and its nested section.
def test_direct_app_execution_establishes_app_base_then_nests(run_log) -> None:
    set_nest_level(run_log, "app")           # app runs directly -> base 3
    assert get_nest_level(run_log) == 3
    next_nest_level(run_log)                  # analysis-owned section
    assert get_nest_level(run_log) == 4


# TEST-006 — representative workflow execution: an app owns the workflow, which
# owns its steps (corrected hierarchy: app 3 -> workflow 4 -> workflow_step 5).
def test_app_then_workflow_then_step(run_log) -> None:
    set_nest_level(run_log, "app")               # 3
    assert set_nest_level(run_log, "workflow") == 4       # workflow runs inside the app
    assert set_nest_level(run_log, "workflow_step") == 5  # a step inside the workflow


# TEST-007 — representative pipeline execution.
def test_pipeline_base_and_deterministic_later_resets(run_log) -> None:
    set_nest_level(run_log, "pipeline")      # 1
    assert get_nest_level(run_log) == 1
    # A later boundary establishes its own fixed base deterministically.
    assert set_nest_level(run_log, "app") == 3


def test_state_lives_on_ctx_not_globally(run_log) -> None:
    """Two contexts keep independent levels."""
    a, b = _ctx(), _ctx()
    set_nest_level(run_log, "pipeline")
    set_nest_level(run_log, "app")
    assert get_nest_level(run_log) == 1
    assert get_nest_level(run_log) == 3
