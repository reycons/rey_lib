"""Tests for Workflow, the orchestrator of a hydrated graph (backlog 950).

``Workflow.run(parent_run, ...)`` releases each eligible step, checking its
contract first, assigns its sequence and indent, and creates one child Run for
it under the parent Run (a skipped step gets one too). The child Run executes
the step and finishes itself; the completed step is removed from the active
steps, and ``run`` returns the parent Run. The parity tests run one fixture
through the recovered ``run_workflow`` (backlog 949) and through a Workflow
graph built from it, with the same recording handlers, and compare call order,
step outcomes (child Run statuses) and the run-log records.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from tests.conftest import make_db_run_log, start_test_run
from tests.support.workflow_publication import prepared
from tests.support.workflow_runs import RecordingControl, parent_run

from rey_lib.config.applications import Application, ApplicationOperation
from rey_lib.errors.error_utils import AppError
from rey_lib.run import Run
from rey_lib.workflow import run_workflow
from rey_lib.workflow import workflow as workflow_module
from rey_lib.workflow.coordinator import _deep_merge
from rey_lib.workflow.engine import WorkflowError as LegacyWorkflowError
from rey_lib.workflow.workflow import Workflow
from rey_lib.workflow.workflow_process import WorkflowProcess
from rey_lib.workflow.workflow_step import WorkflowStep

_ENGINE_KEYS = frozenset({"implementation", "apply_only", "always_run", "enabled", "name"})
#: Fields that identify a run, a moment or a source location rather than behaviour.
_VOLATILE = frozenset({"run_id", "run_timestamp", "timestamp", "duration_ms", "failure_record_id"})
_VOLATILE_ERROR = frozenset({"failure_record_id", "sanitized_traceback", "traceback_summary"})
#: The records whose content is compared; every other record is compared by type.
_COMPARED = frozenset({"RUN_START", "RUN_COMPLETE", "STEP_START", "STEP_END", "STEP_FAILURE"})
#: A legacy outcome status and the child Run's terminal status that stands for it.
_STATUS = {"ok": "SUCCEEDED", "skipped": "SKIPPED", "failed": "FAILED"}
_SCHEMA = "https://json-schema.org/draft/2020-12/schema"


# --- fixtures ---------------------------------------------------------------


def _fixture() -> dict[str, Any]:
    """Four steps with distinct ids/labels/processes; p2 is apply_only."""
    return {
        "name": "wf",
        "processes": {"p1": {}, "p2": {"apply_only": True}, "p3": {}, "p4": {}},
        "steps": [
            {"id": "s1", "label": "One", "process": "p1"},
            {"id": "s2", "label": "Two", "process": "p2"},
            {"id": "s3", "label": "Three", "process": "p3"},
            {"id": "s4", "label": "Four", "process": "p4"},
        ],
    }


def _recording(calls: list[str], behaviour: dict[str, Callable[[], Any]] | None = None
               ) -> dict[str, Any]:
    """Handlers p1..p4 that record their process, then do what ``behaviour`` says."""
    behaviour = behaviour or {}

    def make(name: str) -> Any:
        def handler(_ctx: Any, _run_log: Any, _settings: dict[str, Any], _run: Any) -> Any:
            calls.append(name)
            return behaviour[name]() if name in behaviour else None
        return handler
    return {name: make(name) for name in ("p1", "p2", "p3", "p4")}


def _log(tmp_path: Path) -> Any:
    """A run log for a launched-looking run, both destinations, ids minted."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    ctx = SimpleNamespace(log_file=str(tmp_path / "app.jsonl"))
    start_test_run(ctx)
    return make_db_run_log(tmp_path, run_id=ctx.run_id, run_timestamp=str(ctx.run_timestamp),
                           path=str(tmp_path / "app.jsonl"))


