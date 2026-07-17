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
from typing import Any

from rey_lib.config.config_utils import Namespace
from rey_lib.logs import (
    log_run_record,
    next_nest_level,
    set_nest_level,
)

# Synthetic root: the parent stamped on records with no active lower semantic level.
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
    """Rebuild the logical tree from parent_record_id and render it indented.

    Children are grouped by parent and walked from the synthetic root, preserving
    emission order among siblings, so the returned lines are the hierarchy the
    emitted records actually describe.
    """
    children: dict[int, list[dict[str, Any]]] = {}
    for record in records:
        children.setdefault(int(record["parent_record_id"]), []).append(record)

    lines: list[str] = []

    def walk(parent: int, depth: int) -> None:
        """Append each child of parent at the given indent, then recurse."""
        for record in children.get(parent, []):
            lines.append("  " * depth + _label(record))
            walk(int(record["record_id"]), depth + 1)

    walk(_ROOT, 0)
    return lines


def _pipeline_step_app(ctx: Namespace) -> None:
    """Emit the representative Pipeline -> Pipeline Step -> App spine."""
    set_nest_level(ctx, "pipeline")
    log_run_record(ctx, "RUN_START", pipeline_name="demo_pipeline")
    set_nest_level(ctx, "pipeline_step")
    log_run_record(ctx, "STEP_START", step_name="load")
    set_nest_level(ctx, "app")
    log_run_record(ctx, "RUN_START", app="rey_loader")


# -- scenario 1 ---------------------------------------------------------------

def test_pipeline_step_app_chain(tmp_path: Path) -> None:
    """Pipeline -> Pipeline Step -> App descends one owner per semantic base."""
    ctx = _ctx(tmp_path)
    _pipeline_step_app(ctx)

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
    set_nest_level(ctx, "pipeline")
    log_run_record(ctx, "RUN_START", pipeline_name="demo_pipeline")
    set_nest_level(ctx, "pipeline_step")
    log_run_record(ctx, "STEP_START", step_name="load")
    # A second step re-asserts the same base rather than descending again.
    set_nest_level(ctx, "pipeline_step")
    log_run_record(ctx, "STEP_START", step_name="analyze")

    pipeline, first, second = _records(tmp_path)
    assert _identity(pipeline) == (1, _ROOT, 1)
    assert _identity(first) == (2, 1, 2)
    assert _identity(second) == (3, 1, 2)
    # Both steps hang off the pipeline, not off each other.
    assert first["parent_record_id"] == second["parent_record_id"]
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
    set_nest_level(ctx, "app")
    log_run_record(ctx, "RUN_START", app="rey_analyzer")
    next_nest_level(ctx)
    log_run_record(ctx, "INPUT_FILE_REFERENCE", app="rey_analyzer", source_name="a.csv")
    log_run_record(ctx, "LLM_INTERPRETATION", app="rey_analyzer")
    log_run_record(ctx, "INPUT_FILE_REFERENCE", app="rey_analyzer", source_name="b.csv")
    log_run_record(ctx, "LLM_INTERPRETATION", app="rey_analyzer")

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
    set_nest_level(ctx, "app")
    log_run_record(ctx, "RUN_START", app="rey_loader")
    set_nest_level(ctx, "workflow")
    log_run_record(ctx, "RUN_START", app="rey_loader", workflow="daily_load")
    set_nest_level(ctx, "workflow_step")
    log_run_record(ctx, "STEP_START", app="rey_loader", step_name="extract")

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
    set_nest_level(ctx, "app")
    log_run_record(ctx, "RUN_START", app="rey_loader")

    (app,) = _records(tmp_path)
    # No pipeline exists, so the app anchors on the synthetic root without
    # its semantic level collapsing to 1.
    assert _identity(app) == (1, _ROOT, 3)


# -- scenario 6 ---------------------------------------------------------------

def test_direct_workflow_execution(tmp_path: Path) -> None:
    """A workflow with no active lower level parents to the root at its fixed base 4."""
    ctx = _ctx(tmp_path)
    set_nest_level(ctx, "workflow")
    log_run_record(ctx, "RUN_START", workflow="daily_load")
    set_nest_level(ctx, "workflow_step")
    log_run_record(ctx, "STEP_START", step_name="extract")

    workflow, step = _records(tmp_path)
    assert _identity(workflow) == (1, _ROOT, 4)
    assert _identity(step) == (2, 1, 5)


