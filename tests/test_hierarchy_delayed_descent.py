"""Delayed-descent hierarchy scenarios for the shared nest-level and record-parenting
path (SGC_Rey_Log_Record_Parenting_Phase_2, SGC_Rey_Log_Parent_Resolver_Semantic_Descent).

The scenarios in ``test_hierarchy_parenting`` all descend immediately after writing the
record that establishes a level, so the globally last-written record happens to be that
level's semantic owner. Real runs interleave informational records at a level before
descending, which those scenarios never exercised.

These tests define the target invariant:

    Writing additional records at the current level must not silently change which
    semantic owner parents the next deeper execution scope.

Each test emits through the existing hierarchy APIs only and asserts the durable
``record_id`` / ``parent_record_id`` / ``nest_level`` triple. Sequences mirror the real
coordinator ordering: the step base is re-asserted at the start of every step and again
in the inner ``finally`` the instant an app returns.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.conftest import make_db_run_log
from typing import Any

from rey_lib.config.config_utils import Namespace
from rey_lib.logs import log_run_record

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


def _by_type(records: list[dict[str, Any]], record_type: str) -> list[dict[str, Any]]:
    """Return every record of one type, in emission order."""
    return [r for r in records if r["record_type"] == record_type]


def _identity(record: dict[str, Any]) -> tuple[int, int | None, int]:
    """Return the hierarchy triple (run_log_id, parent_run_log_id, nest_level)."""
    parent = record["parent_run_log_id"]
    return (
        int(record["run_log_id"]),
        None if parent is None else int(parent),
        int(record["nest_level"]),
    )


# -- delayed descent: pipeline -> pipeline step -------------------------------

def test_pipeline_delayed_descent_anchors_to_the_pipeline_owner(tmp_path: Path) -> None:
    """Pipeline-level records written before the descent must not own the step."""
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    run_log.set_nest_level("pipeline")
    log_run_record(run_log, "RUN_START", pipeline_name="demo_pipeline")
    log_run_record(run_log, "CONFIG_FILE_REFERENCE", path="installation.yaml")
    log_run_record(run_log, "CONFIG_FILE_REFERENCE", path="pipeline.yaml")
    log_run_record(run_log, "EXECUTION_PLAN")
    run_log.set_nest_level("pipeline_step")
    log_run_record(run_log, "STEP_START", step_name="prepare_trade_files")

    records = _records(tmp_path)
    run_start = _by_type(records, "RUN_START")[0]
    step_start = _by_type(records, "STEP_START")[0]
    # The step belongs to the pipeline, not to the EXECUTION_PLAN written just before it.
    assert _identity(step_start) == (5, int(run_start["run_log_id"]), 2)


# -- delayed descent: pipeline step -> app ------------------------------------

def test_step_delayed_descent_anchors_to_the_step_owner(tmp_path: Path) -> None:
    """Step-level records written before the descent must not own the app."""
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    run_log.set_nest_level("pipeline")
    log_run_record(run_log, "RUN_START", pipeline_name="demo_pipeline")
    run_log.set_nest_level("pipeline_step")
    log_run_record(run_log, "STEP_START", step_name="prepare_trade_files")
    log_run_record(run_log, "FILE_OPERATION", path="prepare.ctx.json")
    log_run_record(run_log, "APP_EXECUTION", app="rey_loader")
    run_log.set_nest_level("app")
    log_run_record(run_log, "RUN_START", app="rey_loader")

    records = _records(tmp_path)
    step_start = _by_type(records, "STEP_START")[0]
    app_start = _by_type(records, "RUN_START")[1]
    # The app belongs to the step, not to the APP_EXECUTION written just before it.
    assert _identity(app_start) == (5, int(step_start["run_log_id"]), 3)


# -- delayed descent: app -> nested analysis scope -----------------------------

def test_app_delayed_descent_anchors_to_the_app_owner(tmp_path: Path) -> None:
    """App-level records written before the descent must not own the nested scope."""
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    run_log.set_nest_level("app")
    log_run_record(run_log, "RUN_START", app="rey_analyzer")
    log_run_record(run_log, "INPUT_DISCOVERED", path="a.csv")
    log_run_record(run_log, "INPUT_DISCOVERED", path="b.csv")
    run_log.enter()
    log_run_record(run_log, "INPUT_FILE_REFERENCE", source_name="a.csv")

    records = _records(tmp_path)
    app_start = _by_type(records, "RUN_START")[0]
    analysis = _by_type(records, "INPUT_FILE_REFERENCE")[0]
    # The analysis scope belongs to the app, not to the last INPUT_DISCOVERED.
    assert _identity(analysis) == (4, int(app_start["run_log_id"]), 4)


# -- sibling steps with intervening pipeline-level records ---------------------

def test_sibling_pipeline_steps_share_the_pipeline_owner(tmp_path: Path) -> None:
    """Both steps parent to the pipeline despite intervening pipeline/step records."""
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    run_log.set_nest_level("pipeline")
    log_run_record(run_log, "RUN_START", pipeline_name="demo_pipeline")
    log_run_record(run_log, "CONFIG_FILE_REFERENCE", path="pipeline.yaml")
    log_run_record(run_log, "EXECUTION_PLAN")
    # Step one, as the coordinator opens each sequential step.
    run_log.set_nest_level("pipeline_step")
    log_run_record(run_log, "STEP_START", step_name="prepare_trade_files")
    log_run_record(run_log, "FILE_OPERATION", path="prepare.ctx.json")
    log_run_record(run_log, "STEP_END", step_name="prepare_trade_files", status="success")
    # Step two.
    run_log.set_nest_level("pipeline_step")
    log_run_record(run_log, "STEP_START", step_name="redact_trade_inbox")

    records = _records(tmp_path)
    run_start = _by_type(records, "RUN_START")[0]
    first, second = _by_type(records, "STEP_START")
    assert int(first["parent_run_log_id"]) == int(run_start["run_log_id"])
    assert int(second["parent_run_log_id"]) == int(run_start["run_log_id"])
    assert int(first["nest_level"]) == int(second["nest_level"]) == 2


# -- sibling apps beneath one step ---------------------------------------------

def test_each_app_execution_has_its_own_pipeline_step(tmp_path: Path) -> None:
    """Repeated app invocations are separate App executions under their own steps.

    The supported shape is one App per Pipeline Step; the pipeline may invoke the same
    app more than once, each time under its own step. Mirrors the coordinator: the step
    base is re-asserted in the inner finally the instant each app returns.
    """
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    run_log.set_nest_level("pipeline")
    log_run_record(run_log, "RUN_START", pipeline_name="demo_pipeline")
    # Step one invokes rey_analyzer.
    run_log.set_nest_level("pipeline_step")
    log_run_record(run_log, "STEP_START", step_name="generate_trade_staging_tables")
    run_log.set_nest_level("app")
    log_run_record(run_log, "RUN_START", app="rey_analyzer")
    run_log.set_nest_level("pipeline_step")          # app returned
    log_run_record(run_log, "APP_EXECUTION", app="rey_analyzer")
    # Step two invokes rey_analyzer again, as a separate App execution.
    run_log.set_nest_level("pipeline_step")
    log_run_record(run_log, "STEP_START", step_name="generate_trade_final_ddl")
    run_log.set_nest_level("app")
    log_run_record(run_log, "RUN_START", app="rey_analyzer")

    records = _records(tmp_path)
    pipeline = _by_type(records, "RUN_START")[0]
    first_step, second_step = _by_type(records, "STEP_START")
    first_app, second_app = _by_type(records, "RUN_START")[1:3]
    # The steps are siblings beneath the pipeline.
    assert int(first_step["parent_run_log_id"]) == int(pipeline["run_log_id"])
    assert int(second_step["parent_run_log_id"]) == int(pipeline["run_log_id"])
    # Each app belongs to its own step.
    assert int(first_app["parent_run_log_id"]) == int(first_step["run_log_id"])
    assert int(second_app["parent_run_log_id"]) == int(second_step["run_log_id"])
    assert int(first_app["nest_level"]) == int(second_app["nest_level"]) == 3


def test_workflow_nests_inside_its_app_under_the_step(tmp_path: Path) -> None:
    """Pipeline -> Step -> App -> Workflow -> Workflow Step, with delayed writes.

    A workflow is a child of the App execution — never a peer of the App and never a
    direct child of the Pipeline Step — and informational records written at each level
    before the descent must not re-anchor the level below.
    """
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    run_log.set_nest_level("pipeline")
    log_run_record(run_log, "RUN_START", pipeline_name="demo_pipeline")
    log_run_record(run_log, "EXECUTION_PLAN")
    run_log.set_nest_level("pipeline_step")
    log_run_record(run_log, "STEP_START", step_name="prepare_trade_files")
    log_run_record(run_log, "APP_EXECUTION", app="rey_loader")
    run_log.set_nest_level("app")
    log_run_record(run_log, "RUN_START", app="rey_loader")
    log_run_record(run_log, "INPUT_DISCOVERED", path="a.csv")
    run_log.set_nest_level("workflow")
    log_run_record(run_log, "RUN_START", app="rey_loader", workflow="daily_load")
    log_run_record(run_log, "CONFIG_FILE_REFERENCE", path="workflow.yaml")
    run_log.set_nest_level("workflow_step")
    log_run_record(run_log, "STEP_START", app="rey_loader", step_name="extract")

    records = _records(tmp_path)
    pipeline, app_start, workflow = _by_type(records, "RUN_START")
    pipeline_step, workflow_step = _by_type(records, "STEP_START")
    assert _identity(pipeline) == (1, _ROOT, 1)
    assert _identity(pipeline_step) == (3, 1, 2)
    # The app anchors on its step, not on the APP_EXECUTION written just before it.
    assert _identity(app_start) == (5, 3, 3)
    # The workflow is a child of the app, not of the pipeline step.
    assert _identity(workflow) == (7, 5, 4)
    # The workflow step is a child of the workflow, not of the app.
    assert _identity(workflow_step) == (9, 7, 5)


# -- successful return followed by another descent ------------------------------

def test_successful_return_then_descent_anchors_to_the_new_step(tmp_path: Path) -> None:
    """After a step completes, the next step's app parents to the next step."""
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    run_log.set_nest_level("pipeline")
    log_run_record(run_log, "RUN_START", pipeline_name="demo_pipeline")
    # Step one runs an app and completes.
    run_log.set_nest_level("pipeline_step")
    log_run_record(run_log, "STEP_START", step_name="step_one")
    run_log.set_nest_level("app")
    log_run_record(run_log, "RUN_START", app="rey_loader")
    run_log.set_nest_level("pipeline_step")          # app returned
    log_run_record(run_log, "STEP_END", step_name="step_one", status="success")
    # Step two opens and runs its own app.
    run_log.set_nest_level("pipeline_step")
    log_run_record(run_log, "STEP_START", step_name="step_two")
    run_log.set_nest_level("app")
    log_run_record(run_log, "RUN_START", app="rey_analyzer")

    records = _records(tmp_path)
    second_step = _by_type(records, "STEP_START")[1]
    second_app = _by_type(records, "RUN_START")[2]
    # The second app belongs to the second step, not the first.
    assert int(second_app["parent_run_log_id"]) == int(second_step["run_log_id"])


