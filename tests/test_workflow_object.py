"""Tests for Workflow, the orchestrator of a hydrated graph (backlog 950).

``Workflow.direct(step, from_step, to_step)`` releases each eligible step, checking its
contract first, assigns its sequence and indent, and creates one child Run for
it under the parent Run (a skipped step gets one too). The child Run executes
the step and finishes itself; the step stays in the graph and holds that Run's
outcome as its state, and ``run`` returns the Workflow Run. The parity tests run one fixture
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
#: Where the contract departs from the legacy engine on purpose (backlog 964): a
#: step's sequence is its ordinal, not a counter over the selection. Asserted on
#: its own, left out of the parity comparison.
_ORDINAL_SEQUENCE = frozenset({"step_sequence", "failed_step_sequence"})
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
        def handler(_ctx: Any, _run_log: Any, _settings: dict[str, Any], _run: Any,
                    _step: Any = None) -> Any:
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
                    config=None, processes=[process], control=control)


def _run(graph: Workflow, parent: Any, ctx: Any, **selection: Any) -> Any:
    """Direct ``graph`` as hydration would hold it: with ``parent``'s log and run id
    -- the creating Application Run's -- in ``ctx``."""
    graph.log, graph.runtime, graph.run_id = parent.log, ctx, parent.run_id
    return graph.direct(**selection)


