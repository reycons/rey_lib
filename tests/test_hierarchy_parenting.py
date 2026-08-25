"""Direct hierarchy-mechanics tests for the shared nest-level and record-parenting
path (SGC_Rey_Log_Record_Parenting_Phase_2, SGC_Rey_Log_Parent_Resolver_Semantic_Descent,
SGC_Rey_Log_Hierarchy_Shared_Run_State_Correction).

These tests exercise the hierarchy APIs only — ``set_nest_level``/``next_nest_level``/
``previous_nest_level`` plus the shared record writer ``log_run_record`` — over a fake
but representative execution tree written to a temporary run log. Nothing here touches
the Console, the Tree object, or any projection code: the subject is the durable
``record_id`` / ``parent_record_id`` / ``nest_level`` stamped onto emitted records.

Each test uses its own ``tmp_path``, so the per-run companion state file starts fresh
and record ids begin at 1.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.conftest import make_db_run_log
from typing import Any

from rey_lib.config.config_utils import Namespace
from rey_lib.logs import (
    log_run_record,
)

# Synthetic root: the parent stamped on records with no active lower semantic level.
#: A root record has no parent row to point at, so its parent is NULL.
_ROOT = None


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


def _identity(record: dict[str, Any]) -> tuple[int, int | None, int]:
    """Return the hierarchy triple (run_log_id, parent_run_log_id, nest_level)."""
    parent = record["parent_run_log_id"]
    return (
        int(record["run_log_id"]),
        None if parent is None else int(parent),
        int(record["nest_level"]),
    )


def _label(record: dict[str, Any]) -> str:
    """Render a record as a short tree label from its existing identifying fields."""
    name = (
        record.get("pipeline_name")
        or record.get("workflow")
        or record.get("step_name")
        or record.get("source_name")
        or record.get("app")
        or ""
    )
    return f"{record['record_type']}({name})" if name else str(record["record_type"])


def _shape(records: list[dict[str, Any]]) -> list[str]:
    """Rebuild the logical tree from parent_run_log_id and render it indented.

    Children are grouped by parent and walked from the roots -- the records
    whose parent is NULL -- preserving emission order among siblings, so the
    returned lines are the hierarchy the emitted records actually describe.
    """
    children: dict[int | None, list[dict[str, Any]]] = {}
    for record in records:
        parent = record["parent_run_log_id"]
        children.setdefault(None if parent is None else int(parent),
                            []).append(record)

    lines: list[str] = []

    def walk(parent: int | None, depth: int) -> None:
        """Append each child of parent at the given indent, then recurse."""
        for record in children.get(parent, []):
            lines.append("  " * depth + _label(record))
            walk(int(record["run_log_id"]), depth + 1)

    walk(_ROOT, 0)
    return lines


def _pipeline_step_app(run_log, ctx: Namespace) -> None:
    """Emit the representative Pipeline -> Pipeline Step -> App spine."""
    run_log.set_nest_level("pipeline")
    log_run_record(run_log, "RUN_START", pipeline_name="demo_pipeline")
    run_log.set_nest_level("pipeline_step")
    log_run_record(run_log, "STEP_START", step_name="load")
    run_log.set_nest_level("app")
    log_run_record(run_log, "RUN_START", app="rey_loader")


# -- scenario 1 ---------------------------------------------------------------

def test_pipeline_step_app_chain(tmp_path: Path) -> None:
    """Pipeline -> Pipeline Step -> App descends one owner per semantic base."""
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    _pipeline_step_app(run_log, ctx)

    pipeline, step, app = _records(tmp_path)
    assert _identity(pipeline) == (1, _ROOT, 1)
    assert _identity(step) == (2, 1, 2)
    assert _identity(app) == (3, 2, 3)
    assert _shape(_records(tmp_path)) == [
        "RUN_START(demo_pipeline)",
        "  STEP_START(load)",
        "    RUN_START(rey_loader)",
    ]


# -- scenario 2 ---------------------------------------------------------------

def test_two_sibling_pipeline_steps(tmp_path: Path) -> None:
    """Re-setting the pipeline_step base returns to the pipeline as the shared parent."""
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    run_log.set_nest_level("pipeline")
    log_run_record(run_log, "RUN_START", pipeline_name="demo_pipeline")
    run_log.set_nest_level("pipeline_step")
    log_run_record(run_log, "STEP_START", step_name="load")
    # A second step re-asserts the same base rather than descending again.
    run_log.set_nest_level("pipeline_step")
    log_run_record(run_log, "STEP_START", step_name="analyze")

    pipeline, first, second = _records(tmp_path)
    assert _identity(pipeline) == (1, _ROOT, 1)
    assert _identity(first) == (2, 1, 2)
    assert _identity(second) == (3, 1, 2)
    # Both steps hang off the pipeline, not off each other.
    assert first["parent_run_log_id"] == second["parent_run_log_id"]
    assert _shape(_records(tmp_path)) == [
        "RUN_START(demo_pipeline)",
        "  STEP_START(load)",
        "  STEP_START(analyze)",
    ]


# -- scenario 3 ---------------------------------------------------------------

def test_two_sibling_analysis_branches(tmp_path: Path) -> None:
    """Analyses under one app share the app-level anchor as their parent.

    Mirrors the analyzer's real pattern: the app boundary establishes the app scope and
    writes RUN_START, then the command boundary enters the analysis scope once. That
    RUN_START anchors level 3, so every analysis below it is a sibling anchored on the
    app rather than on the previous analysis.
    """
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    run_log.set_nest_level("app")
    log_run_record(run_log, "RUN_START", app="rey_analyzer")
    run_log.enter()
    log_run_record(run_log, "INPUT_FILE_REFERENCE", app="rey_analyzer", source_name="a.csv")
    log_run_record(run_log, "LLM_INTERPRETATION", app="rey_analyzer")
    log_run_record(run_log, "INPUT_FILE_REFERENCE", app="rey_analyzer", source_name="b.csv")
    log_run_record(run_log, "LLM_INTERPRETATION", app="rey_analyzer")

    app, first_input, first_result, second_input, second_result = _records(tmp_path)
    assert _identity(app) == (1, _ROOT, 3)
    # Every analysis record is a sibling at level 4 anchored on the app record.
    assert _identity(first_input) == (2, 1, 4)
    assert _identity(first_result) == (3, 1, 4)
    assert _identity(second_input) == (4, 1, 4)
    assert _identity(second_result) == (5, 1, 4)


# -- scenario 4 ---------------------------------------------------------------

def test_app_workflow_workflow_step(tmp_path: Path) -> None:
    """A workflow nests inside its app, and a workflow step inside that workflow."""
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    run_log.set_nest_level("app")
    log_run_record(run_log, "RUN_START", app="rey_loader")
    run_log.set_nest_level("workflow")
    log_run_record(run_log, "RUN_START", app="rey_loader", workflow="daily_load")
    run_log.set_nest_level("workflow_step")
    log_run_record(run_log, "STEP_START", app="rey_loader", step_name="extract")

    app, workflow, step = _records(tmp_path)
    assert _identity(app) == (1, _ROOT, 3)
    assert _identity(workflow) == (2, 1, 4)
    assert _identity(step) == (3, 2, 5)
    assert _shape(_records(tmp_path)) == [
        "RUN_START(rey_loader)",
        "  RUN_START(daily_load)",
        "    STEP_START(extract)",
    ]


# -- scenario 5 ---------------------------------------------------------------

def test_direct_app_execution(tmp_path: Path) -> None:
    """An app invoked directly keeps its fixed base 3 and parents to the root."""
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    run_log.set_nest_level("app")
    log_run_record(run_log, "RUN_START", app="rey_loader")

    (app,) = _records(tmp_path)
    # No pipeline exists, so the app anchors on the synthetic root without
    # its semantic level collapsing to 1.
    assert _identity(app) == (1, _ROOT, 3)


# -- scenario 6 ---------------------------------------------------------------

def test_direct_workflow_execution(tmp_path: Path) -> None:
    """A workflow with no active lower level parents to the root at its fixed base 4."""
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    run_log.set_nest_level("workflow")
    log_run_record(run_log, "RUN_START", workflow="daily_load")
    run_log.set_nest_level("workflow_step")
    log_run_record(run_log, "STEP_START", step_name="extract")

    workflow, step = _records(tmp_path)
    assert _identity(workflow) == (1, _ROOT, 4)
    assert _identity(step) == (2, 1, 5)


# -- scenario 7 ---------------------------------------------------------------

def test_return_from_app_to_pipeline_step(tmp_path: Path) -> None:
    """Re-asserting the step base after a deep app returns ownership to the step."""
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    _pipeline_step_app(run_log, ctx)
    # The app descends and never returns, as a real app body may leave it.
    run_log.set_nest_level("workflow")
    log_run_record(run_log, "RUN_START", app="rey_loader", workflow="daily_load")
    # The coordinator re-asserts the step base on app return.
    run_log.set_nest_level("pipeline_step")
    log_run_record(run_log, "STEP_END", step_name="load", status="success")

    records = _records(tmp_path)
    step_start, step_end = records[1], records[-1]
    # STEP_END returns to level 2 under the pipeline — a sibling of its STEP_START.
    assert _identity(step_end) == (5, 1, 2)
    assert step_end["parent_run_log_id"] == step_start["parent_run_log_id"]


# -- scenario 8 ---------------------------------------------------------------

def test_return_from_pipeline_step_to_pipeline(tmp_path: Path) -> None:
    """Re-asserting the pipeline base returns ownership to the run root."""
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    run_log.set_nest_level("pipeline")
    log_run_record(run_log, "RUN_START", pipeline_name="demo_pipeline")
    run_log.set_nest_level("pipeline_step")
    log_run_record(run_log, "STEP_START", step_name="load")
    run_log.set_nest_level("pipeline")
    log_run_record(run_log, "RUN_COMPLETE", pipeline_name="demo_pipeline", status="success")

    run_start, _step, run_complete = _records(tmp_path)
    # Pipeline finalization returns to level 1 as a sibling of the pipeline RUN_START.
    assert _identity(run_complete) == (3, _ROOT, 1)
    assert run_complete["parent_run_log_id"] == run_start["parent_run_log_id"]


# -- scenario 9 ---------------------------------------------------------------

def test_failure_return_follows_ownership_reset(tmp_path: Path) -> None:
    """A failing step resets ownership exactly as the success path does."""
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    _pipeline_step_app(run_log, ctx)
    run_log.set_nest_level("workflow")
    log_run_record(run_log, "ERROR", app="rey_loader", message="boom")
    # Failure return re-asserts the same base the success path re-asserts.
    run_log.set_nest_level("pipeline_step")
    log_run_record(run_log, "STEP_FAILURE", step_name="load", status="failed")

    records = _records(tmp_path)
    step_start, step_failure = records[1], records[-1]
    assert _identity(step_failure) == (5, 1, 2)
    # The failed step lands where a successful STEP_END would (scenario 7).
    assert step_failure["parent_run_log_id"] == step_start["parent_run_log_id"]


# -- scenario 10 --------------------------------------------------------------

def test_record_sequence_is_continuous_and_parents_precede_children(tmp_path: Path) -> None:
    """run_log_id is gapless from 1 and every parent references an earlier record."""
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    _pipeline_step_app(run_log, ctx)
    run_log.set_nest_level("workflow")
    log_run_record(run_log, "RUN_START", app="rey_loader", workflow="daily_load")
    run_log.set_nest_level("workflow_step")
    log_run_record(run_log, "STEP_START", app="rey_loader", step_name="extract")
    run_log.set_nest_level("pipeline_step")
    log_run_record(run_log, "STEP_END", step_name="load", status="success")
    run_log.set_nest_level("pipeline")
    log_run_record(run_log, "RUN_COMPLETE", pipeline_name="demo_pipeline", status="success")

    records = _records(tmp_path)
    ids = [int(record["run_log_id"]) for record in records]
    assert ids == list(range(1, len(records) + 1))

    seen: set[int] = set()
    for record in records:
        parent = record["parent_run_log_id"]
        # A parent must already have been written -- the database mints the id
        # on insert, so a child cannot reference a row that does not exist yet.
        # A root has no parent row at all.
        assert parent is None or int(parent) in seen
        seen.add(int(record["run_log_id"]))
