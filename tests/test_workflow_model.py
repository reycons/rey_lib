"""The canonical workflow objects and their one hydration boundary (backlog 917).

Workflow -> WorkflowProcess -> WorkflowStep, built by ``hydrate_workflow`` from
the entry the configuration load resolved. Two halves:

- the live workflow definitions of every installation, merged by the loader's
  own functions exactly as the config load merges them, hydrate into objects
  that say what the definitions say;
- the behaviour that moved onto the objects -- defaults and overrides, tokens,
  apply_only / always_run, execution defaults, selection -- answers as the
  coordinator answered before it moved.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.config.applications import Application, ApplicationCommand
from rey_lib.config.config_context import _stamp_workflow_ownership
from rey_lib.config.config_loader import _deep_merge, _load_yaml, _yaml_files_in_folder
from rey_lib.config.config_namespace import Namespace
from rey_lib.workflow import (
    Workflow,
    WorkflowError,
    WorkflowProcess,
    WorkflowStep,
    hydrate_workflow,
    run_canonical_workflow,
)

_INSTALLATIONS = Path("/workspaces/installations")

#: Every installation's workflow folders, as each installation declares them.
#: Pipeline folders are included: a pipeline-local workflow (rey_analyzer's
#: generate_* workflows) is a workflow like any other.
_WORKFLOW_FOLDERS = (
    ("local", _INSTALLATIONS / "local" / "work" / "workflows"),
    ("local", _INSTALLATIONS / "local" / "work" / "pipelines"),
    ("wolff_popper", _INSTALLATIONS / "wolff_popper" / "config" / "work" / "workflows"),
    ("wolff_popper", _INSTALLATIONS / "wolff_popper" / "config" / "work" / "pipelines"),
    ("admin", _INSTALLATIONS / "admin" / "work" / "workflows"),
)


def _merged_entries(folder: Path) -> list[dict[str, Any]]:
    """The resolved workflow entries of one folder, merged as the config load does."""
    raw: dict[str, Any] = {}
    for one in _yaml_files_in_folder(folder, folder / "none"):
        raw = _deep_merge(raw, _stamp_workflow_ownership(_load_yaml(one)))
    return [entry for entry in raw.get("workflows") or [] if isinstance(entry, dict)]


def _live_entries() -> list[tuple[str, dict[str, Any]]]:
    """Every live workflow entry, with its installation."""
    found = [
        (installation, entry)
        for installation, root in _WORKFLOW_FOLDERS if root.is_dir()
        for child in sorted(root.iterdir()) if child.is_dir()
        for entry in _merged_entries(child)
    ]
    if not found:
        pytest.skip("no installation workflow definitions are present")
    return found


# ---------------------------------------------------------------------------
# Live definitions hydrate into objects that say what the files say
# ---------------------------------------------------------------------------

def test_every_live_workflow_hydrates_and_says_what_its_definition_says() -> None:
    for installation, entry in _live_entries():
        workflow = hydrate_workflow(entry, installation=installation)
        name = entry["name"]
        assert workflow.name == name and workflow.installation == installation
        assert not hasattr(workflow, "app"), "a workflow holds no application"
        if entry.get("enabled", True) is False:
            assert workflow.processes == ()
            continue

        declared = entry.get("processes") or {}
        assert [one.name for one in workflow.processes] == list(declared)
        for process in workflow.processes:
            defaults = declared[process.name] or {}
            assert process.application == entry["app"]
            assert process.operation == str(defaults.get("implementation") or "")
            assert dict(process.defaults) == defaults

        steps = entry.get("steps") or []
        ordered = workflow.ordered_steps()
        assert [step.key for _process, step in ordered] == [one["id"] for one in steps]
        assert [step.ordinal for _process, step in ordered] == list(range(1, len(steps) + 1))
        for (process, step), source in zip(ordered, steps):
            assert process.name == source["process"]
            assert step in process.steps, "a step is a child of its own process"
            assert step.label == (source.get("label") or source["id"])
            assert dict(step.overrides) == (source.get("config") or {})
        assert dict(workflow.tokens) == (entry.get("tokens") or {})
        declared_contract = entry.get("execution") or {}
        assert dict(workflow.execution) == {
            "full": bool(declared_contract.get("full", True)),
            "dry_run": bool(declared_contract.get("dry_run", True)),
            "step": bool(declared_contract.get("step", False)),
            "range": bool(declared_contract.get("range", False)),
        }


def test_no_live_step_names_an_undefined_process() -> None:
    """The measurement behind refusing at hydration (2026-10-12): zero."""
    for installation, entry in _live_entries():
        hydrate_workflow(entry, installation=installation)


# ---------------------------------------------------------------------------
# Hierarchy and ownership
# ---------------------------------------------------------------------------

_DEFINITION = {
    "name": "w",
    "app": "app_a",
    "tokens": {"root": "/data", "inbox": "{root}/inbox"},
    "processes": {
        "copy": {"implementation": "copy_files", "target": {"path": "{inbox}", "mode": "a"},
                 "retries": 3},
        "check": {"implementation": "check", "apply_only": True},
    },
    "steps": [
        {"id": "first", "label": "First copy", "process": "copy"},
        {"id": "verify", "process": "check"},
        {"id": "second", "label": "Second copy", "process": "copy",
         "config": {"target": {"mode": "b"}, "retries": None}},
    ],
}


def test_repeated_use_of_a_process_is_several_children_ordered_workflow_wide() -> None:
    workflow = hydrate_workflow(_DEFINITION, installation="test")
    copy = next(one for one in workflow.processes if one.name == "copy")

    assert [step.key for step in copy.steps] == ["first", "second"]
    assert [(p.name, s.key, s.ordinal) for p, s in workflow.ordered_steps()] == [
        ("copy", "first", 1), ("check", "verify", 2), ("copy", "second", 3),
    ]
    assert workflow.ordered_steps()[1][1].label == "verify", "label defaults to the key"


def test_each_process_owns_its_application_and_a_workflow_may_mix_them() -> None:
    workflow = hydrate_workflow(_DEFINITION, installation="test")
    assert {one.application for one in workflow.processes} == {"app_a"}

    step = WorkflowStep(key="s", ordinal=1, label="s")
    mixed = Workflow(name="m", installation="test", processes=(
        WorkflowProcess(name="a", application="app_a", operation="op", steps=(step,)),
        WorkflowProcess(name="b", application="app_b", operation="op",
                        steps=(WorkflowStep(key="t", ordinal=2, label="t"),)),
    ))
    assert [process.application for process, _step in mixed.ordered_steps()] == [
        "app_a", "app_b",
    ]


def test_a_namespace_entry_hydrates_like_a_mapping() -> None:
    namespace = Namespace({**_DEFINITION, "processes": dict(_DEFINITION["processes"])})
    assert [s.key for _p, s in hydrate_workflow(namespace, installation="t").ordered_steps()] == [
        "first", "verify", "second",
    ]


# ---------------------------------------------------------------------------
# Defaults and overrides
# ---------------------------------------------------------------------------

def test_defaults_merge_with_overrides_and_tokens_expand() -> None:
    workflow = hydrate_workflow(_DEFINITION, installation="test")
    tokens = workflow.resolved_tokens()
    copy, second = next((p, s) for p, s in workflow.ordered_steps() if s.key == "second")

    assert tokens == {"root": "/data", "inbox": "/data/inbox"}
    assert copy.effective(second, tokens) == {
        "target": {"path": "/data/inbox", "mode": "b"},
        "retries": None,
    }
    assert copy.defaults["retries"] == 3, "the process defaults are never changed"


@pytest.mark.parametrize("override", [None, False, {}, [], "", 0])
def test_every_override_value_replaces_rather_than_merging(override: Any) -> None:
    process = WorkflowProcess(name="p", application="a", operation="op",
                              defaults={"value": {"nested": 1}})
    step = WorkflowStep(key="s", ordinal=1, label="s", overrides={"value": override})
    expected = {"nested": 1} if isinstance(override, dict) else override
    assert process.effective(step, {}) == {"value": expected}


def test_an_absent_override_keeps_the_default() -> None:
    process = WorkflowProcess(name="p", application="a", operation="op",
                              defaults={"value": 7})
    step = WorkflowStep(key="s", ordinal=1, label="s", overrides={})
    assert process.effective(step, {}) == {"value": 7}


def test_hydration_keeps_absent_null_false_empty_overrides_distinct() -> None:
    entry = {"name": "w", "app": "a", "processes": {"p": {"implementation": "op"}},
             "steps": [{"id": "s", "process": "p",
                        "config": {"n": None, "f": False, "m": {}, "l": []}}]}
    (_process, step), = hydrate_workflow(entry, installation="t").ordered_steps()
    assert dict(step.overrides) == {"n": None, "f": False, "m": {}, "l": []}
    assert "absent" not in step.overrides


def test_engine_vocabulary_is_read_and_never_passed_down() -> None:
    workflow = hydrate_workflow(_DEFINITION, installation="test")
    check, verify = next((p, s) for p, s in workflow.ordered_steps() if s.key == "verify")
    assert check.apply_only(verify) is True
    assert check.always_run(verify) is False
    assert check.effective(verify, {}) == {}

    cleared = WorkflowStep(key="v", ordinal=1, label="v",
                           overrides={"apply_only": False, "always_run": True})
    assert check.apply_only(cleared) is False and check.always_run(cleared) is True


# ---------------------------------------------------------------------------
# Execution capabilities
# ---------------------------------------------------------------------------

def test_execution_defaults_never_enable_partial_runs_by_omission() -> None:
    assert dict(hydrate_workflow(_DEFINITION, installation="t").execution) == {
        "full": True, "dry_run": True, "step": False, "range": False,
    }
    declared = {**_DEFINITION, "execution": {"dry_run": False, "step": True}}
    assert dict(hydrate_workflow(declared, installation="t").execution) == {
        "full": True, "dry_run": False, "step": True, "range": False,
    }


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("selection", "ordinals"), [
    ({}, {1, 2, 3}),
    ({"step": "verify"}, {2}),
    ({"step": "Second copy"}, {3}),
    ({"from_step": "verify"}, {2, 3}),
    ({"to_step": "verify"}, {1, 2}),
    ({"from_step": "verify", "to_step": "second"}, {2, 3}),
    ({"step": "check"}, {2}),
])
def test_selection_by_key_label_or_process(selection: dict, ordinals: set) -> None:
    assert hydrate_workflow(_DEFINITION, installation="t").select(**selection) == ordinals


@pytest.mark.parametrize(("selection", "message"), [
    ({"step": "copy"}, "ambiguous"),
    ({"step": "nope"}, "no step matches"),
    ({"step": "first", "from_step": "verify"}, "cannot be combined"),
    ({"from_step": "second", "to_step": "first"}, "empty range"),
])
def test_selection_refusals(selection: dict, message: str) -> None:
    with pytest.raises(WorkflowError, match=message):
        hydrate_workflow(_DEFINITION, installation="t").select(**selection)


# ---------------------------------------------------------------------------
# Hydration refusals
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("entry", "message"), [
    ({"name": "w", "processes": {}, "steps": [{"process": "p"}]}, "missing required 'id'"),
    ({"name": "w", "processes": {}, "steps": [{"id": "s"}]}, "missing required 'process'"),
    ({"name": "w", "processes": {}, "steps": [{"id": "s", "process": "p"}]},
     "undefined process"),
    ({"name": "w", "processes": {}, "steps": [], "restore_mappings": []}, "retired key"),
])
def test_a_malformed_definition_is_refused_at_hydration(entry: dict, message: str) -> None:
    with pytest.raises(WorkflowError, match=message):
        hydrate_workflow(entry, installation="t")


def test_a_disabled_workflow_is_identity_only_and_never_refused_for_its_steps() -> None:
    entry = {"name": "w", "enabled": False, "steps": [{"id": "s", "process": "nope"}],
             "restore_mappings": []}
    workflow = hydrate_workflow(entry, installation="t")
    assert workflow.enabled is False and workflow.processes == ()


# ---------------------------------------------------------------------------
# The coordinator executes the canonical Workflow, and only it
# ---------------------------------------------------------------------------

def test_the_coordinator_refuses_anything_but_a_canonical_workflow(run_log) -> None:
    with pytest.raises(WorkflowError, match="canonical Workflow"):
        run_canonical_workflow(SimpleNamespace(applications=()), run_log, dict(_DEFINITION), {},
                     application="app_a")


def test_the_coordinator_executes_only_its_own_applications_processes(run_log) -> None:
    called: list[str] = []
    ctx = SimpleNamespace(applications=(
        Application(name="app_a", workflow_operations=(ApplicationCommand(name="op"),)),
        Application(name="app_b", workflow_operations=(ApplicationCommand(name="op"),)),
    ))
    mixed = Workflow(name="m", installation="t", processes=(
        WorkflowProcess(name="a", application="app_a", operation="op",
                        steps=(WorkflowStep(key="s", ordinal=1, label="s"),)),
        WorkflowProcess(name="b", application="app_b", operation="op",
                        steps=(WorkflowStep(key="t", ordinal=2, label="t"),)),
    ))

    with pytest.raises(WorkflowError, match="belongs to 'app_b'"):
        run_canonical_workflow(ctx, run_log, mixed,
                     {"op": lambda *_args: called.append("op")}, application="app_a")
    assert called == ["op"], "the application's own process ran; the other was refused"