def _both(tmp_path: Path, workflow: dict[str, Any], *,
          behaviour: dict[str, Callable[[], Any]] | None = None, **selection: Any
          ) -> tuple[Any, Any]:
    """Run ``workflow`` on both paths.

    Returns (legacy run, legacy calls, legacy log) and (outcome, new calls,
    new log, control, Application Run).
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
    application_run = parent_run(control, log=new_log)
    returned = _run(graph, application_run, new_ctx, **selection)
    return ((legacy_run, legacy_calls, legacy_log),
            (returned, new_calls, new_log, control, application_run))


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
        record = {key: value for key, value in record.items()
                  if key not in _VOLATILE | _ORDINAL_SEQUENCE}
        if isinstance(record.get("error_message"), dict):
            record["error_message"] = {
                key: value for key, value in record["error_message"].items()
                if key not in _VOLATILE_ERROR | _ORDINAL_SEQUENCE
            }
        stable.append(record)
    return stable


def _assert_parity(legacy: Any, new: Any) -> None:
    """Same call order, step outcomes and run-log records on both paths."""
    (legacy_run, legacy_calls, legacy_log), (returned, new_calls, new_log, control,
                                             application_run) = legacy, new
    assert returned in ("SUCCEEDED", "FAILED")
    # Only Step Runs are created, each a child of the Application Run.
    assert [row["subject_type"] for row in control.rows] == ["step"] * len(control.rows)
    assert new_calls == legacy_calls
    assert [(row["subject_id"], row["status"]) for row in control.children(application_run)] == [
        (outcome.id, _STATUS[outcome.status]) for outcome in legacy_run.outcomes
    ]
    assert _records(new_log) == _through_run_complete(_records(legacy_log))


def _through_run_complete(records: list[Any]) -> list[Any]:
    """The records up to and including RUN_COMPLETE.

    The legacy engine finalizes the run log itself (RESULTS_SUMMARY onward); the
    Workflow leaves that to the app's common shutdown (backlog 964).
    """
    for index, record in enumerate(records):
        if isinstance(record, dict) and record["record_type"] == "RUN_COMPLETE":
            return records[:index + 1]
    return records


def _failed(detail: str) -> Callable[[], Any]:
    return lambda: SimpleNamespace(status="failed", detail=detail)


# --- parity: selection, ordering, dry-run -----------------------------------


@pytest.mark.parametrize("selection", [
    {},
    {"step": "s3"},
    {"step": "Three"},
    {"step": "p3"},
    {"from_step": "s2"},
    {"to_step": "s3"},
    {"from_step": "s2", "to_step": "s3"},
], ids=["full", "step_by_id", "step_by_label", "step_by_process", "from_step",
        "to_step", "range"])
def test_selection_matches_run_workflow(selection: dict[str, Any], tmp_path: Path) -> None:
    _assert_parity(*_both(tmp_path, _fixture(), **selection))


def _step_ends(run_log: Any) -> list[tuple[str, str, str]]:
    """Each STEP_END as (step_id, status, message)."""
    records = [json.loads(line) for line in
               Path(run_log.path()).read_text(encoding="utf-8").splitlines()]
    return [(r["step_id"], r["status"], r.get("message") or "")
            for r in records if r["record_type"] == "STEP_END"]


@pytest.mark.parametrize("selection", [{}, {"from_step": "s1", "to_step": "s3"}],
                         ids=["dry_run", "dry_run_range"])
def test_a_dry_run_does_not_release_apply_only_steps(selection: dict[str, Any],
                                                     tmp_path: Path) -> None:
    """Backlog 964: apply_only in a dry run is a terminal rule -- NOT RUN, not SKIPPED."""
    calls: list[str] = []
    control, run_log = RecordingControl(), _log(tmp_path)
    wf, ctx = prepared(_fixture(), SimpleNamespace())
    graph = _build(wf, ctx.applications[0], _recording(calls), control)

    application_run = parent_run(control, log=run_log)
    _run(graph, application_run, ctx, apply=False, **selection)

    assert "p2" not in calls
    assert ("s2", "NOT RUN") in [(c["subject_id"], c["status"])
                                 for c in control.children(application_run)]
    assert ("s2", "not run", "not released") in _step_ends(run_log)


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
        _run(graph, parent_run(control, log=_log(tmp_path / "new")), new_ctx, **selection)

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
        _run(_build(new_wf, new_ctx.applications[0], _recording([]), control),
             parent_run(control, log=_log(tmp_path / "new")), new_ctx, step="One")

    assert "ambiguous" in str(new.value)
    assert str(new.value) == str(legacy.value)


# --- failure progression --------------------------------------------------------
#
# The same steps run as on the legacy engine; a step an earlier failure holds back
# is NOT RUN under its own Run (backlog 964), where the legacy engine skipped it.


def _statuses(new: Any) -> list[tuple[str, str]]:
    """(step id, terminal status) of every Step Run under the Application Run."""
    _returned, _calls, _log, control, application_run = new
    return [(c["subject_id"], c["status"]) for c in control.children(application_run)]


def test_a_failure_holds_ordinary_steps_back_and_always_run_still_runs(tmp_path: Path) -> None:
    workflow = _fixture()
    workflow["processes"] = {"p1": {}, "p2": {}, "p3": {"always_run": True}, "p4": {}}

    legacy, new = _both(tmp_path, workflow, behaviour={"p1": _failed("p1 broke")})

    assert new[1] == legacy[1] == ["p1", "p3"]
    assert _statuses(new) == [("s1", "FAILED"), ("s2", "NOT RUN"), ("s3", "SUCCEEDED"),
                              ("s4", "NOT RUN")]
    assert ("s2", "not run", "not released") in _step_ends(new[2])


def test_the_first_failure_is_retained_when_an_always_run_step_also_fails(
    tmp_path: Path,
) -> None:
    workflow = _fixture()
    workflow["processes"] = {"p1": {}, "p2": {}, "p3": {"always_run": True}, "p4": {}}

    legacy, new = _both(tmp_path, workflow, behaviour={
        "p1": _failed("first"), "p3": _failed("second"),
    })

    assert new[1] == legacy[1]
    assert _statuses(new) == [("s1", "FAILED"), ("s2", "NOT RUN"), ("s3", "FAILED"),
                              ("s4", "NOT RUN")]
    complete = next(r for r in _records(new[2])
                    if isinstance(r, dict) and r["record_type"] == "RUN_COMPLETE")
    assert complete["failed_step_id"] == "s1"
    assert new[0] == "FAILED"


def test_a_raised_failure_holds_the_rest_back(tmp_path: Path) -> None:
    def boom() -> Any:
        raise RuntimeError("load failed")

    legacy, new = _both(tmp_path, _fixture(), behaviour={"p2": boom})

    assert new[1] == legacy[1] == ["p1", "p2"]
    assert _statuses(new) == [("s1", "SUCCEEDED"), ("s2", "FAILED"), ("s3", "NOT RUN"),
                              ("s4", "NOT RUN")]


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


def test_a_released_steps_contract_refusal_creates_no_step_run(tmp_path: Path) -> None:
    """The released step checks its own contract (backlog 964) and refuses before
    its Run exists; the Workflow records the refusal. No Run of any kind is created.

    The refusal propagates as the step's own AppError (#1006): not relabelled,
    not replaced, and recorded through the canonical evidence path."""
    workflow, ctx = _requiring_source()
    run_log, calls, control = _log(tmp_path), [], RecordingControl()
    graph = _build(workflow, ctx.applications[0], _recording(calls), control)
    with pytest.raises(AppError) as raised:
        _run(graph, parent_run(control, log=run_log), ctx)

    error = raised.value
    # The step's own refusal: raised in WorkflowStep.run, not a replacement chained to it.
    assert type(error) is AppError and error.__cause__ is None
    assert str(error) == graph.steps["s1"].contract_refusal("wf")
    assert raised.traceback[-1].name == "run"
    assert calls == []
    # No Step Run was created, and there is no Workflow Run.
    assert control.rows == []

    records = [json.loads(line) for line in
               Path(run_log.path()).read_text(encoding="utf-8").splitlines()]
    failure = next(r for r in records if r["record_type"] == "STEP_FAILURE")
    complete = next(r for r in records if r["record_type"] == "RUN_COMPLETE")
    evidence = failure["error_message"]
    # The error's own type and traceback, not Workflow-made "WorkflowError" evidence.
    assert evidence["error_type"] == "AppError"
    assert evidence.get("sanitized_traceback")
    assert complete["status"] == "failed"
    assert complete["failure_record_id"] == evidence["failure_record_id"]


def test_an_unreleased_step_is_never_contract_checked(tmp_path: Path) -> None:
    """A dry-run apply_only step is not released, so its contract is not checked."""
    workflow, ctx = _requiring_source()
    workflow["processes"]["p1"]["apply_only"] = True
    control = RecordingControl()
    graph = _build(workflow, ctx.applications[0], _recording([]), control)

    application_run = parent_run(control, log=_log(tmp_path))
    _run(graph, application_run, ctx, apply=False)

    assert ("s1", "NOT RUN") in [(c["subject_id"], c["status"])
                                 for c in control.children(application_run)]


def test_a_disabled_workflow_is_a_refused_run_like_run_workflow(tmp_path: Path) -> None:
    workflow = _fixture()
    workflow["enabled"] = False

    (legacy_run, legacy_calls, legacy_log), (returned, new_calls, new_log, control, _app) = _both(
        tmp_path, workflow)

    # Refused: no Run is created, nothing is released, the records match.
    assert returned is None and control.rows == []
    assert new_calls == legacy_calls == []
    assert _records(new_log) == _through_run_complete(_records(legacy_log))


# --- child Runs, sequence and indent -----------------------------------------


def test_the_only_runs_are_step_runs_under_the_application_run(
    tmp_path: Path,
) -> None:
    """Application Run -> Step Runs (backlog 1003): the Workflow creates no Run."""
    control = RecordingControl()
    wf, ctx = prepared(_fixture(), SimpleNamespace())
    graph = _build(wf, ctx.applications[0], _recording([]), control)
    parent = parent_run(control, run_id=42, log=_log(tmp_path))

    returned = _run(graph, parent, ctx, apply=False)

    assert returned == "SUCCEEDED"
    # Every Run recorded is a Step Run, and every one is a child of the Application Run.
    assert [row["subject_type"] for row in control.rows] == ["step"] * 4
    children = control.children(parent)
    assert len(children) == len(control.rows)
    assert [(c["subject_type"], c["subject_id"], c["subject_name"], c["app_name"], c["status"])
            for c in children] == [
        ("step", "s1", "One", "fixture_app", "SUCCEEDED"),
        ("step", "s2", "Two", "fixture_app", "NOT RUN"),
        ("step", "s3", "Three", "fixture_app", "SUCCEEDED"),
        ("step", "s4", "Four", "fixture_app", "SUCCEEDED"),
    ]
    assert len({c["run_id"] for c in children}) == 4


def test_a_step_held_back_by_an_earlier_failure_is_not_run(tmp_path: Path) -> None:
    control = RecordingControl()
    wf, ctx = prepared(_fixture(), SimpleNamespace())
    graph = _build(wf, ctx.applications[0], _recording([], {"p1": _failed("broke")}), control)

    application_run = parent_run(control, log=_log(tmp_path))
    returned = _run(graph, application_run, ctx)

    assert [c["status"] for c in control.children(application_run)] == [
        "FAILED", "NOT RUN", "NOT RUN", "NOT RUN"]
    assert returned == "FAILED"


def test_a_step_sequence_is_its_ordinal_within_a_selection(tmp_path: Path) -> None:
    control, run_log = RecordingControl(), _log(tmp_path)
    wf, ctx = prepared(_fixture(), SimpleNamespace())
    graph = _build(wf, ctx.applications[0], _recording([]), control)

    _run(graph, parent_run(control, log=run_log), ctx, from_step="s3")

    starts = [json.loads(line) for line in
              Path(run_log.path()).read_text(encoding="utf-8").splitlines()]
    assert [(r["step_id"], r["step_sequence"]) for r in starts
            if r["record_type"] == "STEP_START"] == [("s3", 3), ("s4", 4)]


def test_the_workflow_assigns_sequence_and_the_step_indent_level(tmp_path: Path) -> None:
    control = RecordingControl()
    run_log = _log(tmp_path)
    wf, ctx = prepared(_fixture(), SimpleNamespace())
    graph = _build(wf, ctx.applications[0], _recording([]), control)

    _run(graph, parent_run(control, log=run_log), ctx, apply=False)

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

    Workflow(name="wf", enabled=True, config=None, processes=[process],
             control=control, runtime=SimpleNamespace(),
             log=_log(tmp_path)).direct()

    # The Step Run is the only Run.
    assert [row["settings"] for row in control.rows] == [{"source": "a"}]


# --- execution context and cross-step data ------------------------------------


def test_the_handler_receives_its_run_and_steps_share_the_directions_data(
    tmp_path: Path,
) -> None:
    control = RecordingControl()
    seen: list[Any] = []

    def export(_ctx: Any, _log: Any, _settings: dict[str, Any], run: Any, _step: Any) -> None:
        run.data["object_files"] = ["a.sql"]
        run.metadata["engine"] = "postgres"
        seen.append(run)

    def lint(_ctx: Any, _log: Any, _settings: dict[str, Any], run: Any, _step: Any) -> None:
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
    parent = parent_run(control, log=_log(tmp_path))

    workflow = Workflow(name="wf", enabled=True, config=None,
                        processes=[process], control=control,
                        runtime=SimpleNamespace(), log=parent.log, run_id=parent.run_id)
    outcome = workflow.direct(metadata={"workflow": "wf"})

    first, (second, files, engine, workflow_name, apply) = seen
    assert outcome == "SUCCEEDED"
    assert isinstance(first, Run) and isinstance(second, Run) and first is not second
    # The Step Runs are children of the Application Run, and share the direction's
    # working data and metadata -- not the Application Run's, which is untouched.
    assert first.parent_run_id == second.parent_run_id == parent.run_id
    assert (files, engine, workflow_name, apply) == (["a.sql"], "postgres", "wf", True)
    assert first.data is second.data and first.metadata is second.metadata
    assert parent.data == {} and parent.metadata == {}
    assert [row["subject_type"] for row in control.rows] == ["step", "step"]

    seen.clear()
    workflow.direct(step="lint")

    # A later direction starts with fresh data and metadata.
    (_third, files, engine, workflow_name, _apply), = seen
    assert (files, engine, workflow_name) == (None, None, None)


# --- reusable, stateful, observable ---------------------------------------------


class _Watching:
    """An observer of a Workflow: what it and its steps were when each change was told."""

    def __init__(self, graph: Workflow) -> None:
        self.graph = graph
        self.seen: list[tuple[str, tuple[str, ...]]] = []
        graph.observe(self)

    def changed(self, workflow: Workflow) -> None:
        assert workflow is self.graph
        self.seen.append((workflow.state, tuple(one.state for one in workflow.steps.values())))


def test_steps_stay_in_the_workflow_and_its_processes_after_a_run(tmp_path: Path) -> None:
    control = RecordingControl()
    wf, ctx = prepared(_fixture(), SimpleNamespace())
    graph = _build(wf, ctx.applications[0], _recording([]), control)

    _run(graph, parent_run(control, log=_log(tmp_path)), ctx)

    assert list(graph.steps) == ["s1", "s2", "s3", "s4"]
    assert [one.id for one in graph.processes[0].steps] == ["s1", "s2", "s3", "s4"]


def test_a_released_step_holds_its_runs_outcome_and_an_unreleased_one_its_state(
    tmp_path: Path,
) -> None:
    control = RecordingControl()
    wf, ctx = prepared(_fixture(), SimpleNamespace())
    graph = _build(wf, ctx.applications[0], _recording([]), control)

    application_run = parent_run(control, log=_log(tmp_path))
    _run(graph, application_run, ctx, step="s3")

    assert {key: one.state for key, one in graph.steps.items()} == {
        "s1": "PENDING", "s2": "PENDING", "s3": "SUCCEEDED", "s4": "PENDING"}
    assert graph.steps["s3"].current_run.parent_run_id == application_run.run_id
    assert graph.state == "IDLE"


def test_the_workflow_is_reusable(tmp_path: Path) -> None:
    calls: list[str] = []
    control = RecordingControl()
    wf, ctx = prepared(_fixture(), SimpleNamespace())
    graph = _build(wf, ctx.applications[0], _recording(calls), control)
    _run(graph, parent_run(control, log=_log(tmp_path / "first")), ctx, step="s1")
    first = graph.steps["s1"].current_run

    _run(graph, parent_run(control, log=_log(tmp_path / "second")), ctx, step="s1")

    assert calls == ["p1", "p1"]
    assert graph.steps["s1"].current_run is not first
    assert graph.steps["s1"].state == "SUCCEEDED"


def test_each_step_is_running_while_its_run_executes_and_the_observer_is_told(
    tmp_path: Path,
) -> None:
    control = RecordingControl()
    wf, ctx = prepared(_fixture(), SimpleNamespace())
    graph = _build(wf, ctx.applications[0], _recording([]), control)
    watching = _Watching(graph)

    _run(graph, parent_run(control, log=_log(tmp_path)), ctx, from_step="s3")

    assert watching.seen == [
        ("RUNNING", ("PENDING", "PENDING", "PENDING", "PENDING")),
        ("RUNNING", ("PENDING", "PENDING", "RUNNING", "PENDING")),
        ("RUNNING", ("PENDING", "PENDING", "SUCCEEDED", "PENDING")),
        ("RUNNING", ("PENDING", "PENDING", "SUCCEEDED", "RUNNING")),
        ("RUNNING", ("PENDING", "PENDING", "SUCCEEDED", "SUCCEEDED")),
        ("IDLE", ("PENDING", "PENDING", "SUCCEEDED", "SUCCEEDED")),
    ]


def test_a_failed_step_holds_failed(tmp_path: Path) -> None:
    control = RecordingControl()
    wf, ctx = prepared(_fixture(), SimpleNamespace())
    graph = _build(wf, ctx.applications[0],
                   _recording([], {"p1": lambda: SimpleNamespace(status="failed")}), control)

    _run(graph, parent_run(control, log=_log(tmp_path)), ctx, step="s1")

    assert graph.steps["s1"].state == "FAILED"


def test_a_run_while_running_is_refused(tmp_path: Path) -> None:
    control = RecordingControl()
    wf, ctx = prepared(_fixture(), SimpleNamespace())
    refused: list[str] = []
    graph: Workflow

    def again() -> None:
        with pytest.raises(AppError, match="already running") as raised:
            _run(graph, parent_run(control, log=_log(tmp_path / "second")), ctx)
        refused.append(str(raised.value))

    graph = _build(wf, ctx.applications[0], _recording([], {"p1": again}), control)
    _run(graph, parent_run(control, log=_log(tmp_path / "first")), ctx, step="s1")

    assert len(refused) == 1
    assert graph.state == "IDLE"


def test_the_projection_carries_each_steps_state_and_run(tmp_path: Path) -> None:
    control = RecordingControl()
    wf, ctx = prepared(_fixture(), SimpleNamespace())
    graph = _build(wf, ctx.applications[0], _recording([]), control)
    _run(graph, parent_run(control, log=_log(tmp_path)), ctx, step="s1")

    steps = graph.projection(ctx)["processes"][0]["steps"]

    assert [(one["id"], one["state"]) for one in steps] == [
        ("s1", "SUCCEEDED"), ("s2", "PENDING"), ("s3", "PENDING"), ("s4", "PENDING")]
    assert steps[0]["run_id"] == graph.steps["s1"].current_run.run_id
    assert steps[1]["run_id"] is None


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
    parent = parent_run(control, log=_log(tmp_path))

    Workflow(name="wf", enabled=True, config=None,
             processes=[first, second], control=control,
             runtime=SimpleNamespace(), log=parent.log, run_id=parent.run_id).direct()

    assert calls == ["a.load", "b.check", "a.load"]
    assert [(c["subject_id"], c["app_name"]) for c in control.children(parent)] == [
        ("s1", "a"), ("s2", "b"), ("s3", "a")]


def test_it_holds_config_and_control_and_owns_its_processes() -> None:
    application = _application("load")
    process = WorkflowProcess(name="app", ordinal=1, application=application, config=None,
                              steps=[_step(application, "load", "s1", 1, [])])
    config = {"paths": [{"name": "data", "path": "{root}"}]}
    control = RecordingControl()

    workflow = Workflow(name="wf", enabled=True, config=config,
                        processes=[process], control=control)
    config["paths"].append({"name": "x", "path": "y"})

    assert workflow.config == {"paths": [{"name": "data", "path": "{root}"}]}
    assert workflow.control is control
    assert process.workflow is workflow


def test_a_process_of_another_workflow_is_refused() -> None:
    application = _application("load")
    process = WorkflowProcess(name="app", ordinal=1, application=application, config=None,
                              steps=[])
    Workflow(name="first", enabled=True, config=None, processes=[process],
             control=None)

    with pytest.raises(AppError, match="already belongs to workflow 'first'"):
        Workflow(name="second", enabled=True, config=None,
                 processes=[process], control=None)


def test_a_shared_ordinal_is_refused() -> None:
    application = _application("load")
    process = WorkflowProcess(name="app", ordinal=1, application=application, config=None,
                              steps=[_step(application, "load", "s1", 1, []),
                                     _step(application, "load", "s2", 1, [])])

    with pytest.raises(AppError, match="share ordinal 1"):
        Workflow(name="wf", enabled=True, config=None, processes=[process],
                 control=None)


def test_the_superseded_workflow_result_does_not_exist() -> None:
    assert not hasattr(workflow_module, "WorkflowRun")


@pytest.mark.parametrize(("selection", "expected"), [
    ({"step": "a"}, ["s1", "s2", "s3"]),
    ({"from_step": "b"}, ["s2", "s3"]),
    ({"to_step": "a"}, ["s1", "s2", "s3"]),
    ({"to_step": "b"}, ["s1", "s2"]),
    ({"step": "s2"}, ["s2"]),
])
def test_a_process_name_selects_its_own_steps_in_release_order(
    tmp_path: Path, selection: dict[str, str], expected: list[str],
) -> None:
    """A process action reaches the Workflow as intent; the Workflow resolves it."""
    control = RecordingControl()
    a, b = _application("load", name="a"), _application("check", name="b")
    first = WorkflowProcess(name="a", ordinal=1, application=a, config=None, steps=[
        _step(a, "load", "s1", 1, []), _step(a, "load", "s3", 3, [])])
    second = WorkflowProcess(name="b", ordinal=2, application=b, config=None, steps=[
        _step(b, "check", "s2", 2, [])])
    workflow = Workflow(name="wf", enabled=True, config=None,
                        processes=[first, second], control=control,
                        runtime=SimpleNamespace(),
                        log=_log(tmp_path))

    workflow.direct(**selection)

    assert [row["subject_id"] for row in control.rows
            if control.finished[row["run_id"]] == "SUCCEEDED"] == expected



def test_the_projection_states_the_released_step_and_its_run(tmp_path: Path) -> None:
    """The Workflow owns release, so it states which step it released, and that step's Run."""
    control = RecordingControl()
    wf, ctx = prepared(_fixture(), SimpleNamespace())
    graph = _build(wf, ctx.applications[0], _recording([]), control)
    assert graph.projection(ctx)["released"] is None
    seen: list[Any] = []

    class _Watching:
        def changed(self, workflow: Workflow) -> None:
            seen.append(workflow.projection(ctx)["released"])

    graph.observe(_Watching())
    _run(graph, parent_run(control, log=_log(tmp_path)), ctx, from_step="s3")

    running = [one for one in seen if one is not None]
    assert [one["step"] for one in running][0] == "s3"
    assert running[-1] == {"step": "s4", "run_id": graph.steps["s4"].current_run.run_id}
    assert graph.projection(ctx)["released"]["step"] == "s4"


