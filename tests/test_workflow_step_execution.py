"""Tests for shared workflow step / range execution.

Covers SGC_Rey_Lib_Shared_Workflow_Step_Execution: single-step and inclusive
range selection resolved by id / label / process, deterministic and fail-closed,
with existing full-run and dry-run behaviour unchanged and only selected steps
appearing in the returned outcomes.
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.workflow import RunContext, WorkflowError, run_workflow


def _registry(process_names: list[str], calls: list[str]) -> dict[str, Any]:
    """Return a registry whose handlers record the process they ran."""
    def make(process_name: str) -> Any:
        def handler(ctx: Any, config: dict[str, Any], run: RunContext) -> None:
            calls.append(process_name)
            return None
        return handler
    return {name: make(name) for name in process_names}


def _workflow() -> dict[str, Any]:
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


def _run(**kwargs: Any) -> tuple[Any, list[str]]:
    calls: list[str] = []
    registry = _registry(["p1", "p2", "p3", "p4"], calls)
    run = run_workflow(object(), _workflow(), registry, **kwargs)
    return run, calls


def _ids(run: Any) -> list[str]:
    return [outcome.id for outcome in run.outcomes]


# --- Unchanged behaviour ----------------------------------------------------

def test_full_workflow_runs_all_steps() -> None:
    run, calls = _run()
    assert run.status == "success"
    assert _ids(run) == ["s1", "s2", "s3", "s4"]
    assert calls == ["p1", "p2", "p3", "p4"]


def test_full_dry_run_skips_apply_only_step() -> None:
    run, calls = _run(apply=False)
    assert _ids(run) == ["s1", "s2", "s3", "s4"]
    statuses = {o.id: o.status for o in run.outcomes}
    assert statuses["s2"] == "skipped"          # apply_only skipped in dry-run
    assert calls == ["p1", "p3", "p4"]          # p2 handler never invoked


# --- Single step ------------------------------------------------------------

def test_single_step_by_id() -> None:
    run, calls = _run(step="s3")
    assert _ids(run) == ["s3"]
    assert calls == ["p3"]


def test_single_step_by_label() -> None:
    run, _ = _run(step="Two")
    assert _ids(run) == ["s2"]


def test_single_step_by_process() -> None:
    run, _ = _run(step="p4")
    assert _ids(run) == ["s4"]


def test_legacy_only_still_selects_single_step() -> None:
    run, _ = _run(only="s2")
    assert _ids(run) == ["s2"]


# --- Ranges -----------------------------------------------------------------

def test_from_step_runs_to_end() -> None:
    run, _ = _run(from_step="s3")
    assert _ids(run) == ["s3", "s4"]


def test_to_step_runs_from_start() -> None:
    run, _ = _run(to_step="s2")
    assert _ids(run) == ["s1", "s2"]


def test_from_and_to_step_inclusive_range() -> None:
    run, _ = _run(from_step="s2", to_step="s3")
    assert _ids(run) == ["s2", "s3"]


def test_dry_run_applies_to_selected_range_only() -> None:
    run, calls = _run(from_step="s2", to_step="s3", apply=False)
    assert _ids(run) == ["s2", "s3"]            # only the selected range
    statuses = {o.id: o.status for o in run.outcomes}
    assert statuses["s2"] == "skipped"          # apply_only skipped in dry-run
    assert calls == ["p3"]                       # only s3 executed


# --- Fail-closed ------------------------------------------------------------

def test_unknown_step_fails_closed() -> None:
    with pytest.raises(WorkflowError):
        _run(step="nope")


def test_ambiguous_identifier_fails_closed() -> None:
    calls: list[str] = []
    registry = _registry(["p_a", "shared"], calls)
    workflow = {
        "name": "amb",
        "processes": {"p_a": {}, "shared": {}},
        "steps": [
            {"id": "a", "label": "A", "process": "p_a"},
            {"id": "b", "label": "B", "process": "shared"},
            {"id": "c", "label": "C", "process": "shared"},
        ],
    }
    with pytest.raises(WorkflowError):
        run_workflow(object(), workflow, registry, step="shared")  # matches b and c


def test_step_combined_with_range_fails_closed() -> None:
    with pytest.raises(WorkflowError):
        _run(step="s1", from_step="s2")


def test_reversed_range_fails_closed() -> None:
    with pytest.raises(WorkflowError):
        _run(from_step="s3", to_step="s1")