def _build(workflow: dict[str, Any], application: Application, approved: dict[str, Any],
           control: Any) -> Workflow:
    """The graph the hydrator would create: settings composed once, here."""
    processes = workflow.get("processes") or {}
    steps = []
    for ordinal, step in enumerate(workflow["steps"], start=1):
        declared = dict(processes.get(step["process"]) or {})
        # Composed as the legacy engine composes: nested mappings merge.
        merged = _deep_merge(declared, dict(step.get("config") or {}))
        steps.append(WorkflowStep(
            id=step["id"],
            process_name=step["process"],
            label=step.get("label") or step["id"],
            ordinal=ordinal,
            application=application,
            operation_name=str(declared.get("implementation") or step["process"]),
            approved=approved,
            settings={key: value for key, value in merged.items() if key not in _ENGINE_KEYS},
            apply_only=bool(merged.get("apply_only")),
            always_run=bool(merged.get("always_run")),
        ))
    process = WorkflowProcess(name=application.name, ordinal=1, application=application,
                              config=None, steps=steps)
    return Workflow(name=workflow["name"], enabled=workflow.get("enabled", True),
                    tokens=None, config=None, processes=[process], control=control)


def _both(tmp_path: Path, workflow: dict[str, Any], *,
          behaviour: dict[str, Callable[[], Any]] | None = None, **selection: Any
          ) -> tuple[Any, Any]:
    """Run ``workflow`` on both paths.

    Returns (legacy run, legacy calls, legacy log) and (parent Run, new calls,
    new log, control).
    """
    legacy_calls: list[str] = []
    legacy_log = _log(tmp_path / "legacy")
    legacy_wf, legacy_ctx = prepared(workflow, SimpleNamespace())
    legacy_run = run_workflow(legacy_ctx, legacy_log, legacy_wf,
                              _recording(legacy_calls, behaviour), **selection)

    new_calls: list[str] = []
    new_log = _log(tmp_path / "new")
    control = RecordingControl()
    new_wf, new_ctx = prepared(workflow, SimpleNamespace())
    graph = _build(new_wf, new_ctx.applications[0], _recording(new_calls, behaviour), control)
    returned = graph.run(parent_run(control), new_ctx, new_log, **selection)
    return (legacy_run, legacy_calls, legacy_log), (returned, new_calls, new_log, control)


def _records(run_log: Any) -> list[Any]:
    """Every record's type, with the content of the compared records, volatile fields out."""
    path = Path(run_log.path())
    if not path.exists():
        return []
    stable: list[Any] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record["record_type"] not in _COMPARED:
            stable.append(record["record_type"])
            continue
        record = {key: value for key, value in record.items() if key not in _VOLATILE}
        if isinstance(record.get("error_message"), dict):
            record["error_message"] = {
                key: value for key, value in record["error_message"].items()
                if key not in _VOLATILE_ERROR
            }
        stable.append(record)
    return stable


def _assert_parity(legacy: Any, new: Any) -> None:
    """Same call order, step outcomes and run-log records on both paths."""
    (legacy_run, legacy_calls, legacy_log), (returned, new_calls, new_log, control) = legacy, new
    assert isinstance(returned, Run)
    assert new_calls == legacy_calls
    assert [(row["subject_id"], row["status"]) for row in control.children(returned)] == [
        (outcome.id, _STATUS[outcome.status]) for outcome in legacy_run.outcomes
    ]
    assert _records(new_log) == _records(legacy_log)


def _failed(detail: str) -> Callable[[], Any]:
    return lambda: SimpleNamespace(status="failed", detail=detail)


# --- parity: selection, ordering, dry-run -----------------------------------


@pytest.mark.parametrize("selection", [
    {},
    {"apply": False},
    {"step": "s3"},
    {"step": "Three"},
    {"step": "p3"},
    {"from_step": "s2"},
    {"to_step": "s3"},
    {"from_step": "s2", "to_step": "s3"},
    {"from_step": "s1", "to_step": "s3", "apply": False},
], ids=["full", "dry_run", "step_by_id", "step_by_label", "step_by_process", "from_step",
        "to_step", "range", "dry_run_range"])
def test_selection_and_dry_run_match_run_workflow(selection: dict[str, Any],
                                                  tmp_path: Path) -> None:
    _assert_parity(*_both(tmp_path, _fixture(), **selection))


