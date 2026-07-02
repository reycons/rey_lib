"""Tests for the shared workflow coordinator (function-call stacker).

Covers SGC_rey_workflow_internal_function_call_model mechanics owned by
rey_lib: dispatch by registered process name (never by label), process reuse
across steps, effective config (process defaults + step override), workflow
token resolution, required step id, dry-run apply_only skipping, single-step
execution, and fail-closed behaviour on unknown process / missing handler /
handler error.
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.workflow import RunContext, StepResult, WorkflowError, run_workflow


def _recorder() -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    """Return (calls, handler) where handler records (process-scope, config)."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def handler(ctx: Any, config: dict[str, Any], run: RunContext) -> None:
        calls.append((config.get("_scope", ""), config))
        return None

    return calls, handler


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def test_dispatch_by_process_name_reused_across_steps() -> None:
    """One process handler is reused by multiple steps, dispatched by process."""
    calls: list[str] = []

    def git_commit(ctx: Any, config: dict[str, Any], run: RunContext) -> StepResult:
        calls.append(str(config.get("label")))
        return StepResult("git_commit", "ok")

    def export(ctx: Any, config: dict[str, Any], run: RunContext) -> None:
        return None

    workflow = {
        "name": "w",
        "processes": {"git_commit": {"repo_root": "r", "require_commit": True},
                      "export": {}},
        "steps": [
            {"id": "export_before", "label": "Export before", "process": "export"},
            {"id": "commit_before", "label": "Commit before", "process": "git_commit",
             "config": {"label": "before-ddl"}},
            {"id": "commit_after", "label": "Commit after", "process": "git_commit",
             "config": {"label": "after-ddl"}},
        ],
    }

    run = run_workflow(object(), workflow, {"git_commit": git_commit, "export": export})

    assert run.status == "success"
    assert [o.process for o in run.outcomes] == ["export", "git_commit", "git_commit"]
    assert calls == ["before-ddl", "after-ddl"]


def test_dispatch_ignores_labels() -> None:
    """Two steps with different labels but the same process hit one handler."""
    hits = []

    def handler(ctx: Any, config: dict[str, Any], run: RunContext) -> None:
        hits.append(config)
        return None

    workflow = {
        "name": "w",
        "processes": {"p": {}},
        "steps": [
            {"id": "a", "label": "Totally Different Label", "process": "p"},
            {"id": "b", "label": "Another Human Label", "process": "p"},
        ],
    }
    run_workflow(object(), workflow, {"p": handler})
    assert len(hits) == 2


# ---------------------------------------------------------------------------
# Effective config + tokens
# ---------------------------------------------------------------------------

def test_effective_config_merges_step_over_process() -> None:
    """Step config overrides process defaults; nested dicts merge."""
    seen: dict[str, Any] = {}

    def handler(ctx: Any, config: dict[str, Any], run: RunContext) -> None:
        seen.update(config)
        return None

    workflow = {
        "name": "w",
        "processes": {"p": {"a": 1, "b": 2, "nested": {"x": 1, "y": 2}}},
        "steps": [{"id": "s", "label": "S", "process": "p",
                   "config": {"b": 9, "nested": {"y": 20, "z": 30}}}],
    }
    run_workflow(object(), workflow, {"p": handler})
    assert seen == {"a": 1, "b": 9, "nested": {"x": 1, "y": 20, "z": 30}}


def test_workflow_tokens_resolve_into_process_config() -> None:
    """Workflow-local tokens expand in config; global path tokens are left intact."""
    seen: dict[str, Any] = {}

    def handler(ctx: Any, config: dict[str, Any], run: RunContext) -> None:
        seen.update(config)
        return None

    workflow = {
        "name": "w",
        "tokens": {"ddl_root": "{data}/rey_db_admin/database_ddl"},
        "processes": {"p": {"output_root": "{ddl_root}", "repo_root": "{ddl_root}"}},
        "steps": [{"id": "s", "label": "S", "process": "p"}],
    }
    run_workflow(object(), workflow, {"p": handler})
    # Local {ddl_root} expanded; global {data} preserved for the ctx path resolver.
    assert seen["output_root"] == "{data}/rey_db_admin/database_ddl"
    assert seen["repo_root"] == "{data}/rey_db_admin/database_ddl"


