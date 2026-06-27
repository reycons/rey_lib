"""Tests for the shared workflow/step engine (rey_lib.workflow)."""

from __future__ import annotations

import pytest

from rey_lib.workflow import (
    RunContext,
    StepResult,
    StepSpec,
    WorkflowError,
    build_steps,
    run_steps,
)


def _record(name):
    def handler(ctx: RunContext):
        ctx.data.setdefault("ran", []).append(name)
    return handler


def test_runs_in_order_and_records_metadata():
    ctx = RunContext()
    steps = [StepSpec("a", _record("a")), StepSpec("b", _record("b"))]
    result = run_steps(steps, ctx)
    assert result.status == "success"
    assert ctx.data["ran"] == ["a", "b"]
    assert ctx.metadata["status"] == "success"
    assert [s["name"] for s in ctx.metadata["steps"]] == ["a", "b"]


def test_fail_closed_stops_on_first_failure():
    ctx = RunContext()

    def boom(_ctx):
        raise ValueError("kaboom")

    steps = [StepSpec("a", _record("a")), StepSpec("b", boom), StepSpec("c", _record("c"))]
    result = run_steps(steps, ctx)
    assert result.status == "failed"
    assert isinstance(result.error, ValueError)
    assert ctx.data["ran"] == ["a"]  # c never ran
    assert ctx.metadata["status"] == "failed"
    assert [s["status"] for s in ctx.metadata["steps"]] == ["ok", "failed"]


def test_dry_run_skips_apply_only_steps():
    ctx = RunContext(apply=False)
    steps = [
        StepSpec("export", _record("export")),
        StepSpec("recreate", _record("recreate"), apply_only=True),
    ]
    run_steps(steps, ctx)
    assert ctx.data["ran"] == ["export"]  # recreate skipped in dry-run

    ctx2 = RunContext(apply=True)
    run_steps(steps, ctx2)
    assert ctx2.data["ran"] == ["export", "recreate"]


def test_build_steps_resolves_registry_and_rejects_unknown():
    registry = {"x": StepSpec("x", _record("x")), "y": StepSpec("y", _record("y"))}
    steps = build_steps(["y", "x"], registry)
    assert [s.name for s in steps] == ["y", "x"]
    with pytest.raises(WorkflowError):
        build_steps(["x", "missing"], registry)


def test_handler_may_return_stepresult():
    ctx = RunContext()
    steps = [StepSpec("a", lambda c: StepResult("a", "ok", "did a thing"))]
    result = run_steps(steps, ctx)
    assert result.results[0].detail == "did a thing"