@pytest.mark.parametrize("selection, message", [
    ({"step": "nope"}, "no step matches identifier 'nope'"),
    ({"step": "s1", "from_step": "s2"}, "'step' cannot be combined"),
    ({"from_step": "s3", "to_step": "s1"}, "empty range"),
], ids=["unknown", "step_and_range", "reversed"])
def test_selection_refusals_match_run_workflow(selection: dict[str, Any], message: str,
                                               tmp_path: Path) -> None:
    legacy_wf, legacy_ctx = prepared(_fixture(), SimpleNamespace())
    with pytest.raises(LegacyWorkflowError) as legacy:
        run_workflow(legacy_ctx, _log(tmp_path / "legacy"), legacy_wf,
                     _recording([]), **selection)
    control = RecordingControl()
    new_wf, new_ctx = prepared(_fixture(), SimpleNamespace())
    graph = _build(new_wf, new_ctx.applications[0], _recording([]), control)
    with pytest.raises(AppError) as new:
        graph.run(parent_run(control), new_ctx, _log(tmp_path / "new"), **selection)

    assert message in str(new.value)
    assert str(new.value) == str(legacy.value)
    assert control.rows == []


def test_an_ambiguous_identifier_matches_run_workflow(tmp_path: Path) -> None:
    workflow = _fixture()
    workflow["steps"][1]["label"] = "One"
    legacy_wf, legacy_ctx = prepared(workflow, SimpleNamespace())
    with pytest.raises(LegacyWorkflowError) as legacy:
        run_workflow(legacy_ctx, _log(tmp_path / "legacy"), legacy_wf, _recording([]),
                     step="One")
    control = RecordingControl()
    new_wf, new_ctx = prepared(workflow, SimpleNamespace())
    with pytest.raises(AppError) as new:
        _build(new_wf, new_ctx.applications[0], _recording([]), control).run(
            parent_run(control), new_ctx, _log(tmp_path / "new"), step="One")

    assert "ambiguous" in str(new.value)
    assert str(new.value) == str(legacy.value)


# --- parity: failure progression ---------------------------------------------


def test_a_failure_skips_ordinary_steps_and_always_run_still_runs(tmp_path: Path) -> None:
    workflow = _fixture()
    workflow["processes"] = {"p1": {}, "p2": {}, "p3": {"always_run": True}, "p4": {}}

    legacy, new = _both(tmp_path, workflow, behaviour={"p1": _failed("p1 broke")})

    _assert_parity(legacy, new)
    assert new[1] == ["p1", "p3"]


def test_the_first_failure_is_retained_when_an_always_run_step_also_fails(
    tmp_path: Path,
) -> None:
    workflow = _fixture()
    workflow["processes"] = {"p1": {}, "p2": {}, "p3": {"always_run": True}, "p4": {}}

    legacy, new = _both(tmp_path, workflow, behaviour={
        "p1": _failed("first"), "p3": _failed("second"),
    })

    _assert_parity(legacy, new)
    complete = next(r for r in _records(new[2])
                    if isinstance(r, dict) and r["record_type"] == "RUN_COMPLETE")
    assert complete["failed_step_id"] == "s1"


def test_a_raised_failure_matches_run_workflow(tmp_path: Path) -> None:
    def boom() -> Any:
        raise RuntimeError("load failed")

    _assert_parity(*_both(tmp_path, _fixture(), behaviour={"p2": boom}))


# --- parity: refusals that settle the run ------------------------------------


def _requiring_source() -> tuple[dict[str, Any], SimpleNamespace]:
    """A one-step workflow whose operation requires a setting the step lacks."""
    operation = ApplicationOperation(name="p1", contract_definition={
        "$schema": _SCHEMA, "type": "object",
        "properties": {"source": {"type": "string"}}, "required": ["source"],
    })
    ctx = SimpleNamespace(applications=(Application(
        name="fixture_app", workflow_operations=(operation,)),))
    workflow = {
        "name": "wf", "app": "fixture_app",
        "processes": {"p1": {"implementation": "p1"}},
        "steps": [{"id": "s1", "label": "One", "process": "p1"}],
    }
    return workflow, ctx


