"""Tests for WorkflowStep, the instantiated ApplicationOperation (backlog 952).

Binding happens once, at construction, against the application's published
operations and its approved implementation catalog. ``execute`` validates the
step's own settings, writes STEP_START / STEP_END, invokes the implementation,
and settles the step's own outcome. The parity tests run the same recording
handlers through the recovered ``run_workflow`` (backlog 949) and compare the
step evidence and its order.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.conftest import make_db_run_log, start_test_run
from tests.support.workflow_publication import prepared

from rey_lib.config.applications import Application, ApplicationOperation
from rey_lib.errors.error_utils import AppError
from rey_lib.logs import current_step
from rey_lib.workflow import RunContext, WorkflowError, run_workflow
from rey_lib.workflow import coordinator
from rey_lib.workflow import workflow_step
from rey_lib.workflow.workflow_step import WorkflowStep

#: Fields that identify a run, a write or a source location rather than the step.
_VOLATILE = frozenset({
    "run_id", "run_log_id", "run_timestamp", "timestamp", "parent_run_log_id", "duration_ms",
})
_VOLATILE_ERROR = frozenset({"failure_record_id", "sanitized_traceback", "traceback_summary"})

_SCHEMA = "https://json-schema.org/draft/2020-12/schema"


def _operation(name: str, definition: dict[str, Any] | None = None) -> ApplicationOperation:
    """An operation publishing ``definition``, or one accepting no settings."""
    return ApplicationOperation(
        name=name,
        contract_definition=definition or {
            "$schema": _SCHEMA, "type": "object", "additionalProperties": False,
        },
    )


def _application(*operations: ApplicationOperation, name: str = "fixture_app") -> Application:
    """An application publishing ``operations``."""
    return Application(name=name, workflow_operations=operations)


def _step(
    handler: Any,
    *,
    application: Application | None = None,
    settings: dict[str, Any] | None = None,
) -> WorkflowStep:
    """Step s1 ("One") instantiating p1, approved with ``handler``."""
    return WorkflowStep(
        id="s1",
        process_name="p1",
        label="One",
        ordinal=1,
        application=application or _application(_operation("p1")),
        operation_name="p1",
        approved={"p1": handler},
        settings=settings or {},
    )


def _log(tmp_path: Path) -> Any:
    """A run log for a launched-looking run, both destinations, ids minted."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    ctx = SimpleNamespace(log_file=str(tmp_path / "app.jsonl"))
    start_test_run(ctx)
    return make_db_run_log(
        tmp_path,
        run_id=ctx.run_id,
        run_timestamp=str(ctx.run_timestamp),
        path=str(tmp_path / "app.jsonl"),
    )


def _read(run_log: Any) -> list[dict[str, Any]]:
    """Every JSONL record the run log wrote."""
    return [json.loads(line) for line in Path(run_log.path()).read_text(
        encoding="utf-8").splitlines()]


def _step_records(run_log: Any) -> list[dict[str, Any]]:
    """The STEP_* records, without the fields that identify run, write or source."""
    stable = []
    for record in _read(run_log):
        if not record["record_type"].startswith("STEP_"):
            continue
        record = {key: value for key, value in record.items() if key not in _VOLATILE}
        if isinstance(record.get("error_message"), dict):
            record["error_message"] = {
                key: value for key, value in record["error_message"].items()
                if key not in _VOLATILE_ERROR
            }
        stable.append(record)
    return stable


def _execute(step: WorkflowStep, run_log: Any) -> Any:
    """Execute ``step`` as sequence 1 of workflow wf, inside a workflow-level scope."""
    run_log.set_nest_level("workflow")
    return step.execute(SimpleNamespace(), run_log, RunContext(apply=True),
                        sequence=1, workflow_name="wf")


