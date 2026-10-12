"""Parity of the canonical workflow path with the legacy one (backlog 917).

The canonical path -- ``hydrate_workflow`` -> ``Workflow`` ->
``run_canonical_workflow`` -- is built beside the legacy dict path
(``run_workflow``) and replaces it only once this proves they do the same
thing. Each case runs one definition through both, each into its own run log,
and compares everything observable:

- the run's status and every step outcome;
- every handler call, with the exact configuration it received;
- the run metadata;
- the run-log records, without times and generated ids;
- for a refusal, the error's type and message.

Cases: every live workflow definition of every installation, run apply, dry-run,
single-step and as a range; and the coordinator mechanics -- tokens, overrides,
apply_only, always_run, failures, and every refusal.

One difference is accepted and is asserted as such: a step naming an undefined
process. The legacy path accepts the definition and fails the run; the
canonical one cannot represent the step and refuses at hydration (user,
2026-10-12). No live definition has one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

import pytest

from tests.conftest import make_run_log
from tests.support.workflow_publication import prepared
from tests.test_workflow_model import _live_entries

from rey_lib.config.applications import (
    Application,
    ApplicationCommand,
    ApplicationCommandParameter,
)
from rey_lib.workflow import (
    StepResult,
    WorkflowError,
    hydrate_workflow,
    run_canonical_workflow,
    run_workflow,
)

#: Record fields that differ between any two runs by construction.
_VOLATILE = ("time", "timestamp", "ts", "run_id", "record_id", "error_id", "duration",
             "elapsed", "started", "ended", "created", "_at", "pid", "host", "path", "file")


# ---------------------------------------------------------------------------
# Running one definition through both paths
# ---------------------------------------------------------------------------

def _recording_registry(implementations: list[str], behaviour: Mapping[str, Any]):
    """A handler per implementation that records its call and does as told."""
    calls: list[tuple[str, Any]] = []

    def handler_for(name: str) -> Callable[..., Any]:
        def handler(ctx: Any, run_log: Any, config: dict[str, Any], run: Any) -> Any:
            calls.append((name, json.loads(json.dumps(config, default=str))))
            outcome = behaviour.get(name)
            if isinstance(outcome, BaseException):
                # A fresh exception each time: a re-raised instance keeps the
                # frames of its earlier raise in its traceback.
                raise type(outcome)(*outcome.args)
            return outcome
        return handler

    return {name: handler_for(name) for name in implementations}, calls


def _records(folder: Path) -> list[dict[str, Any]]:
    """The run-log records written into ``folder``, volatile fields removed."""
    rows: list[dict[str, Any]] = []
    for log in sorted(folder.glob("*.jsonl")):
        for line in log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rows.append(_stable(json.loads(line)))
    return rows


def _stable(value: Any) -> Any:
    """``value`` with every volatile field removed, at any depth.

    A captured traceback names the frame it passed through, and the two paths
    are two functions at two lines: the location is normalised, never the text.
    """
    if isinstance(value, str):
        value = re.sub(r"line \d+", "line N", value)
        return value.replace("run_canonical_workflow", "run_workflow")
    if isinstance(value, dict):
        return {
            key: _stable(item) for key, item in value.items()
            if not any(part in key.lower() for part in _VOLATILE)
        }
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value


def _run(
    path: str,
    definition: dict[str, Any],
    ctx: Any,
    folder: Path,
    implementations: list[str],
    behaviour: Mapping[str, Any],
    **kwargs: Any,
) -> dict[str, Any]:
    """One definition through one path, everything observable collected."""
    folder.mkdir(parents=True)
    run_log = make_run_log(folder)
    registry, calls = _recording_registry(implementations, behaviour)
    try:
        if path == "legacy":
            run = run_workflow(ctx, run_log, definition, registry, **kwargs)
        else:
            workflow = hydrate_workflow(definition, installation="parity")
            run = run_canonical_workflow(
                ctx, run_log, workflow, registry,
                application=str(definition.get("app") or ""), **kwargs,
            )
    except WorkflowError as exc:
        return {"refused": (type(exc).__name__, str(exc)), "calls": calls,
                "records": _records(folder)}
    return {
        "status": run.status,
        "outcomes": [
            (o.id, o.label, o.process, o.status, o.detail, o.error) for o in run.outcomes
        ],
        "metadata": run.context.metadata if run.context is not None else None,
        "calls": calls,
        "records": _records(folder),
    }


def _assert_parity(
    tmp_path: Path,
    definition: dict[str, Any],
    ctx: Any,
    implementations: list[str],
    behaviour: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run both paths and require them to agree; return what they agreed on."""
    legacy = _run("legacy", definition, ctx, tmp_path / "legacy",
                  implementations, behaviour or {}, **kwargs)
    canonical = _run("canonical", definition, ctx, tmp_path / "canonical",
                     implementations, behaviour or {}, **kwargs)
    assert canonical == legacy
    return legacy


