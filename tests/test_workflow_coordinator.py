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

from tests.conftest import make_run_log

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

# ---------------------------------------------------------------------------
# Retired configuration (SGC_Log_Run_Rollback)
# ---------------------------------------------------------------------------

def test_retired_restore_mappings_key_is_rejected() -> None:
    """A retired key fails closed rather than being silently ignored."""
    workflow = {
        "name": "standalone",
        "restore_mappings": [{"from": "/source", "to": "/dest"}],
        "processes": {"noop": {}},
        "steps": [{"id": "s1", "label": "S1", "process": "noop"}],
    }

    with pytest.raises(WorkflowError, match="retired key 'restore_mappings'"):
        run_workflow(object(), workflow, {"noop": lambda *_: None})


def test_retired_key_is_rejected_before_any_step_runs() -> None:
    """Rejection happens during validation, so no handler is invoked."""
    calls, handler = _recorder()
    workflow = {
        "name": "standalone",
        "restore_mappings": [],
        "processes": {"noop": {}},
        "steps": [{"id": "s1", "label": "S1", "process": "noop"}],
    }

    with pytest.raises(WorkflowError, match="retired key"):
        run_workflow(object(), workflow, {"noop": handler})

    assert calls == []


def test_workflow_without_the_retired_key_runs_normally() -> None:
    calls, handler = _recorder()
    workflow = {
        "name": "standalone",
        "processes": {"noop": {}},
        "steps": [{"id": "s1", "label": "S1", "process": "noop"}],
    }

    run = run_workflow(object(), workflow, {"noop": handler})

    assert run.status == "success"
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Disabled workflows and pre-execution failure evidence
# ---------------------------------------------------------------------------

def test_disabled_workflow_is_refused_without_raising() -> None:
    """enabled: false is a governed outcome, not a fault: no exception."""
    workflow = {
        "name": "reduce_source_files",
        "enabled": False,
        "processes": {"p": {}},
        "steps": [{"id": "s", "process": "p"}],
    }
    calls: list[str] = []

    def handler(ctx: Any, config: dict[str, Any], run: RunContext) -> None:
        calls.append("ran")

    run = run_workflow(object(), workflow, {"p": handler})

    assert run.status == "refused"
    assert run.status != "success"
    assert run.name == "reduce_source_files"
    assert calls == []


def test_a_disabled_workflow_is_refused_before_its_definition_is_parsed() -> None:
    """A workflow that will not run is never rejected for an unrelated defect."""
    workflow = {
        "name": "w",
        "enabled": False,
        "processes": {},
        "steps": [{"label": "no id here"}],
    }

    assert run_workflow(object(), workflow, {}).status == "refused"


def test_a_workflow_without_an_enabled_key_still_runs() -> None:
    """Absent means enabled; only an explicit false refuses."""
    calls: list[str] = []

    def handler(ctx: Any, config: dict[str, Any], run: RunContext) -> None:
        calls.append("ran")

    workflow = {
        "name": "w",
        "processes": {"p": {}},
        "steps": [{"id": "s", "process": "p"}],
    }
    assert run_workflow(object(), workflow, {"p": handler}).status == "success"
    assert calls == ["ran"]
    assert run_workflow(
        object(), {**workflow, "enabled": True}, {"p": handler}
    ).status == "success"


def _run_log_records(tmp_path: Any) -> list[dict[str, Any]]:
    """Read the run log the logging layer named for itself."""
    import json
    from pathlib import Path

    logs = sorted(Path(tmp_path).glob("*.jsonl"))
    assert len(logs) == 1, f"expected one run log, found {logs}"
    return [
        json.loads(line)
        for line in logs[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _log_ctx(tmp_path: Any) -> Any:
    from types import SimpleNamespace

    from rey_lib.run.identity import establish_run_identity

    ctx = SimpleNamespace(
        log_file=str(tmp_path / "app.jsonl"), owner_app_name="file_operator"
    )
    establish_run_identity(ctx)
    return ctx


def test_missing_handler_writes_failure_evidence_before_raising(
    tmp_path: Any,
) -> None:
    """A failure between RUN_START and the first step is still evidenced."""
    import json

    ctx = _log_ctx(tmp_path)
    run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    workflow = {
        "name": "reduce_source_files",
        "processes": {"prepare_rule_set_inputs": {}},
        "steps": [
            {
                "id": "reduce_source_files",
                "label": "Reduce classified CSV sources",
                "process": "prepare_rule_set_inputs",
            }
        ],
    }

    with pytest.raises(WorkflowError, match="no registered handler"):
        run_workflow(ctx, run_log, workflow, {})

    records = _run_log_records(tmp_path)
    types = [record.get("record_type") for record in records]
    assert "RUN_START" in types
    assert "RUN_COMPLETE" in types

    complete = next(r for r in records if r.get("record_type") == "RUN_COMPLETE")
    assert complete["status"] == "failed"
    assert complete["failed_step_id"] == "reduce_source_files"
    assert "no registered handler" in complete["failure_message"]

    failure = next(
        r
        for r in records
        if r.get("record_type") not in {"RUN_START", "RUN_COMPLETE"}
        and "no registered handler" in json.dumps(r)
    )
    assert failure["error_type"] == "WorkflowError"
    assert failure["process"] == "prepare_rule_set_inputs"


def test_undefined_process_writes_failure_evidence_before_raising(
    tmp_path: Any,
) -> None:
    ctx = _log_ctx(tmp_path)
    run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    workflow = {"name": "w", "processes": {}, "steps": [{"id": "s", "process": "nope"}]}

    with pytest.raises(WorkflowError, match="undefined process"):
        run_workflow(ctx, run_log, workflow, {"nope": lambda *a: None})

    complete = next(
        r for r in _run_log_records(tmp_path) if r.get("record_type") == "RUN_COMPLETE"
    )
    assert complete["status"] == "failed"


def test_disabled_workflow_is_recorded_through_the_normal_run_path(
    tmp_path: Any,
) -> None:
    """The attempt is evidence: a run starts, completes, and is finalized."""
    ctx = _log_ctx(tmp_path)
    run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))

    run = run_workflow(
        ctx,
        run_log, {"name": "w", "enabled": False, "processes": {}, "steps": []},
        {},
    )

    assert run.status == "refused"
    records = _run_log_records(tmp_path)
    types = [record.get("record_type") for record in records]
    assert "RUN_START" in types
    assert "RUN_COMPLETE" in types

    complete = next(r for r in records if r.get("record_type") == "RUN_COMPLETE")
    assert complete["status"] == "refused"
    assert "is disabled and was not executed" in complete["message"]