# ---------------------------------------------------------------------------
# Dry-run and single-step
# ---------------------------------------------------------------------------

def test_dry_run_skips_apply_only_process() -> None:
    """A process whose effective config sets apply_only is skipped in dry-run."""
    ran: list[str] = []

    def handler(ctx: Any, config: dict[str, Any], run: RunContext) -> None:
        ran.append(config.get("_scope", ""))
        return None

    workflow = {
        "name": "w",
        "processes": {"lint": {"_scope": "lint"},
                      "recreate": {"_scope": "recreate", "apply_only": True}},
        "steps": [{"id": "lint", "label": "L", "process": "lint"},
                  {"id": "recreate", "label": "R", "process": "recreate"}],
    }
    run = run_workflow(object(), workflow, {"lint": handler, "recreate": handler},
                       apply=False)
    assert [o.status for o in run.outcomes] == ["ok", "skipped"]
    assert ran == ["lint"]


def test_dry_run_skips_apply_only_from_step_override() -> None:
    """apply_only may come from a step override (e.g. the second export)."""
    ran: list[str] = []

    def handler(ctx: Any, config: dict[str, Any], run: RunContext) -> None:
        ran.append(config["_scope"])
        return None

    workflow = {
        "name": "w",
        "processes": {"export": {"_scope": "export"}},
        "steps": [{"id": "before", "label": "B", "process": "export"},
                  {"id": "after", "label": "A", "process": "export",
                   "config": {"apply_only": True}}],
    }
    run = run_workflow(object(), workflow, {"export": handler}, apply=False)
    assert [o.status for o in run.outcomes] == ["ok", "skipped"]
    assert ran == ["export"]


def test_single_step_only_runs_matching_id() -> None:
    """only=<id> runs just that step."""
    ran: list[str] = []
    workflow = {
        "name": "w",
        "processes": {"a": {}, "b": {}},
        "steps": [{"id": "sa", "label": "A", "process": "a"},
                  {"id": "sb", "label": "B", "process": "b"}],
    }
    registry = {"a": lambda *_: ran.append("a"), "b": lambda *_: ran.append("b")}
    run = run_workflow(object(), workflow, registry, only="sb")
    assert ran == ["b"]
    assert [o.id for o in run.outcomes] == ["sb"]


# ---------------------------------------------------------------------------
# Fail-closed
# ---------------------------------------------------------------------------

def test_step_id_required() -> None:
    """A step without an id fails closed."""
    workflow = {"name": "w", "processes": {"p": {}},
                "steps": [{"label": "S", "process": "p"}]}
    with pytest.raises(WorkflowError, match="missing required 'id'"):
        run_workflow(object(), workflow, {"p": lambda *_: None})


def test_undefined_process_fails_closed() -> None:
    """A step calling a process absent from workflow.processes fails closed."""
    workflow = {"name": "w", "processes": {"p": {}},
                "steps": [{"id": "s", "label": "S", "process": "missing"}]}
    with pytest.raises(WorkflowError, match="undefined process"):
        run_workflow(object(), workflow, {"p": lambda *_: None})


def test_process_without_registered_handler_fails_closed() -> None:
    """A process with no handler in this app's registry fails closed."""
    workflow = {"name": "w", "processes": {"p": {}},
                "steps": [{"id": "s", "label": "S", "process": "p"}]}
    with pytest.raises(WorkflowError, match="no registered handler"):
        run_workflow(object(), workflow, {})


def test_handler_error_stops_run_fail_closed() -> None:
    """A handler exception records a failed outcome and stops the run."""
    ran: list[str] = []

    def boom(ctx: Any, config: dict[str, Any], run: RunContext) -> None:
        raise RuntimeError("nope")

    def after(ctx: Any, config: dict[str, Any], run: RunContext) -> None:
        ran.append("after")
        return None

    workflow = {
        "name": "w",
        "processes": {"boom": {}, "after": {}},
        "steps": [{"id": "s1", "label": "1", "process": "boom"},
                  {"id": "s2", "label": "2", "process": "after"}],
    }
    run = run_workflow(object(), workflow, {"boom": boom, "after": after})
    assert run.status == "failed"
    assert run.outcomes[-1].status == "failed"
    assert "nope" in (run.outcomes[-1].error or "")
    assert ran == []