@pytest.mark.parametrize("failing_state", ["RUNNING", "IDLE"])
def test_observer_failure_releases_lock_and_allows_another_direction(
    tmp_path: Path, failing_state: str,
) -> None:
    control = RecordingControl()
    wf, ctx = prepared(_fixture(), SimpleNamespace())
    calls: list[str] = []
    graph = _build(wf, ctx.applications[0], _recording(calls), control)
    failure = RuntimeError("observer failed")

    class FailOnce:
        failed = False

        def changed(self, workflow: Workflow) -> None:
            if workflow.state == failing_state and not self.failed:
                self.failed = True
                raise failure

    graph.observe(FailOnce())
    with pytest.raises(RuntimeError) as raised:
        _run(graph, parent_run(control, log=_log(tmp_path / "first")), ctx, step="s1")
    assert raised.value is failure
    assert graph.state == "IDLE"
    assert calls == ([] if failing_state == "RUNNING" else ["p1"])

    result = _run(graph, parent_run(control, log=_log(tmp_path / "second")), ctx, step="s2")
    assert result == "SUCCEEDED"
    assert calls[-1] == "p2"
    assert graph.state == "IDLE"


@pytest.mark.parametrize("earlier_failure", [False, True], ids=["dry_run", "earlier_failure"])
def test_non_release_replaces_previous_step_outcome_and_notifies_projection(
    tmp_path: Path, earlier_failure: bool,
) -> None:
    control = RecordingControl()
    wf, ctx = prepared(_fixture(), SimpleNamespace())
    calls: list[str] = []
    behaviour = {"p1": _failed("broke")} if earlier_failure else {}
    graph = _build(wf, ctx.applications[0], _recording(calls, behaviour), control)
    _run(graph, parent_run(control, log=_log(tmp_path / "first")), ctx, step="s2")
    previous = graph.steps["s2"].current_run
    assert graph.steps["s2"].state == "SUCCEEDED"
    calls.clear()
    seen = []

    class Watching:
        def changed(self, workflow: Workflow) -> None:
            seen.extend(step for process in workflow.projection(ctx)["processes"]
                        for step in process["steps"] if step["id"] == "s2")

    graph.observe(Watching())
    log = _log(tmp_path / "second")
    application_run = parent_run(control, log=log)
    _run(graph, application_run, ctx, apply=earlier_failure)
    step = graph.steps["s2"]
    assert "p2" not in calls
    assert step.current_run is not previous
    assert step.current_run.parent_run_id == application_run.run_id
    assert step.current_run.status == step.state == "NOT RUN"
    assert step.current_run.detail == "not released"
    assert ("s2", "not run", "not released") in _step_ends(log)
    assert any(item["state"] == "NOT RUN" and item["run_id"] == step.current_run.run_id
               for item in seen)


def test_relationships_follow_execution_order_across_interleaved_processes() -> None:
    application = _application("load")
    first = WorkflowProcess(name="a", ordinal=1, application=application, config=None,
                            steps=[_step(application, "load", "s3", 3, []),
                                   _step(application, "load", "s1", 1, [])])
    second = WorkflowProcess(name="b", ordinal=2, application=application, config=None,
                             steps=[_step(application, "load", "s2", 2, [])])
    workflow = Workflow(name="wf", enabled=True, config=None,
                        processes=[first, second], control=None)
    relationships = workflow.projection(None)["relationships"]
    assert [edge for edge in relationships if edge["kind"] == "precedes"] == [
        {"source": "step:1", "target": "step:2", "kind": "precedes"},
        {"source": "step:2", "target": "step:3", "kind": "precedes"},
    ]
    assert {"source": "process:1", "target": "step:3", "kind": "contains"} in relationships
    assert {"source": "process:2", "target": "step:2", "kind": "contains"} in relationships
