"""Tests for a child Run executing a WorkflowStep (backlog 950).

The child Run is the step's execution instance: it executes the step's bound
operation with itself as the handler's execution context, handles the execution
error through AppError, and finishes its own lifecycle SUCCEEDED / FAILED /
SKIPPED. A child works in its parent's apply / data / metadata, and records its
step evidence in order; the logging is consumed unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.conftest import make_db_run_log, start_test_run
from tests.support.workflow_runs import RecordingControl, parent_run

from rey_lib.config.applications import Application, ApplicationOperation
from rey_lib.errors.error_utils import AppError
from rey_lib.logs import current_step
from rey_lib.run import Run
from rey_lib.run import run as run_module
from rey_lib.workflow.workflow_step import WorkflowStep

_SCHEMA = "https://json-schema.org/draft/2020-12/schema"


def _step(handler: Any, *, settings: dict[str, Any] | None = None) -> WorkflowStep:
    """Step s1 ("One") instantiating p1, approved with ``handler``."""
    application = Application(name="fixture_app", workflow_operations=(ApplicationOperation(
        name="p1", contract_definition={"$schema": _SCHEMA, "type": "object"}),))
    return WorkflowStep(id="s1", process_name="p1", label="One", ordinal=1,
                        application=application, operation_name="p1",
                        approved={"p1": handler}, settings=settings or {})


def _log(tmp_path: Path) -> Any:
    """A run log for a launched-looking run, both destinations, ids minted."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    ctx = SimpleNamespace(log_file=str(tmp_path / "app.jsonl"))
    start_test_run(ctx)
    return make_db_run_log(tmp_path, run_id=ctx.run_id, run_timestamp=str(ctx.run_timestamp),
                           path=str(tmp_path / "app.jsonl"))


def _read(run_log: Any) -> list[dict[str, Any]]:
    path = Path(run_log.path())
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _child(control: RecordingControl, parent: Run) -> Run:
    return Run.start(control, subject_type="step", subject_id="s1", subject_name="One",
                     app_name="fixture_app", settings={}, parent=parent)


def _execute(step: WorkflowStep, run_log: Any, *, control: RecordingControl | None = None,
             parent: Run | None = None) -> Run:
    """Execute ``step`` in a child Run, as sequence 1 of workflow wf, at the step level."""
    control = control or RecordingControl()
    parent = parent or parent_run(control)
    child = _child(control, parent)
    run_log.set_nest_level("workflow_step")
    child.log = run_log
    child.execute(step, SimpleNamespace(), sequence=1, workflow_name="wf")
    return child


def _raises(*_args: Any) -> None:
    raise RuntimeError("load failed")


def _returns_failed(*_args: Any) -> SimpleNamespace:
    return SimpleNamespace(status="failed", detail="validation failed")


# --- the child Run -----------------------------------------------------------


def test_a_child_run_records_its_own_identity_under_its_parent() -> None:
    control = RecordingControl()
    parent = parent_run(control, run_id=7)

    child = _child(control, parent)

    assert child.run_id != parent.run_id
    assert child.parent_run_id == 7
    assert control.rows == [{
        "run_id": child.run_id, "subject_type": "step", "subject_id": "s1",
        "subject_name": "One", "app_name": "fixture_app", "parent_run_id": 7,
        "settings": {}, "required": True,
    }]


def test_a_child_run_works_in_its_parents_apply_data_and_metadata() -> None:
    control = RecordingControl()
    parent = parent_run(control)
    parent.apply = True

    first, second = _child(control, parent), _child(control, parent)
    first.data["object_files"] = ["a.sql"]
    first.metadata["engine"] = "postgres"

    assert second.apply is True
    assert second.data["object_files"] == ["a.sql"]
    assert parent.metadata == {"engine": "postgres"}


# --- execution ---------------------------------------------------------------


def test_the_handler_receives_the_child_run_as_its_execution_context(tmp_path: Path) -> None:
    seen: list[Any] = []

    def handler(_ctx: Any, _run_log: Any, settings: dict[str, Any], run: Any,
                step: Any) -> None:
        seen.append((run, settings, run.apply, run.data, run.metadata, step))

    released = _step(handler, settings={"source": "a"})
    child = _execute(released, _log(tmp_path))

    run, settings, apply, data, metadata, step = seen[0]
    assert run is child
    assert settings == {"source": "a"}
    # The canonical step itself: its parent process holds the application config.
    assert step is released
    assert (apply, data, metadata) == (child.apply, child.data, child.metadata)


def test_success_finishes_the_run_succeeded_with_detail_and_artifacts(tmp_path: Path) -> None:
    control = RecordingControl()

    def handler(*_args: Any) -> SimpleNamespace:
        return SimpleNamespace(status="ok", detail="loaded", artifacts=["a.csv"])

    child = _execute(_step(handler), _log(tmp_path), control=control)

    assert (child.status, child.detail, child.artifacts) == ("SUCCEEDED", "loaded", ["a.csv"])
    assert control.finished[child.run_id] == "SUCCEEDED"
    assert child.failure_record_id is None