def _published(definition: dict[str, Any]) -> tuple[dict[str, Any], Any, list[str]]:
    """The definition with its operations published, and their names.

    ``prepared`` publishes one operation per process. Several processes may bind
    the same operation with different defaults -- the live loader workflow does
    -- so each operation is published once, declaring what every process that
    binds it configures.
    """
    workflow, ctx = prepared(definition)
    merged: dict[str, set[str]] = {}
    for operation in ctx.applications[0].workflow_operations:
        merged.setdefault(operation.name, set()).update(
            parameter.name for parameter in operation.parameters
        )
    ctx.applications = (Application(
        name=ctx.applications[0].name,
        workflow_operations=tuple(
            ApplicationCommand(name=name, parameters=tuple(
                ApplicationCommandParameter(name=one) for one in sorted(parameters)
            ))
            for name, parameters in merged.items()
        ),
    ),)
    return workflow, ctx, sorted(merged)


# ---------------------------------------------------------------------------
# Every live definition
# ---------------------------------------------------------------------------

def _live_cases() -> list[tuple[str, dict[str, Any]]]:
    """Each enabled live definition, named by installation and workflow."""
    return [
        (f"{installation}/{entry['name']}", entry)
        for installation, entry in _live_entries()
    ]


@pytest.mark.parametrize("apply", [True, False], ids=["apply", "dry_run"])
@pytest.mark.parametrize(("case", "entry"), _live_cases(), ids=[c for c, _e in _live_cases()])
def test_every_live_definition_runs_identically(
    tmp_path: Path, case: str, entry: dict[str, Any], apply: bool,
) -> None:
    workflow, ctx, implementations = _published(entry)
    agreed = _assert_parity(tmp_path, workflow, ctx, implementations, apply=apply)
    if entry.get("enabled", True) is not False and "refused" not in agreed:
        assert len(agreed["calls"]) + sum(
            1 for one in agreed.get("outcomes", []) if one[3] == "skipped"
        ) == len(entry.get("steps") or []), case


@pytest.mark.parametrize(("case", "entry"), _live_cases(), ids=[c for c, _e in _live_cases()])
def test_every_live_definition_selects_identically(
    tmp_path: Path, case: str, entry: dict[str, Any],
) -> None:
    steps = entry.get("steps") or []
    if entry.get("enabled", True) is False or not steps:
        pytest.skip(f"{case} has no steps to select")
    workflow, ctx, implementations = _published(entry)
    first, last = steps[0]["id"], steps[-1]["id"]
    _assert_parity(tmp_path / "one", workflow, ctx, implementations, step=first)
    _assert_parity(tmp_path / "range", workflow, ctx, implementations,
                   from_step=first, to_step=last)
    _assert_parity(tmp_path / "tail", workflow, ctx, implementations, from_step=last)


# ---------------------------------------------------------------------------
# Coordinator mechanics
# ---------------------------------------------------------------------------

_MECHANICS = {
    "name": "mechanics",
    "app": "fixture_app",
    "tokens": {"root": "/data", "inbox": "{root}/inbox"},
    "processes": {
        "copy": {"implementation": "copy_files",
                 "target": {"path": "{inbox}", "mode": "a"}, "retries": 3},
        "check": {"implementation": "check", "apply_only": True},
        "cleanup": {"implementation": "cleanup", "always_run": True},
    },
    "steps": [
        {"id": "first", "label": "First copy", "process": "copy"},
        {"id": "verify", "process": "check"},
        {"id": "second", "label": "Second copy", "process": "copy",
         "config": {"target": {"mode": "b"}, "retries": None}},
        {"id": "tidy", "process": "cleanup"},
    ],
}


