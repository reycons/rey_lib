"""Generic nesting contract for the shared hierarchy utility
(SGC_Rey_Log_Nest_Level_Phase_1, SGC_Rey_Log_Parent_Resolver_Semantic_Descent).

``set_nest_level`` establishes a parent/base scope; relative nesting beneath it begins
at ``minimum_nest_level = parent_level + 1``. These tests prove that contract through
the public hierarchy APIs only — no record-type, app, pipeline, workflow, or Console
knowledge is involved, and the semantics asserted here hold for every semantic base.

The contract under test:

    set_nest_level(level)   parent_level = level; minimum_nest_level = level + 1;
                            current_nest_level = level; relative context reset
    next_nest_level()       enters/descends the relative child hierarchy, starting at
                            minimum_nest_level
    previous_nest_level()   returns upward within the relative hierarchy, never below
                            minimum_nest_level
    record writes           never change parent_level or minimum_nest_level
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rey_lib.config.config_utils import Namespace
from rey_lib.logs import (
    get_nest_level,
    log_run_record,
    next_nest_level,
    previous_nest_level,
    set_nest_level,
)

# Synthetic root: the parent stamped on records with no active lower scope.
_ROOT = 0


def _ctx(tmp_path: Path) -> Namespace:
    """Build a context backed by a durable run log inside tmp_path."""
    return Namespace(
        {
            "run_log_dir": str(tmp_path),
            "app_name": "demo",
            "run_id": "run-demo",
            "run_timestamp": "20260101_000000",
        }
    )


def _records(tmp_path: Path) -> list[dict[str, Any]]:
    """Return every record appended to the run log, in emission order."""
    log = next(tmp_path.glob("*.jsonl"))
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def _identity(record: dict[str, Any]) -> tuple[int, int, int]:
    """Return the hierarchy triple (record_id, parent_record_id, nest_level)."""
    return (
        int(record["record_id"]),
        int(record["parent_record_id"]),
        int(record["nest_level"]),
    )


# -- base scope and relative floor --------------------------------------------

def test_set_establishes_parent_level_and_relative_nesting_starts_below_it() -> None:
    """set_nest_level establishes the parent level; next descends to parent + 1."""
    ctx = Namespace({})
    assert set_nest_level(ctx, "pipeline") == 1      # parent_level = 1
    assert get_nest_level(ctx) == 1                  # records may be written at 1
    assert next_nest_level(ctx) == 2                 # minimum_nest_level = 2
    assert next_nest_level(ctx) == 3                 # descends further
    assert next_nest_level(ctx) == 4


def test_previous_returns_upward_but_never_below_the_relative_floor() -> None:
    """previous_nest_level clamps at minimum_nest_level, not at zero."""
    ctx = Namespace({})
    set_nest_level(ctx, "pipeline")                  # parent 1, minimum 2
    next_nest_level(ctx)                             # 2
    next_nest_level(ctx)                             # 3
    assert previous_nest_level(ctx) == 2
    # The floor holds: further returns cannot escape the relative context.
    assert previous_nest_level(ctx) == 2
    assert previous_nest_level(ctx) == 2


def test_relative_floor_is_relative_to_the_established_base() -> None:
    """A deeper base raises the floor with it; the floor is always parent + 1."""
    ctx = Namespace({})
    set_nest_level(ctx, "pipeline_step")             # parent 2, minimum 3
    assert next_nest_level(ctx) == 3
    assert previous_nest_level(ctx) == 3             # cannot return to the base itself
    set_nest_level(ctx, "app")                       # parent 3, minimum 4
    assert next_nest_level(ctx) == 4
    assert previous_nest_level(ctx) == 4


def test_previous_at_the_base_does_not_descend() -> None:
    """previous_nest_level never moves deeper, even when sitting on the base."""
    ctx = Namespace({})
    set_nest_level(ctx, "pipeline")                  # current 1, minimum 2
    # The floor is below the current level here; a return must not push down to it.
    assert previous_nest_level(ctx) == 1


# -- writes never redefine the context -----------------------------------------

def test_record_writes_do_not_change_the_base_or_the_floor(tmp_path: Path) -> None:
    """Informational writes leave parent_level and minimum_nest_level intact."""
    ctx = _ctx(tmp_path)
    set_nest_level(ctx, "pipeline")
    log_run_record(ctx, "RUN_START", pipeline_name="demo_pipeline")
    log_run_record(ctx, "CONFIG_FILE_REFERENCE", path="installation.yaml")
    log_run_record(ctx, "EXECUTION_PLAN")
    # The base is unmoved by the writes, so relative nesting still starts at 2.
    assert get_nest_level(ctx) == 1
    assert next_nest_level(ctx) == 2


def test_relative_child_anchors_to_the_scope_owner_not_the_last_write(
    tmp_path: Path,
) -> None:
    """Records written at the base do not become the parent of the relative child."""
    ctx = _ctx(tmp_path)
    set_nest_level(ctx, "pipeline")
    log_run_record(ctx, "RUN_START", pipeline_name="demo_pipeline")
    log_run_record(ctx, "CONFIG_FILE_REFERENCE", path="installation.yaml")
    log_run_record(ctx, "EXECUTION_PLAN")
    next_nest_level(ctx)
    log_run_record(ctx, "STEP_START", step_name="prepare_trade_files")

    run_start, config, plan, step = _records(tmp_path)
    # Base-level records are siblings sharing the enclosing parent.
    assert _identity(run_start) == (1, _ROOT, 1)
    assert _identity(config) == (2, _ROOT, 1)
    assert _identity(plan) == (3, _ROOT, 1)
    # The relative child belongs to the scope, anchored on its first record.
    assert _identity(step) == (4, 1, 2)


# -- a later set resets the relative context -----------------------------------

def test_subsequent_set_resets_the_relative_context_and_rebases() -> None:
    """A new set_nest_level ends the prior relative context and rebases the floor."""
    ctx = Namespace({})
    set_nest_level(ctx, "pipeline")                  # parent 1, minimum 2
    next_nest_level(ctx)                             # 2
    next_nest_level(ctx)                             # 3 — left deep
    # The next base is authoritative regardless of the abandoned relative context.
    assert set_nest_level(ctx, "pipeline_step") == 2  # parent 2, minimum 3
    assert get_nest_level(ctx) == 2
    assert next_nest_level(ctx) == 3
    assert previous_nest_level(ctx) == 3             # the new floor, not the old one


def test_same_level_set_starts_a_new_sibling_scope(tmp_path: Path) -> None:
    """A set at the current level clears that level's anchor for a new sibling scope."""
    ctx = _ctx(tmp_path)
    set_nest_level(ctx, "pipeline")
    log_run_record(ctx, "RUN_START", pipeline_name="demo_pipeline")
    # Sibling scope one.
    set_nest_level(ctx, "pipeline_step")
    log_run_record(ctx, "STEP_START", step_name="step_one")
    log_run_record(ctx, "FILE_OPERATION", path="one.ctx.json")
    # Sibling scope two at the same level replaces the level-2 anchor.
    set_nest_level(ctx, "pipeline_step")
    log_run_record(ctx, "STEP_START", step_name="step_two")
    set_nest_level(ctx, "app")
    log_run_record(ctx, "RUN_START", app="rey_loader")

    pipeline, first_step, _file_op, second_step, app = _records(tmp_path)
    # Both steps are siblings anchored on the pipeline.
    assert _identity(first_step) == (2, 1, 2)
    assert _identity(second_step) == (4, 1, 2)
    # The app anchors on the second step — the new anchor, not the first step.
    assert _identity(app) == (5, int(second_step["record_id"]), 3)
    assert _identity(pipeline) == (1, _ROOT, 1)