def test_a_raised_failure_finishes_the_run_failed_and_does_not_escape(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    run_log = _log(tmp_path)

    with caplog.at_level("ERROR"):
        child = _execute(_step(_raises), run_log)

    assert child.status == "FAILED"
    logged = [r for r in caplog.records if r.levelname == "ERROR" and r.exc_info]
    assert [type(r.exc_info[1]) for r in logged] == [AppError]
    assert type(logged[0].exc_info[1].__cause__) is RuntimeError
    failure = next(r for r in _read(run_log) if r["record_type"] == "STEP_FAILURE")
    assert child.failure_record_id == failure["error_message"]["failure_record_id"]
    assert child.error == "workflow 'wf' step 's1' failed"
    assert current_step() is None


def test_a_returned_failed_status_finishes_the_run_failed(tmp_path: Path) -> None:
    run_log = _log(tmp_path)

    child = _execute(_step(_returns_failed), run_log)

    assert (child.status, child.detail) == ("FAILED", "validation failed")
    failure = next(r for r in _read(run_log) if r["record_type"] == "STEP_FAILURE")
    assert failure["error_message"]["error_type"] == "WorkflowStepFailed"
    assert child.failure_record_id == failure["error_message"]["failure_record_id"]


def test_a_returned_skipped_status_finishes_the_run_skipped(tmp_path: Path) -> None:
    child = _execute(_step(lambda *_a: SimpleNamespace(status="skipped", detail="no file")),
                     _log(tmp_path))

    assert (child.status, child.detail) == ("SKIPPED", "no file")


@pytest.mark.parametrize("handler", [lambda *_a: None, _raises, _returns_failed],
                         ids=["ok", "raised", "returned_failed"])
def test_depth_entered_beneath_step_start_is_left_on_every_path(
    handler: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_log = _log(tmp_path)
    moves: list[str] = []
    enter, exit_ = run_log.enter, run_log.exit
    monkeypatch.setattr(run_log, "enter", lambda: (moves.append("enter"), enter())[1])
    monkeypatch.setattr(run_log, "exit", lambda: (moves.append("exit"), exit_())[1])

    _execute(_step(handler), run_log)

    assert moves == ["enter", "exit"]
    assert current_step() is None


# --- skip ----------------------------------------------------------------------


def test_a_dry_run_skip_records_the_step_and_finishes_skipped(tmp_path: Path) -> None:
    control = RecordingControl()
    run_log = _log(tmp_path)
    child = _child(control, parent_run(control))
    run_log.set_nest_level("workflow_step")

    child.log = run_log
    child.skip("dry-run", step=_step(_raises), sequence=2, workflow_name="wf")

    types = [r["record_type"] for r in _read(run_log)]
    assert types == ["STEP_START", "STEP_END"]
    end = _read(run_log)[1]
    assert (end["status"], end["message"], end["step_sequence"]) == ("skipped", "dry-run", 2)
    assert (child.status, control.finished[child.run_id]) == ("SKIPPED", "SKIPPED")


def test_an_earlier_failure_skip_writes_no_step_record(tmp_path: Path) -> None:
    control = RecordingControl()
    run_log = _log(tmp_path)
    child = _child(control, parent_run(control))

    child.skip("earlier step failed")

    assert _read(run_log) == []
    assert (child.status, child.detail) == ("SKIPPED", "earlier step failed")


# --- not run (backlog 964) -------------------------------------------------------


def test_not_run_is_its_own_terminal_status() -> None:
    assert run_module.NOT_RUN == "NOT RUN"
    assert run_module.NOT_RUN not in {
        run_module.SUCCEEDED, run_module.FAILED, run_module.SKIPPED, "ABORTED",
    }
    assert (run_module.SUCCEEDED, run_module.FAILED, run_module.SKIPPED) == (
        "SUCCEEDED", "FAILED", "SKIPPED")


def test_a_run_finished_not_run_persists_not_run() -> None:
    control = RecordingControl()
    child = _child(control, parent_run(control))

    child.finish(run_module.NOT_RUN)

    assert (child.status, control.finished[child.run_id]) == ("NOT RUN", "NOT RUN")


def test_a_not_released_step_logs_its_reason_and_finishes_not_run(tmp_path: Path) -> None:
    control = RecordingControl()
    run_log = _log(tmp_path)
    child = _child(control, parent_run(control))
    run_log.set_nest_level("workflow_step")

    child.log = run_log
    child.not_released(step=_step(_raises), sequence=3, workflow_name="wf")

    assert [r["record_type"] for r in _read(run_log)] == ["STEP_START", "STEP_END"]
    end = _read(run_log)[1]
    assert (end["status"], end["message"], end["step_sequence"]) == (
        "not run", "not released", 3)
    assert (child.status, child.detail) == ("NOT RUN", "not released")
    assert control.finished[child.run_id] == "NOT RUN"
    assert current_step() is None


# --- evidence order ---------------------------------------------------------------


def _record_order(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[str]:
    order: list[str] = []
    for name in ("log_step_start", "log_step_failure", "log_step_end"):
        original = getattr(module, name)

        def recording(*args: Any, _name: str = name, _original: Any = original,
                      **kwargs: Any) -> Any:
            order.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(module, name, recording)
    report = AppError.report

    def recording_report(self: AppError, *args: Any, **kwargs: Any) -> Any:
        order.append("report")
        return report(self, *args, **kwargs)

    monkeypatch.setattr(AppError, "report", recording_report)
    return order


@pytest.mark.parametrize("handler, expected", [
    (_raises, ["log_step_start", "report", "log_step_failure", "log_step_end"]),
    (_returns_failed, ["log_step_start", "log_step_end", "log_step_failure"]),
    (lambda *_args: None, ["log_step_start", "log_step_end"]),
], ids=["raised", "returned_failed", "ok"])
def test_step_evidence_is_recorded_in_order(
    handler: Any, expected: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = _record_order(monkeypatch, run_module)
    _execute(_step(handler), _log(tmp_path))

    assert order == expected