# -- failed return followed by another descent ----------------------------------

def test_failed_return_then_descent_anchors_to_the_new_step(tmp_path: Path) -> None:
    """A failed step resets ownership exactly as the success path does."""
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    run_log.set_nest_level("pipeline")
    log_run_record(run_log, "RUN_START", pipeline_name="demo_pipeline")
    # Step one fails inside its app.
    run_log.set_nest_level("pipeline_step")
    log_run_record(run_log, "STEP_START", step_name="step_one")
    run_log.set_nest_level("app")
    log_run_record(run_log, "RUN_START", app="rey_loader")
    log_run_record(run_log, "ERROR", app="rey_loader", message="boom")
    run_log.set_nest_level("pipeline_step")          # app returned by exception
    log_run_record(run_log, "STEP_FAILURE", step_name="step_one", status="failed")
    # Step two opens and runs its own app.
    run_log.set_nest_level("pipeline_step")
    log_run_record(run_log, "STEP_START", step_name="step_two")
    run_log.set_nest_level("app")
    log_run_record(run_log, "RUN_START", app="rey_analyzer")

    records = _records(tmp_path)
    pipeline = _by_type(records, "RUN_START")[0]
    failure = _by_type(records, "STEP_FAILURE")[0]
    second_step = _by_type(records, "STEP_START")[1]
    second_app = _by_type(records, "RUN_START")[2]
    # The failure record stays a step-level sibling under the pipeline.
    assert _identity(failure) == (int(failure["run_log_id"]), int(pipeline["run_log_id"]), 2)
    assert int(second_app["parent_run_log_id"]) == int(second_step["run_log_id"])


