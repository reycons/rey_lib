"""Tests for semantic-descent parent resolution
(SGC_Rey_Log_Parent_Resolver_Semantic_Descent).

A set_nest_level() transition to a deeper numeric level is a semantic descent: the
last written record parents the records at the new deeper level, so base boundaries
nest under their orchestrator rather than the synthetic root. A set to the same or a
shallower level remains a reset/return that clears deeper levels and restores the
largest active lower parent.

The resolver is map-agnostic: it keys off the numeric transition, not the semantic
name. These tests exercise it through the bases that exist today (pipeline=1,
workflow=2, app=3); the specific pipeline_step / workflow_step parenting is proven by
the resumed SGC_Rey_Log_Nest_Level_Hierarchy_Correction once those names exist. Tests
drive the resolver through the public nest-level API and the writer stamp/commit hooks,
exactly as the shared run-log writer does.
"""

from __future__ import annotations

from rey_lib.config.config_utils import Namespace
from rey_lib.logs import next_nest_level, set_nest_level
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


# TEST-001 / AC-001 — a deeper set parents the new level to the last written record.
def test_deeper_set_parents_to_last_record_id() -> None:
    ctx = _ctx()
    set_nest_level(ctx, "pipeline")     # 0 -> 1 descent; last=0 -> parent 0
    parent = _write(ctx, 1)             # id=1
    set_nest_level(ctx, "workflow")     # 1 -> 2 descent; parent = last record (1)
    child = _write(ctx, 2)
    assert child["parent_record_id"] == parent["record_id"]


# TEST-002 / AC-002 — successive deeper sets chain: each level parents to the one above.
def test_successive_descents_chain() -> None:
    ctx = _ctx()
    set_nest_level(ctx, "pipeline")       # 1
    a = _write(ctx, 1)
    set_nest_level(ctx, "pipeline_step")  # 1 -> 2 descent; parent = a
    b = _write(ctx, 2)
    set_nest_level(ctx, "app")            # 2 -> 3 descent; parent = b
    c = _write(ctx, 3)
    assert a["parent_record_id"] == 0
    assert b["parent_record_id"] == a["record_id"]
    assert c["parent_record_id"] == b["record_id"]


# TEST-003 / AC-003 — a deeper set followed by a relative next both nest, not flat to 0.
def test_deeper_set_then_next_nests() -> None:
    ctx = _ctx()
    set_nest_level(ctx, "pipeline")     # 1
    pipe = _write(ctx, 1)
    set_nest_level(ctx, "app")          # 1 -> 3 descent (skips 2); parent = pipe
    app = _write(ctx, 3)
    next_nest_level(ctx)                # 3 -> 4; parent = app
    deeper1 = _write(ctx, 4)
    deeper2 = _write(ctx, 4)
    assert app["parent_record_id"] == pipe["record_id"]
    assert deeper1["parent_record_id"] == app["record_id"]
    # Siblings share the parent; a sibling never parents the next sibling.
    assert deeper2["parent_record_id"] == app["record_id"]


# TEST-004 / AC-004 — a same-level set is a reset, never a descent.
def test_same_level_set_is_reset() -> None:
    ctx = _ctx()
    set_nest_level(ctx, "app")          # 3
    _write(ctx, 3)
    next_nest_level(ctx)                # 4
    deep = _write(ctx, 4)
    set_nest_level(ctx, "app")          # prior 4 -> 3 (<= is not a descent): reset
    back = _write(ctx, 3)
    # Restored to the app-level parent (root 0), never the deeper record.
    assert back["parent_record_id"] == 0
    assert back["parent_record_id"] != deep["record_id"]


# TEST-005 / AC-004 — a shallower set restores the largest active lower parent.
def test_shallower_set_restores_lower_parent() -> None:
    ctx = _ctx()
    set_nest_level(ctx, "pipeline")       # 1
    pipe = _write(ctx, 1)
    set_nest_level(ctx, "pipeline_step")  # 1 -> 2 descent; parent = pipe
    _write(ctx, 2)
    set_nest_level(ctx, "app")            # 2 -> 3 descent
    _write(ctx, 3)
    set_nest_level(ctx, "pipeline_step")  # 3 -> 2 return; deeper cleared
    back = _write(ctx, 2)
    # Back at pipeline_step, the largest active lower level is pipeline (1) -> pipe.
    assert back["parent_record_id"] == pipe["record_id"]


# TEST-006 / AC-005 — a direct base with no lower level falls back to synthetic root 0.
def test_direct_base_falls_back_to_root() -> None:
    ctx = _ctx()
    set_nest_level(ctx, "app")          # 0 -> 3 descent; last=0 -> parent 0
    assert _write(ctx, 3)["parent_record_id"] == 0


# TEST-007 — next_nest_level parent behavior is unchanged.
def test_next_nest_level_unchanged() -> None:
    ctx = _ctx()
    set_nest_level(ctx, "app")
    r1 = _write(ctx, 3)
    r2 = _write(ctx, 3)
    next_nest_level(ctx)                # parent = most recent record (r2)
    child = _write(ctx, 4)
    assert child["parent_record_id"] == r2["record_id"]


# TEST-008 / AC-006 — record_id sequencing is unchanged by descent parenting.
def test_record_id_sequencing_unchanged() -> None:
    ctx = _ctx()
    set_nest_level(ctx, "app")
    assert _write(ctx, 3)["record_id"] == 1
    _write(ctx, 3, fail=True)           # not committed -> no advance
    set_nest_level(ctx, "workflow")     # a base transition mid-sequence
    assert _write(ctx, 2)["record_id"] == 2