def _record_order(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[str]:
    """Record, in order, every AppError report and step record ``module`` writes."""
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


def _legacy(handler: Any, tmp_path: Path) -> Any:
    """Run one step s1 ("One", process p1) through the recovered run_workflow."""
    run_log = _log(tmp_path)
    workflow = {
        "name": "wf",
        "processes": {"p1": {}},
        "steps": [{"id": "s1", "label": "One", "process": "p1"}],
    }
    _wf, _ctx = prepared(workflow, SimpleNamespace())
    run_workflow(_ctx, run_log, _wf, {"p1": handler})
    return run_log


def _raises(_ctx: Any, _run_log: Any, _settings: dict[str, Any], _run: RunContext) -> None:
    raise RuntimeError("load failed")


def _returns_failed(_ctx: Any, _run_log: Any, _settings: dict[str, Any],
                    _run: RunContext) -> SimpleNamespace:
    return SimpleNamespace(status="failed", detail="validation failed")


# --- Binding ----------------------------------------------------------------


def test_binding_holds_the_published_operation_and_approved_implementation() -> None:
    operation = _operation("p1")
    step = _step(_raises, application=_application(operation))

    assert step.operation is operation
    assert step.implementation is _raises
    assert step.outcome is None
    assert step.failure_record_id is None


def test_binding_refuses_an_operation_the_application_does_not_publish() -> None:
    with pytest.raises(WorkflowError, match="does not publish"):
        _step(_raises, application=_application(_operation("other")))


def test_binding_refuses_an_operation_another_application_publishes() -> None:
    elsewhere = _application(_operation("p1"), name="elsewhere")
    with pytest.raises(WorkflowError, match="does not publish"):
        WorkflowStep(
            id="s1", process_name="p1", label="One", ordinal=1,
            application=_application(_operation("other")),
            operation_name=elsewhere.workflow_operations[0].name,
            approved={"p1": _raises}, settings={},
        )


def test_binding_refuses_a_published_operation_not_in_the_approved_catalog() -> None:
    with pytest.raises(WorkflowError, match="approved implementation catalog"):
        WorkflowStep(
            id="s1", process_name="p1", label="One", ordinal=1,
            application=_application(_operation("p1")),
            operation_name="p1", approved={}, settings={},
        )


# --- Contract ---------------------------------------------------------------


def test_a_contract_violation_refuses_before_any_record_and_never_invokes(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def handler(*_args: Any) -> None:
        calls.append("called")

    required = _operation("p1", {
        "$schema": _SCHEMA, "type": "object",
        "properties": {"source": {"type": "string"}}, "required": ["source"],
    })
    step = _step(handler, application=_application(required))
    run_log = _log(tmp_path)

    with pytest.raises(WorkflowError, match="do not satisfy the contract"):
        _execute(step, run_log)

    assert calls == []
    # Nothing was written: the run log's file was never created.
    assert not Path(run_log.path()).exists()
    assert step.outcome is None


def test_unsupported_settings_are_warned_about_and_the_step_runs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    step = _step(lambda *_args: None, settings={"stray": 1})

    with caplog.at_level("WARNING"):
        outcome = _execute(step, _log(tmp_path))

    assert outcome.status == "ok"
    assert any("does not support" in record.getMessage() for record in caplog.records)


# --- Execution --------------------------------------------------------------


def test_the_implementation_receives_the_steps_own_settings(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def handler(_ctx: Any, _run_log: Any, settings: dict[str, Any], _run: RunContext) -> None:
        seen.append(settings)

    accepts = _operation("p1", {
        "$schema": _SCHEMA, "type": "object", "properties": {"source": {}},
        "additionalProperties": False,
    })
    step = _step(handler, application=_application(accepts), settings={"source": "a"})
    _execute(step, _log(tmp_path))

    assert seen == [{"source": "a"}]
    assert seen[0] is step.settings


def test_success_settles_status_detail_and_artifacts(tmp_path: Path) -> None:
    def handler(*_args: Any) -> SimpleNamespace:
        return SimpleNamespace(status="ok", detail="loaded", artifacts=["a.csv"])

    step = _step(handler)
    outcome = _execute(step, _log(tmp_path))

    assert outcome is step.outcome
    assert (outcome.id, outcome.label, outcome.process, outcome.status) == (
        "s1", "One", "p1", "ok")
    assert outcome.detail == "loaded"
    assert outcome.artifacts == ["a.csv"]
    assert step.failure_record_id is None


def test_a_raised_failure_is_settled_on_the_step_and_does_not_escape(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    step = _step(_raises)
    run_log = _log(tmp_path)

    with caplog.at_level("ERROR"):
        outcome = _execute(step, run_log)

    assert outcome.status == "failed"
    assert step.outcome is outcome
    logged = [r for r in caplog.records if r.levelname == "ERROR" and r.exc_info]
    assert [type(r.exc_info[1]) for r in logged] == [WorkflowError]
    assert type(logged[0].exc_info[1].__cause__) is RuntimeError
    failure = next(r for r in _read(run_log) if r["record_type"] == "STEP_FAILURE")
    assert step.failure_record_id == failure["error_message"]["failure_record_id"]
    assert current_step() is None


def test_a_returned_failed_status_is_settled_on_the_step(tmp_path: Path) -> None:
    step = _step(_returns_failed)
    run_log = _log(tmp_path)

    outcome = _execute(step, run_log)

    assert (outcome.status, outcome.detail) == ("failed", "validation failed")
    failure = next(r for r in _read(run_log) if r["record_type"] == "STEP_FAILURE")
    assert failure["error_message"]["error_type"] == "WorkflowStepFailed"
    assert step.failure_record_id == failure["error_message"]["failure_record_id"]
    assert current_step() is None


@pytest.mark.parametrize("handler", [
    lambda *_args: None, _raises, _returns_failed,
], ids=["ok", "raised", "returned_failed"])
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


def test_step_records_descend_from_step_start(tmp_path: Path) -> None:
    run_log = _log(tmp_path)
    _execute(_step(_raises), run_log)

    records = _read(run_log)
    start = next(r for r in records if r["record_type"] == "STEP_START")
    for kind in ("STEP_FAILURE", "STEP_END"):
        record = next(r for r in records if r["record_type"] == kind)
        assert record["parent_run_log_id"] == start["run_log_id"]


# --- Parity with the recovered run_workflow ---------------------------------


@pytest.mark.parametrize("handler, expected", [
    (_raises, ["log_step_start", "report", "log_step_failure", "log_step_end"]),
    (_returns_failed, ["log_step_start", "log_step_end", "log_step_failure"]),
    (lambda *_args: None, ["log_step_start", "log_step_end"]),
], ids=["raised", "returned_failed", "ok"])
def test_evidence_order_matches_run_workflow(
    handler: Any, expected: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_order = _record_order(monkeypatch, coordinator)
    _legacy(handler, tmp_path / "legacy")
    legacy = list(legacy_order)

    new_order = _record_order(monkeypatch, workflow_step)
    _execute(_step(handler), _log(tmp_path / "new"))

    assert legacy == expected
    assert new_order == expected


@pytest.mark.parametrize("handler", [
    _raises, _returns_failed, lambda *_args: None,
], ids=["raised", "returned_failed", "ok"])
def test_step_records_match_run_workflow(handler: Any, tmp_path: Path) -> None:
    legacy = _legacy(handler, tmp_path / "legacy")
    new = _log(tmp_path / "new")
    _execute(_step(handler), new)

    assert _step_records(new) == _step_records(legacy)
