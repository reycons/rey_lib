"""Tests for logical record identity and parent resolution
(SGC_Rey_Log_Record_Parenting_Phase_2).

The shared writer stamps record_id / parent_record_id / nest_level derived from
Phase 1 nest-level transitions. These tests exercise the model directly through
the stamp/commit hooks and the nest-level functions that consume transitions.
"""

from __future__ import annotations

from rey_lib.config.config_utils import Namespace
from rey_lib.logs import (
    next_nest_level,
    previous_nest_level,
    set_nest_level,
)
from rey_lib.logs import record_parenting as rp


def _ctx() -> Namespace:
    return Namespace({})


def _write(ctx, nest_level, *, fail=False) -> dict:
    """Stamp a record; commit only on a successful append (fail=False)."""
    record: dict = {}
    record_id = rp.stamp_record(ctx, record, nest_level)
    if not fail:
        rp.commit_record(ctx, record_id)
    return record


# TEST-001 / AC-001/002
def test_sequential_logical_record_ids() -> None:
    ctx = _ctx()
    set_nest_level(ctx, "app")
    assert _write(ctx, 3)["record_id"] == 1
    assert _write(ctx, 3)["record_id"] == 2
    assert _write(ctx, 3)["record_id"] == 3


# TEST-002 / AC-005
def test_failed_append_does_not_advance_last_record_id() -> None:
    ctx = _ctx()
    set_nest_level(ctx, "app")
    assert _write(ctx, 3)["record_id"] == 1
    assert _write(ctx, 3, fail=True)["record_id"] == 2   # stamped but not committed
    # The next successful write reuses id 2 — no skip.
    assert _write(ctx, 3)["record_id"] == 2


# TEST-003 / AC-006
def test_same_level_sibling_records_share_one_parent() -> None:
    ctx = _ctx()
    set_nest_level(ctx, "app")     # level 3, parent 0
    r1 = _write(ctx, 3)
    next_nest_level(ctx)           # level 4, parent = r1
    a = _write(ctx, 4)
    b = _write(ctx, 4)
    c = _write(ctx, 4)
    assert a["parent_record_id"] == b["parent_record_id"] == c["parent_record_id"] == r1["record_id"]
    # Writing a sibling does not make it the parent of the next sibling.
    assert b["parent_record_id"] != a["record_id"]


# TEST-004 / AC-007
def test_increase_level_parents_to_last_written_record() -> None:
    ctx = _ctx()
    set_nest_level(ctx, "app")
    r1 = _write(ctx, 3)
    r2 = _write(ctx, 3)
    next_nest_level(ctx)           # parent for level 4 = most recent record (r2)
    child = _write(ctx, 4)
    assert child["parent_record_id"] == r2["record_id"]


# TEST-005 / AC-008/009
def test_decrease_restores_lower_parent_and_clears_deeper() -> None:
    ctx = _ctx()
    set_nest_level(ctx, "app")     # 3, parent 0
    app_rec = _write(ctx, 3)
    next_nest_level(ctx)           # 4, parent app_rec
    deep = _write(ctx, 4)
    previous_nest_level(ctx)       # back to 3
    back = _write(ctx, 3)
    # Restored to the app-level parent (0), not the deeper record.
    assert back["parent_record_id"] == 0
    assert back["parent_record_id"] != deep["record_id"]


# TEST-006 / AC-010
def test_direct_app_parents_to_synthetic_root() -> None:
    ctx = _ctx()
    set_nest_level(ctx, "app")     # no pipeline/workflow active
    assert _write(ctx, 3)["parent_record_id"] == 0


# TEST-007 / AC-011 — an app owns a workflow (4) which owns its steps (5); each
# level parents to the record above it (corrected hierarchy).
def test_app_workflow_step_parent_chain() -> None:
    ctx = _ctx()
    set_nest_level(ctx, "app")        # 3, parent 0
    app = _write(ctx, 3)
    set_nest_level(ctx, "workflow")   # 4, descent -> parent = app
    wf = _write(ctx, 4)
    next_nest_level(ctx)              # 5, parent wf
    assert wf["parent_record_id"] == app["record_id"]
    assert _write(ctx, 5)["parent_record_id"] == wf["record_id"]


# TEST-008 / AC-012 — app parents to the nearest written ancestor when the
# intervening pipeline-step level (2) has no record of its own.
def test_pipeline_directly_parents_app_when_step_record_absent() -> None:
    ctx = _ctx()
    set_nest_level(ctx, "pipeline")  # 1, parent 0
    pipe = _write(ctx, 1)
    next_nest_level(ctx)             # enter level 2 (pipeline step); no record written
    set_nest_level(ctx, "app")       # 3 — descent parents to the last record (pipeline)
    assert _write(ctx, 3)["parent_record_id"] == pipe["record_id"]


# TEST-009 — stale branch cleanup on reset.
def test_reset_to_lower_level_removes_deeper_levels() -> None:
    ctx = _ctx()
    set_nest_level(ctx, "app")       # 3
    r = _write(ctx, 3)
    next_nest_level(ctx)             # 4
    _write(ctx, 4)
    next_nest_level(ctx)             # 5
    _write(ctx, 5)
    # Reset to app base: deeper levels (4, 5) are discarded; a new descent parents
    # to the app-level record, not a stale level-4/5 record.
    set_nest_level(ctx, "app")       # 3
    app2 = _write(ctx, 3)
    next_nest_level(ctx)             # 4 again, parent = app2 (most recent), not stale
    assert _write(ctx, 4)["parent_record_id"] == app2["record_id"]


# TEST-010 / AC-003
def test_record_id_is_logical_not_physical_line() -> None:
    """record_id comes from writer state; failed writes leave physical gaps but ids do not skip."""
    ctx = _ctx()
    set_nest_level(ctx, "app")
    assert _write(ctx, 3)["record_id"] == 1
    _write(ctx, 3, fail=True)                    # would-be physical line, not written
    _write(ctx, 3, fail=True)                    # another
    assert _write(ctx, 3)["record_id"] == 2      # still logical 2, independent of lines