@pytest.mark.parametrize("apply", [True, False], ids=["apply", "dry_run"])
def test_the_contract_is_checked_before_release_like_run_workflow(apply: bool,
                                                                   tmp_path: Path) -> None:
    workflow, legacy_ctx = _requiring_source()
    if not apply:
        workflow["processes"]["p1"]["apply_only"] = True
    legacy_log, legacy_calls = _log(tmp_path / "legacy"), []
    with pytest.raises(LegacyWorkflowError) as legacy:
        run_workflow(legacy_ctx, legacy_log, workflow, _recording(legacy_calls), apply=apply)

    workflow, new_ctx = _requiring_source()
    if not apply:
        workflow["processes"]["p1"]["apply_only"] = True
    new_log, new_calls, control = _log(tmp_path / "new"), [], RecordingControl()
    graph = _build(workflow, new_ctx.applications[0], _recording(new_calls), control)
    with pytest.raises(AppError) as new:
        graph.run(parent_run(control), new_ctx, new_log, apply=apply)

    assert str(new.value) == str(legacy.value)
    assert new_calls == legacy_calls == []
    assert _records(new_log) == _records(legacy_log)
    # Refused before release: no child Run was created.
    assert control.rows == []


def test_a_disabled_workflow_is_a_refused_run_like_run_workflow(tmp_path: Path) -> None:
    workflow = _fixture()
    workflow["enabled"] = False

    legacy, new = _both(tmp_path, workflow)

    _assert_parity(legacy, new)
    assert new[1] == []


# --- child Runs, sequence and indent -----------------------------------------


def test_every_released_step_gets_one_child_run_under_the_parent(tmp_path: Path) -> None:
    control = RecordingControl()
    wf, ctx = prepared(_fixture(), SimpleNamespace())
    graph = _build(wf, ctx.applications[0], _recording([]), control)
    parent = parent_run(control, run_id=42)

    returned = graph.run(parent, ctx, _log(tmp_path), apply=False)

    assert returned is parent
    children = control.children(parent)
    assert [(c["subject_type"], c["subject_id"], c["subject_name"], c["app_name"], c["status"])
            for c in children] == [
        ("step", "s1", "One", "fixture_app", "SUCCEEDED"),
        ("step", "s2", "Two", "fixture_app", "SKIPPED"),
        ("step", "s3", "Three", "fixture_app", "SUCCEEDED"),
        ("step", "s4", "Four", "fixture_app", "SUCCEEDED"),
    ]
    assert all(c["parent_run_id"] == 42 for c in children)
    assert len({c["run_id"] for c in children}) == 4


def test_a_step_skipped_after_an_earlier_failure_gets_a_skipped_child_run(
    tmp_path: Path,
) -> None:
    control = RecordingControl()
    wf, ctx = prepared(_fixture(), SimpleNamespace())
    graph = _build(wf, ctx.applications[0], _recording([], {"p1": _failed("broke")}), control)
    parent = parent_run(control)

    graph.run(parent, ctx, _log(tmp_path))

    assert [c["status"] for c in control.children(parent)] == [
        "FAILED", "SKIPPED", "SKIPPED", "SKIPPED"]


def test_the_workflow_assigns_sequence_and_the_step_indent_level(tmp_path: Path) -> None:
    control = RecordingControl()
    run_log = _log(tmp_path)
    wf, ctx = prepared(_fixture(), SimpleNamespace())
    graph = _build(wf, ctx.applications[0], _recording([]), control)

    graph.run(parent_run(control), ctx, run_log, apply=False)

    lines = Path(run_log.path()).read_text(encoding="utf-8").splitlines()
    starts = [json.loads(line) for line in lines]
    starts = [r for r in starts if r["record_type"] == "STEP_START"]
    assert [(r["step_id"], r["step_sequence"]) for r in starts] == [
        ("s1", 1), ("s2", 2), ("s3", 3), ("s4", 4)]
    assert {r["nest_level"] for r in starts} == {5}