# -- scenario 7 ---------------------------------------------------------------

def test_return_from_app_to_pipeline_step(tmp_path: Path) -> None:
    """Re-asserting the step base after a deep app returns ownership to the step."""
    ctx = _ctx(tmp_path)
    _pipeline_step_app(ctx)
    # The app descends and never returns, as a real app body may leave it.
    set_nest_level(ctx, "workflow")
    log_run_record(ctx, "RUN_START", app="rey_loader", workflow="daily_load")
    # The coordinator re-asserts the step base on app return.
    set_nest_level(ctx, "pipeline_step")
    log_run_record(ctx, "STEP_END", step_name="load", status="success")

    records = _records(tmp_path)
    step_start, step_end = records[1], records[-1]
    # STEP_END returns to level 2 under the pipeline — a sibling of its STEP_START.
    assert _identity(step_end) == (5, 1, 2)
    assert step_end["parent_record_id"] == step_start["parent_record_id"]


# -- scenario 8 ---------------------------------------------------------------

def test_return_from_pipeline_step_to_pipeline(tmp_path: Path) -> None:
    """Re-asserting the pipeline base returns ownership to the run root."""
    ctx = _ctx(tmp_path)
    set_nest_level(ctx, "pipeline")
    log_run_record(ctx, "RUN_START", pipeline_name="demo_pipeline")
    set_nest_level(ctx, "pipeline_step")
    log_run_record(ctx, "STEP_START", step_name="load")
    set_nest_level(ctx, "pipeline")
    log_run_record(ctx, "RUN_COMPLETE", pipeline_name="demo_pipeline", status="success")

    run_start, _step, run_complete = _records(tmp_path)
    # Pipeline finalization returns to level 1 as a sibling of the pipeline RUN_START.
    assert _identity(run_complete) == (3, _ROOT, 1)
    assert run_complete["parent_record_id"] == run_start["parent_record_id"]


# -- scenario 9 ---------------------------------------------------------------

def test_failure_return_follows_ownership_reset(tmp_path: Path) -> None:
    """A failing step resets ownership exactly as the success path does."""
    ctx = _ctx(tmp_path)
    _pipeline_step_app(ctx)
    set_nest_level(ctx, "workflow")
    log_run_record(ctx, "ERROR", app="rey_loader", message="boom")
    # Failure return re-asserts the same base the success path re-asserts.
    set_nest_level(ctx, "pipeline_step")
    log_run_record(ctx, "STEP_FAILURE", step_name="load", status="failed")

    records = _records(tmp_path)
    step_start, step_failure = records[1], records[-1]
    assert _identity(step_failure) == (5, 1, 2)
    # The failed step lands where a successful STEP_END would (scenario 7).
    assert step_failure["parent_record_id"] == step_start["parent_record_id"]


# -- scenario 10 --------------------------------------------------------------

def test_record_sequence_is_continuous_and_parents_precede_children(tmp_path: Path) -> None:
    """record_id is gapless from 1 and every nonzero parent references an earlier record."""
    ctx = _ctx(tmp_path)
    _pipeline_step_app(ctx)
    set_nest_level(ctx, "workflow")
    log_run_record(ctx, "RUN_START", app="rey_loader", workflow="daily_load")
    set_nest_level(ctx, "workflow_step")
    log_run_record(ctx, "STEP_START", app="rey_loader", step_name="extract")
    set_nest_level(ctx, "pipeline_step")
    log_run_record(ctx, "STEP_END", step_name="load", status="success")
    set_nest_level(ctx, "pipeline")
    log_run_record(ctx, "RUN_COMPLETE", pipeline_name="demo_pipeline", status="success")

    records = _records(tmp_path)
    ids = [int(record["record_id"]) for record in records]
    assert ids == list(range(1, len(records) + 1))

    seen: set[int] = set()
    for record in records:
        parent = int(record["parent_record_id"])
        # A nonzero parent must already have been written; 0 is the synthetic root.
        assert parent == _ROOT or parent in seen
        seen.add(int(record["record_id"]))