@pytest.mark.parametrize("apply", [True, False], ids=["apply", "dry_run"])
def test_tokens_overrides_and_apply_only_agree(tmp_path: Path, apply: bool) -> None:
    workflow, ctx, implementations = _published(_MECHANICS)
    agreed = _assert_parity(tmp_path, workflow, ctx, implementations, apply=apply)
    assert ("copy_files", {"target": {"path": "/data/inbox", "mode": "b"},
                           "retries": None}) in agreed["calls"]


@pytest.mark.parametrize("behaviour", [
    {"copy_files": RuntimeError("boom")},
    {"copy_files": StepResult("copy_files", "failed", "said no")},
    {"copy_files": RuntimeError("boom"), "cleanup": RuntimeError("also")},
], ids=["raises", "returns_failed", "always_run_also_fails"])
def test_failures_and_always_run_agree(tmp_path: Path, behaviour: dict) -> None:
    workflow, ctx, implementations = _published(_MECHANICS)
    agreed = _assert_parity(tmp_path, workflow, ctx, implementations, behaviour)
    assert agreed["status"] == "failed"
    assert [name for name, _config in agreed["calls"]][-1] == "cleanup"


@pytest.mark.parametrize("selection", [
    {"step": "copy"}, {"step": "nope"}, {"step": "first", "to_step": "tidy"},
    {"from_step": "tidy", "to_step": "first"}, {"only": "verify"},
])
def test_selection_refusals_and_the_legacy_selector_agree(
    tmp_path: Path, selection: dict,
) -> None:
    workflow, ctx, implementations = _published(_MECHANICS)
    _assert_parity(tmp_path, workflow, ctx, implementations, **selection)


def _unprepared(processes: dict, steps: list, publish: tuple) -> tuple[dict, Any]:
    """A definition published exactly as given, for the refusals."""
    definition = {"name": "w", "app": "fixture_app", "processes": processes, "steps": steps}
    ctx = SimpleNamespace(applications=(Application(name="fixture_app", workflow_operations=publish),))
    return definition, ctx


@pytest.mark.parametrize(("processes", "publish", "implementations"), [
    ({"p": {}}, (), ["p"]),
    ({"p": {"implementation": "op"}}, (), ["op"]),
    ({"p": {"implementation": "op"}}, (ApplicationCommand(name="op"),), []),
    ({"p": {"implementation": "op", "extra": 1}}, (ApplicationCommand(name="op"),), ["op"]),
    ({"p": {"implementation": "op"}},
     (ApplicationCommand(name="op", parameters=(
         ApplicationCommandParameter(name="needed", required=True),)),), ["op"]),
], ids=["no_implementation", "unpublished", "unregistered", "undeclared_key", "missing_required"])
def test_pre_execution_refusals_agree(
    tmp_path: Path, processes: dict, publish: tuple, implementations: list,
) -> None:
    definition, ctx = _unprepared(processes, [{"id": "s", "process": "p"}], publish)
    agreed = _assert_parity(tmp_path, definition, ctx, implementations)
    assert "refused" in agreed


@pytest.mark.parametrize("definition", [
    {"name": "w", "enabled": False, "steps": [{"id": "s", "process": "nope"}]},
    {"name": "w", "processes": {}, "steps": [], "restore_mappings": []},
    {"name": "w", "processes": {}, "steps": [{"process": "p"}]},
    {"name": "w", "processes": {}, "steps": [{"id": "s"}]},
], ids=["disabled", "retired_key", "missing_id", "missing_process"])
def test_disabled_and_structural_refusals_agree(tmp_path: Path, definition: dict) -> None:
    ctx = SimpleNamespace(applications=())
    _assert_parity(tmp_path, definition, ctx, [])


def test_the_one_accepted_difference_an_undefined_process(tmp_path: Path) -> None:
    """Legacy fails the run; canonical cannot represent the step (2026-10-12)."""
    definition, ctx = _unprepared({}, [{"id": "s", "process": "nope"}], ())
    legacy = _run("legacy", definition, ctx, tmp_path / "legacy", [], {})
    canonical = _run("canonical", definition, ctx, tmp_path / "canonical", [], {})

    assert "undefined process" in legacy["refused"][1]
    assert any(r.get("record_type") == "RUN_COMPLETE" for r in legacy["records"])
    assert "undefined process" in canonical["refused"][1]
    assert canonical["records"] == [], "nothing started"