def test_a_child_runs_settings_are_its_steps_resolved_settings(tmp_path: Path) -> None:
    control = RecordingControl()
    application = Application(name="app", workflow_operations=(ApplicationOperation(
        name="load", contract_definition={"$schema": _SCHEMA, "type": "object",
                                          "properties": {"source": {}}}),))
    step = WorkflowStep(id="s1", process_name="load", label="Load", ordinal=1,
                        application=application, operation_name="load",
                        approved={"load": lambda *_a: None}, settings={"source": "a"})
    process = WorkflowProcess(name="app", ordinal=1, application=application, config=None,
                              steps=[step])

    Workflow(name="wf", enabled=True, tokens=None, config=None, processes=[process],
             control=control).run(parent_run(control), SimpleNamespace(), _log(tmp_path))

    assert control.rows[0]["settings"] == {"source": "a"}


# --- execution context and cross-step data ------------------------------------


def test_the_handler_receives_its_child_run_and_steps_share_the_parents_data(
    tmp_path: Path,
) -> None:
    control = RecordingControl()
    seen: list[Any] = []

    def export(_ctx: Any, _log: Any, _settings: dict[str, Any], run: Any) -> None:
        run.data["object_files"] = ["a.sql"]
        run.metadata["engine"] = "postgres"
        seen.append(run)

    def lint(_ctx: Any, _log: Any, _settings: dict[str, Any], run: Any) -> None:
        seen.append((run, run.data.get("object_files"), run.metadata.get("engine"),
                     run.metadata.get("workflow"), run.apply))

    application = Application(name="db", workflow_operations=tuple(
        ApplicationOperation(name=one, contract_definition={"$schema": _SCHEMA,
                                                             "type": "object"})
        for one in ("export", "lint")))
    steps = [
        WorkflowStep(id="export", process_name="export", label="export", ordinal=1,
                     application=application, operation_name="export",
                     approved={"export": export}, settings={}),
        WorkflowStep(id="lint", process_name="lint", label="lint", ordinal=2,
                     application=application, operation_name="lint",
                     approved={"lint": lint}, settings={}),
    ]
    process = WorkflowProcess(name="db", ordinal=1, application=application, config=None,
                              steps=steps)
    parent = parent_run(control)

    Workflow(name="wf", enabled=True, tokens=None, config=None, processes=[process],
             control=control).run(parent, SimpleNamespace(), _log(tmp_path),
                                  metadata={"workflow": "wf"})

    first, (second, files, engine, workflow_name, apply) = seen
    assert isinstance(first, Run) and isinstance(second, Run)
    assert first is not second and first is not parent
    assert first.parent_run_id == second.parent_run_id == parent.run_id
    assert (files, engine, workflow_name, apply) == (["a.sql"], "postgres", "wf", True)
    assert parent.data["object_files"] == ["a.sql"]
    assert parent.metadata["engine"] == "postgres"


# --- single use ----------------------------------------------------------------


def test_completed_steps_are_removed_from_the_workflow_and_its_processes(
    tmp_path: Path,
) -> None:
    control = RecordingControl()
    wf, ctx = prepared(_fixture(), SimpleNamespace())
    graph = _build(wf, ctx.applications[0], _recording([]), control)

    graph.run(parent_run(control), ctx, _log(tmp_path))

    assert graph.steps == {}
    assert graph.processes[0].steps == ()


def test_a_step_not_selected_stays_and_a_released_one_is_consumed(tmp_path: Path) -> None:
    control = RecordingControl()
    wf, ctx = prepared(_fixture(), SimpleNamespace())
    graph = _build(wf, ctx.applications[0], _recording([]), control)

    graph.run(parent_run(control), ctx, _log(tmp_path), step="s3")

    assert [one.id for one in graph.steps.values()] == ["s1", "s2", "s4"]
    assert [one.id for one in graph.processes[0].steps] == ["s1", "s2", "s4"]