def test_set_next_enters_collection_and_sibling_reopens_peer_branches(tmp_path: Path) -> None:
    """Next enters once; sibling replaces peer anchors without changing level."""
    ctx = _ctx(tmp_path)
    set_nest_level(ctx, "app")
    log_run_record(ctx, "RUN_START", app="demo")

    assert set_nest_level(ctx, "next") == 4
    assert set_nest_level(ctx, "sibling") == 4
    log_run_record(ctx, "INPUT_FILE_REFERENCE", display_name="v01.json")
    next_nest_level(ctx)
    log_run_record(ctx, "LLM_CONTRACT", contract_path="v01.md")
    assert previous_nest_level(ctx) == 4

    assert set_nest_level(ctx, "sibling") == 4
    log_run_record(ctx, "INPUT_FILE_REFERENCE", display_name="v02.json")
    next_nest_level(ctx)
    log_run_record(ctx, "LLM_CONTRACT", contract_path="v02.md")

    app, first_input, first_child, second_input, second_child = _records(tmp_path)
    assert _identity(app) == (1, _ROOT, 3)
    assert _identity(first_input) == (2, 1, 4)
    assert _identity(first_child) == (3, 2, 5)
    assert _identity(second_input) == (4, 1, 4)
    assert _identity(second_child) == (5, 4, 5)
    assert second_input["parent_record_id"] != first_input["record_id"]
    assert [record["record_id"] for record in _records(tmp_path)] == [1, 2, 3, 4, 5]


def test_set_then_write_orders_the_scope_spine(tmp_path: Path) -> None:
    """Each base's first record anchors the next base's records."""
    ctx = _ctx(tmp_path)
    set_nest_level(ctx, "pipeline")
    log_run_record(ctx, "RUN_START", pipeline_name="demo_pipeline")
    log_run_record(ctx, "CONFIG_FILE_REFERENCE", path="installation.yaml")
    set_nest_level(ctx, "pipeline_step")
    log_run_record(ctx, "STEP_START", step_name="prepare_trade_files")
    log_run_record(ctx, "FILE_OPERATION", path="prepare.ctx.json")
    log_run_record(ctx, "APP_EXECUTION", app="rey_loader")
    set_nest_level(ctx, "app")
    log_run_record(ctx, "RUN_START", app="rey_loader")

    records = _records(tmp_path)
    pipeline, _config, step, _file_op, _app_exec, app = records
    assert _identity(pipeline) == (1, _ROOT, 1)
    # The step's records anchor on the pipeline's first record.
    assert _identity(step) == (3, 1, 2)
    # The app's records anchor on the step's first record, not on APP_EXECUTION.
    assert _identity(app) == (6, 3, 3)