# -- direct executions with intervening same-level records ----------------------

def test_direct_app_with_intervening_records(tmp_path: Path) -> None:
    """A directly invoked app owns its nested scope despite intervening records."""
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    run_log.set_nest_level("app")
    log_run_record(run_log, "RUN_START", app="rey_loader")
    log_run_record(run_log, "INPUT_DISCOVERED", path="a.csv")
    log_run_record(run_log, "ROW_COUNT", rows=10)
    run_log.set_nest_level("workflow")
    log_run_record(run_log, "RUN_START", app="rey_loader", workflow="daily_load")

    records = _records(tmp_path)
    app_start, workflow = _by_type(records, "RUN_START")
    assert _identity(app_start) == (1, _ROOT, 3)
    assert _identity(workflow) == (4, int(app_start["run_log_id"]), 4)


def test_direct_workflow_with_intervening_records(tmp_path: Path) -> None:
    """A directly invoked workflow owns its steps despite intervening records."""
    ctx = _ctx(tmp_path)
    run_log = make_db_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    run_log.set_nest_level("workflow")
    log_run_record(run_log, "RUN_START", workflow="daily_load")
    log_run_record(run_log, "INPUT_DISCOVERED", path="a.csv")
    log_run_record(run_log, "ROW_COUNT", rows=10)
    run_log.set_nest_level("workflow_step")
    log_run_record(run_log, "STEP_START", step_name="extract")

    records = _records(tmp_path)
    workflow = _by_type(records, "RUN_START")[0]
    step = _by_type(records, "STEP_START")[0]
    assert _identity(workflow) == (1, _ROOT, 4)
    assert _identity(step) == (4, int(workflow["run_log_id"]), 5)