def test_a_second_run_after_consumption_is_refused(tmp_path: Path) -> None:
    control = RecordingControl()
    wf, ctx = prepared(_fixture(), SimpleNamespace())
    graph = _build(wf, ctx.applications[0], _recording([]), control)
    graph.run(parent_run(control), ctx, _log(tmp_path / "first"))
    created = len(control.rows)

    with pytest.raises(AppError, match="single-use"):
        graph.run(parent_run(control), ctx, _log(tmp_path / "second"))
    assert len(control.rows) == created


# --- the object ----------------------------------------------------------------


def _application(*names: str, name: str = "app") -> Application:
    return Application(name=name, workflow_operations=tuple(
        ApplicationOperation(name=one, contract_definition={
            "$schema": _SCHEMA, "type": "object", "additionalProperties": False,
        }) for one in names))


def _step(application: Application, operation: str, step_id: str, ordinal: int,
          calls: list[str]) -> WorkflowStep:
    def handler(*_args: Any) -> None:
        calls.append(f"{application.name}.{operation}")
    return WorkflowStep(id=step_id, process_name=operation, label=step_id, ordinal=ordinal,
                        application=application, operation_name=operation,
                        approved={operation: handler}, settings={})


def test_steps_run_in_workflow_wide_ordinal_order_across_processes(tmp_path: Path) -> None:
    calls: list[str] = []
    control = RecordingControl()
    a, b = _application("load", name="a"), _application("check", name="b")
    first = WorkflowProcess(name="a", ordinal=1, application=a, config=None, steps=[
        _step(a, "load", "s1", 1, calls), _step(a, "load", "s3", 3, calls)])
    second = WorkflowProcess(name="b", ordinal=2, application=b, config=None, steps=[
        _step(b, "check", "s2", 2, calls)])
    parent = parent_run(control)

    Workflow(name="wf", enabled=True, tokens=None, config=None, processes=[first, second],
             control=control).run(parent, SimpleNamespace(), _log(tmp_path))

    assert calls == ["a.load", "b.check", "a.load"]
    assert [(c["subject_id"], c["app_name"]) for c in control.children(parent)] == [
        ("s1", "a"), ("s2", "b"), ("s3", "a")]


def test_it_holds_tokens_config_and_control_and_owns_its_processes() -> None:
    application = _application("load")
    process = WorkflowProcess(name="app", ordinal=1, application=application, config=None,
                              steps=[_step(application, "load", "s1", 1, [])])
    tokens, config = {"root": "/data"}, {"paths": [{"name": "data", "path": "{root}"}]}
    control = RecordingControl()

    workflow = Workflow(name="wf", enabled=True, tokens=tokens, config=config,
                        processes=[process], control=control)
    tokens["root"] = "/elsewhere"
    config["paths"].append({"name": "x", "path": "y"})

    assert workflow.tokens == {"root": "/data"}
    assert workflow.config == {"paths": [{"name": "data", "path": "{root}"}]}
    assert workflow.control is control
    assert process.workflow is workflow


def test_a_process_of_another_workflow_is_refused() -> None:
    application = _application("load")
    process = WorkflowProcess(name="app", ordinal=1, application=application, config=None,
                              steps=[])
    Workflow(name="first", enabled=True, tokens=None, config=None, processes=[process],
             control=None)

    with pytest.raises(AppError, match="already belongs to workflow 'first'"):
        Workflow(name="second", enabled=True, tokens=None, config=None,
                 processes=[process], control=None)


def test_a_shared_ordinal_is_refused() -> None:
    application = _application("load")
    process = WorkflowProcess(name="app", ordinal=1, application=application, config=None,
                              steps=[_step(application, "load", "s1", 1, []),
                                     _step(application, "load", "s2", 1, [])])

    with pytest.raises(AppError, match="share ordinal 1"):
        Workflow(name="wf", enabled=True, tokens=None, config=None, processes=[process],
                 control=None)


def test_the_superseded_workflow_result_does_not_exist() -> None:
    assert not hasattr(workflow_module, "WorkflowRun")
