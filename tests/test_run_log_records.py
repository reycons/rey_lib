"""
Tests for the append-only typed run log
(SGC_Rey_Workflow_Pipeline_Automatic_Control_Batch_Logging).

Cover the centralized run-log record API in log_utils: run identity on every
record, execution vs run-result grouping, append-only accumulation, fail-closed
open without a durable log path, and fail-safe appends.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.logs import (
    bind_correlation,
    bind_step,
    clear_correlation,
    clear_step,
    log_app_execution,
    log_artifact_reference,
    log_config_file_reference,
    log_error,
    log_execution_plan,
    log_file_operation,
    log_input_discovered,
    log_input_file_reference,
    log_row_count,
    log_run_complete,
    log_run_start,
    log_run_summary,
    log_sql_execution,
    log_step_end,
    log_step_failure,
    log_step_start,
    log_validation_result,
    run_app_operation,
    open_run_log,
    project_run_log,
    read_run_log_sections,
    sanitize_command_arguments,
    sanitize_log_value,
)
from rey_lib.errors.error_utils import build_process_failure_payload
from rey_lib.run_lifecycle import run_app_operation as lifecycle_run_app_operation


def _ctx(tmp_path: Path) -> SimpleNamespace:
    """A context whose log directory is tmp_path (log_file established)."""
    return SimpleNamespace(
        log_file=str(tmp_path / "app.scan.jsonl"),
        owner_app_name="rey_loader",
        workflow_name="transform_load",
    )


def _read(path: Path) -> list[dict]:
    """Read all JSONL records from a run-log file."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_run_log_named_with_run_timestamp(tmp_path: Path) -> None:
    """The run log is {execution_name}.<run_timestamp>.jsonl in the log directory."""
    ctx = _ctx(tmp_path)
    path = open_run_log(ctx)
    assert path.name == f"transform_load.{ctx.run_timestamp}.jsonl"
    assert path.parent == tmp_path


def test_log_execution_plan_writes_execution_grouped_record(tmp_path: Path) -> None:
    """log_execution_plan emits one EXECUTION_PLAN record grouped as execution."""
    ctx = _ctx(tmp_path)
    log_run_start(ctx)
    steps = [
        {"sequence": 1, "step_id": "a", "step_name": "a", "app": "tool"},
        {"sequence": 2, "step_id": "b", "step_name": "b", "app": "tool"},
    ]
    log_execution_plan(ctx, total_steps=2, steps=steps)

    records = _read(Path(ctx.run_log_path))
    plans = [r for r in records if r["record_type"] == "EXECUTION_PLAN"]
    assert len(plans) == 1
    plan = plans[0]
    assert plan["record_group"] == "execution"
    assert plan["total_steps"] == 2
    assert [s["step_name"] for s in plan["steps"]] == ["a", "b"]
    assert plan["run_id"] == ctx.run_id


def test_records_carry_run_id_and_group(tmp_path: Path) -> None:
    """Every record includes run_id; types are grouped execution vs run-result."""
    ctx = _ctx(tmp_path)
    log_run_start(ctx)
    log_step_start(ctx, "load_data", 1, step_type="loader")
    log_step_end(ctx, "load_data", "success")
    log_run_summary(ctx, {"steps": 1, "status": "success"})
    log_run_complete(ctx, "success")

    records = _read(Path(ctx.run_log_path))
    assert all(r["run_id"] == ctx.run_id for r in records)
    by_type = {r["record_type"]: r for r in records}
    assert by_type["RUN_START"]["record_group"] == "execution"
    assert by_type["STEP_END"]["status"] == "success"
    assert by_type["RUN_SUMMARY"]["record_group"] == "results"
    assert by_type["RUN_SUMMARY"]["summary"] == {"steps": 1, "status": "success"}


def test_append_only_accumulates(tmp_path: Path) -> None:
    """Records accumulate; the log is never rewritten."""
    ctx = _ctx(tmp_path)
    log_run_start(ctx)
    log_artifact_reference(ctx, str(tmp_path / "out.csv"), role="output")
    log_run_complete(ctx, "success")
    assert len(_read(Path(ctx.run_log_path))) == 3


def test_run_app_operation_success_records_lifecycle(tmp_path: Path) -> None:
    """The shared app-run helper owns public command lifecycle records."""
    ctx = SimpleNamespace(log_file=str(tmp_path / "app.log"), app_name="rey_loader")

    assert lifecycle_run_app_operation is run_app_operation
    result = run_app_operation(ctx, "transform", lambda: 0)

    assert result == 0
    records = _read(Path(ctx.run_log_path))
    assert [record["record_type"] for record in records] == [
        "RUN_START",
        "RUN_COMPLETE",
        "RUN_SUMMARY",
    ]
    assert records[0]["operation"] == "transform"
    assert records[1]["status"] == "success"
    assert records[2]["summary"] == {
        "operation": "transform",
        "status": "success",
    }


def test_process_failure_payload_sanitizes_and_summarizes_stderr() -> None:
    """Process failure evidence includes bounded sanitized stderr details."""
    payload = build_process_failure_payload(
        message="Application exited with code 1",
        exit_code=1,
        stderr="database failed password=hunter2",
        failed_step_id="load",
    )

    assert payload["exit_code"] == 1
    assert payload["failed_step_id"] == "load"
    assert payload["stderr_summary"] == "database failed password=[REDACTED]"
    assert "hunter2" not in json.dumps(payload)
    assert payload["message"].startswith("Application exited with code 1: database failed")


def test_process_failure_payload_reports_missing_diagnostics() -> None:
    """Process failure evidence explicitly says when no output was available."""
    payload = build_process_failure_payload(
        message="Application exited with code 1",
        exit_code=1,
    )

    assert "did not emit stderr" in payload["message"]
    assert "stdout_summary" not in payload
    assert "stderr_summary" not in payload


def test_run_app_operation_failure_records_error_and_reraises(tmp_path: Path) -> None:
    """Failures produce canonical ERROR evidence and preserve exception behavior."""
    ctx = SimpleNamespace(log_file=str(tmp_path / "app.log"), app_name="rey_loader")

    def fail() -> None:
        raise ValueError("password=hunter2 failed")

    with pytest.raises(ValueError):
        run_app_operation(ctx, "load", fail)

    records = _read(Path(ctx.run_log_path))
    by_type = {record["record_type"]: record for record in records}
    assert [record["record_type"] for record in records] == [
        "RUN_START",
        "ERROR",
        "RUN_COMPLETE",
        "RUN_SUMMARY",
    ]
    assert by_type["ERROR"]["error_id"]
    assert by_type["ERROR"]["failed_step_id"] == "load"
    assert "hunter2" not in by_type["ERROR"]["error_message"]
    assert by_type["RUN_COMPLETE"]["status"] == "failed"
    assert by_type["RUN_COMPLETE"]["failure_record_id"] == by_type["ERROR"]["error_id"]
    assert by_type["RUN_SUMMARY"]["summary"]["status"] == "failed"


def test_run_app_operation_nonzero_result_records_failed_lifecycle(tmp_path: Path) -> None:
    """A nonzero integer return is failed evidence but still returned unchanged."""
    ctx = SimpleNamespace(log_file=str(tmp_path / "app.log"), app_name="file_redactor")

    result = run_app_operation(ctx, "redact", lambda: 1)

    assert result == 1
    records = _read(Path(ctx.run_log_path))
    by_type = {record["record_type"]: record for record in records}
    assert [record["record_type"] for record in records] == [
        "RUN_START",
        "ERROR",
        "STEP_FAILURE",
        "RUN_COMPLETE",
        "RUN_SUMMARY",
    ]
    assert by_type["ERROR"]["error_type"] == "AppOperationFailed"
    assert by_type["STEP_FAILURE"]["failure_record_id"] == by_type["ERROR"]["error_id"]
    assert by_type["RUN_COMPLETE"]["status"] == "failed"
    assert by_type["RUN_COMPLETE"]["failure_record_id"] == by_type["ERROR"]["error_id"]
    assert by_type["RUN_SUMMARY"]["summary"]["result"] == 1


def test_open_run_log_fails_closed_without_log_path() -> None:
    """Without a durable log path, opening the run log raises (fail closed)."""
    ctx = SimpleNamespace()
    with pytest.raises(ValueError):
        open_run_log(ctx)


def test_record_append_is_fail_safe(tmp_path: Path) -> None:
    """A record whose value is not JSON-serialisable is still written (default=str)."""
    ctx = _ctx(tmp_path)
    log_run_start(ctx, weird=object())
    records = _read(Path(ctx.run_log_path))
    assert records[0]["record_type"] == "RUN_START"


def test_typed_records_inherit_step_and_correlation_context(tmp_path: Path) -> None:
    """All typed helpers flow through the shared enrichment path."""
    ctx = _ctx(tmp_path)
    bind_step(step_id="prepare", step_name="Prepare", step_sequence=2,
              app="rey_loader", workflow_name="transform_load")
    bind_correlation("corr-1")
    try:
        log_input_discovered(
            ctx, input_name="trades", path=str(tmp_path / "trades.csv"),
            pattern="*.csv", source_config="source.trades", exists=True,
            safe_to_preview=True,
        )
    finally:
        clear_correlation()
        clear_step()

    record = _read(Path(ctx.run_log_path))[0]
    assert record["record_type"] == "INPUT_DISCOVERED"
    assert record["record_group"] == "files"
    assert record["record_subgroup"] == "input_files"
    assert record["step_id"] == "prepare"
    assert record["step_name"] == "Prepare"
    assert record["step_sequence"] == 2
    assert record["correlation_id"] == "corr-1"
    assert record["app"] == "rey_loader"


def test_write_side_sanitizer_masks_secret_like_keys(tmp_path: Path) -> None:
    """Secret-like helper fields are sanitized before JSONL persistence."""
    ctx = _ctx(tmp_path)
    log_app_execution(
        ctx,
        app="rey_loader",
        entrypoint="python -m rey_loader",
        arguments_redacted=["--config-path", "safe.yaml"],
        working_directory=str(tmp_path),
        status="started",
        metadata={"api_key": "abc", "nested": {"password": "pw"}, "safe": "ok"},
    )

    record = _read(Path(ctx.run_log_path))[0]
    assert record["metadata"]["api_key"] == "[REDACTED]"
    assert record["metadata"]["nested"]["password"] == "[REDACTED]"
    assert record["metadata"]["safe"] == "ok"
    assert sanitize_log_value({"token": "x", "safe": "y"}) == {
        "token": "[REDACTED]",
        "safe": "y",
    }


def test_command_argument_sanitizer_redacts_secret_like_flags() -> None:
    """Shared command sanitization redacts flag values without app-local logic."""
    assert sanitize_command_arguments([
        "run", "--password", "pw", "--api-key=abc", "--config-path", "safe.yaml",
    ]) == [
        "run", "--password", "[REDACTED]", "--api-key=[REDACTED]",
        "--config-path", "safe.yaml",
    ]


def test_failed_run_complete_requires_failure_evidence(tmp_path: Path) -> None:
    """A failed RUN_COMPLETE without evidence is a programming error."""
    ctx = _ctx(tmp_path)
    with pytest.raises(ValueError, match="requires structured failure evidence"):
        log_run_complete(ctx, "failed")


def test_step_failure_returns_record_id_for_failed_run_complete(tmp_path: Path) -> None:
    """The coordinator can create failure evidence, then reference it at completion."""
    ctx = _ctx(tmp_path)
    failure_id = log_step_failure(
        ctx,
        failed_step_id="load",
        failed_step_name="Load",
        message="loader failed",
        error_type="RuntimeError",
        sanitized_exception="RuntimeError: loader failed",
        exit_code=1,
    )
    log_run_complete(
        ctx,
        "failed",
        failure_record_id=failure_id,
        failed_step_id="load",
        failed_step_name="Load",
        failure_message="loader failed",
    )

    records = _read(Path(ctx.run_log_path))
    assert records[0]["record_type"] == "STEP_FAILURE"
    assert records[0]["failure_record_id"] == failure_id
    assert records[1]["record_type"] == "RUN_COMPLETE"
    assert records[1]["failure_record_id"] == failure_id


def test_new_event_helpers_emit_approved_record_types(tmp_path: Path) -> None:
    """Phase 2 helpers emit narrow event semantics through log_run_record."""
    ctx = _ctx(tmp_path)
    log_error(ctx, message="bad", error_type="RuntimeError")
    log_sql_execution(ctx, connection_name="local", database="db",
                      sql_path=str(tmp_path / "apply.sql"), operation="apply",
                      status="success", duration_ms=12)
    log_row_count(ctx, count_name="loaded", count=10, subject="trades")
    log_validation_result(ctx, validation_name="headers", status="success")

    types = [record["record_type"] for record in _read(Path(ctx.run_log_path))]
    assert types == ["ERROR", "SQL_EXECUTION", "ROW_COUNT", "VALIDATION_RESULT"]


def test_workflow_runner_emits_run_log_records(tmp_path: Path) -> None:
    """run_workflow emits RUN_START, per-step STEP_START/STEP_END, RUN_COMPLETE, RUN_SUMMARY."""
    from rey_lib.workflow import RunContext, run_workflow

    ctx = SimpleNamespace(log_file=str(tmp_path / "app.jsonl"))
    workflow = {
        "name": "wf",
        "processes": {"p1": {}, "p2": {}},
        "steps": [
            {"id": "s1", "label": "One", "process": "p1"},
            {"id": "s2", "label": "Two", "process": "p2"},
        ],
    }

    def handler(_ctx: object, _config: dict, _run: RunContext) -> None:
        return None

    result = run_workflow(ctx, workflow, {"p1": handler, "p2": handler})
    assert result.status == "success"

    records = _read(Path(ctx.run_log_path))
    types = [r["record_type"] for r in records]
    assert types[0] == "RUN_START"
    assert types.count("STEP_START") == 2
    assert types.count("STEP_END") == 2
    assert types[-2] == "RUN_COMPLETE"
    assert types[-1] == "RUN_SUMMARY"
    assert all(r["run_id"] == ctx.run_id for r in records)
    summary = [r for r in records if r["record_type"] == "RUN_SUMMARY"][0]["summary"]
    assert summary["steps_total"] == 2
    assert summary["steps_ok"] == 2
    assert summary["status"] == "success"


def test_workflow_step_context_is_active_only_during_handler(tmp_path: Path) -> None:
    """Workflow coordinator binds step context for handler execution and clears it."""
    from rey_lib.logs import current_step
    from rey_lib.workflow import RunContext, run_workflow

    ctx = SimpleNamespace(log_file=str(tmp_path / "app.jsonl"))
    workflow = {
        "name": "wf",
        "processes": {"p1": {}},
        "steps": [{"id": "s1", "label": "One", "process": "p1"}],
    }
    seen_steps: list[dict] = []

    def handler(_ctx: object, _config: dict, _run: RunContext) -> None:
        seen_steps.append(current_step() or {})

    result = run_workflow(ctx, workflow, {"p1": handler})

    assert result.status == "success"
    assert seen_steps == [{
        "step_id": "s1",
        "step_name": "One",
        "step_sequence": 1,
        "workflow_name": "wf",
    }]
    assert current_step() is None


def test_workflow_failure_emits_canonical_error_and_referenced_completion(
    tmp_path: Path,
) -> None:
    """Exception failures produce canonical ERROR evidence referenced by lifecycle records."""
    from rey_lib.logs import current_step
    from rey_lib.workflow import RunContext, run_workflow

    ctx = SimpleNamespace(log_file=str(tmp_path / "app.jsonl"))
    workflow = {
        "name": "wf",
        "processes": {"p1": {}},
        "steps": [{"id": "s1", "label": "One", "process": "p1"}],
    }

    def handler(_ctx: object, _config: dict, _run: RunContext) -> None:
        raise RuntimeError("load failed password=hunter2 token=abc123")

    result = run_workflow(ctx, workflow, {"p1": handler})

    assert result.status == "failed"
    records = _read(Path(ctx.run_log_path))
    error = next(r for r in records if r["record_type"] == "ERROR")
    failure = next(r for r in records if r["record_type"] == "STEP_FAILURE")
    complete = next(r for r in records if r["record_type"] == "RUN_COMPLETE")
    assert error["error_id"]
    assert error["error_type"] == "RuntimeError"
    assert error["error_message"]
    assert error["sanitized_exception"]
    assert error["sanitized_traceback"]
    assert error["traceback_summary"]
    assert error["failed_step_id"] == "s1"
    assert error["failed_step_name"] == "One"
    assert error["failed_step_sequence"] == 1
    assert "hunter2" not in json.dumps(error)
    assert "abc123" not in json.dumps(error)
    assert failure["failed_step_id"] == "s1"
    assert failure["failed_step_name"] == "One"
    assert failure["failed_step_sequence"] == 1
    assert failure["error_id"] == error["error_id"]
    assert failure["failure_record_id"] == error["error_id"]
    assert "hunter2" not in json.dumps(failure)
    assert "abc123" not in json.dumps(failure)
    assert complete["status"] == "failed"
    assert complete["failure_record_id"] == error["error_id"]
    assert complete["failed_step_id"] == "s1"
    assert complete["failed_step_name"] == "One"
    assert "hunter2" not in json.dumps(complete)
    assert "abc123" not in json.dumps(complete)
    assert current_step() is None


def test_workflow_file_operation_inherits_bound_step_context(tmp_path: Path) -> None:
    """FILE_OPERATION inherits workflow step context without changing file utilities."""
    from rey_lib.files.file_utils import write_file
    from rey_lib.workflow import RunContext, run_workflow

    ctx = SimpleNamespace(log_file=str(tmp_path / "app.jsonl"))
    workflow = {
        "name": "wf",
        "processes": {"p1": {}},
        "steps": [{"id": "s1", "label": "One", "process": "p1"}],
    }

    def handler(_ctx: object, _config: dict, _run: RunContext) -> None:
        write_file(tmp_path / "ctx.json", {"ok": True}, "JSON")

    result = run_workflow(ctx, workflow, {"p1": handler})

    assert result.status == "success"
    records = _read(Path(ctx.run_log_path))
    op = next(r for r in records if r["record_type"] == "FILE_OPERATION")
    assert op["operation"] == "write"
    assert op["step_id"] == "s1"
    assert op["step_name"] == "One"
    assert op["step_sequence"] == 1


def test_workflow_failed_status_outcome_emits_failure_evidence(tmp_path: Path) -> None:
    """A handler result with status=failed also produces STEP_FAILURE evidence."""
    from rey_lib.logs import current_step
    from rey_lib.workflow import RunContext, run_workflow

    ctx = SimpleNamespace(log_file=str(tmp_path / "app.jsonl"))
    workflow = {
        "name": "wf",
        "processes": {"p1": {}},
        "steps": [{"id": "s1", "label": "One", "process": "p1"}],
    }

    def handler(_ctx: object, _config: dict, _run: RunContext) -> SimpleNamespace:
        return SimpleNamespace(status="failed", detail="validation failed")

    result = run_workflow(ctx, workflow, {"p1": handler})

    assert result.status == "failed"
    records = _read(Path(ctx.run_log_path))
    failure = next(r for r in records if r["record_type"] == "STEP_FAILURE")
    complete = next(r for r in records if r["record_type"] == "RUN_COMPLETE")
    assert failure["error_type"] == "WorkflowStepFailed"
    assert failure["failed_step_id"] == "s1"
    assert complete["failure_record_id"] == failure["failure_record_id"]
    assert current_step() is None


def test_discover_runs_lists_newest_first_without_raw_data(tmp_path: Path) -> None:
    """discover_runs returns per-run summaries, newest first, with no raw records."""
    from rey_lib.logs import discover_runs

    for ts, status in [("20260706_090000", "success"), ("20260706_100000", "failed")]:
        (tmp_path / f"daily.{ts}.jsonl").write_text(
            "\n".join(json.dumps(record) for record in [
                {"record_type": "RUN_START", "run_id": f"r-{ts}", "run_timestamp": ts,
                 "timestamp": "2026-07-06T00:00:00+00:00", "app": "rey_loader"},
                {"record_type": "WARNING", "run_id": f"r-{ts}", "run_timestamp": ts},
                {"record_type": "RUN_COMPLETE", "run_id": f"r-{ts}", "run_timestamp": ts,
                 "timestamp": "2026-07-06T00:05:00+00:00", "status": status},
            ]),
            encoding="utf-8",
        )

    runs = discover_runs(tmp_path)

    assert [r["run_timestamp"] for r in runs] == ["20260706_100000", "20260706_090000"]
    newest = runs[0]
    assert newest["status"] == "failed"
    assert newest["warning_count"] == 1
    assert newest["error_count"] == 0
    assert newest["run_log_path"].endswith("daily.20260706_100000.jsonl")
    # A discovery summary carries no raw log data.
    assert "records" not in newest
    assert "sections" not in newest


def test_discover_runs_missing_or_empty_dir_yields_nothing(tmp_path: Path) -> None:
    """Missing or empty directories yield no runs rather than raising."""
    from rey_lib.logs import discover_runs

    assert discover_runs(tmp_path / "missing") == []
    assert discover_runs(tmp_path) == []


def _write_run_log(
    path: Path, run_timestamp: str, metadata: dict[str, str] | None = None,
) -> Path:
    """Write a minimal typed run log (RUN_START + RUN_COMPLETE) and return its path."""
    run_id = f"r-{run_timestamp}"
    start = {
        "record_type": "RUN_START", "run_id": run_id, "run_timestamp": run_timestamp,
        "timestamp": "2026-07-07T00:00:00+00:00", **(metadata or {}),
    }
    complete = {
        "record_type": "RUN_COMPLETE", "run_id": run_id, "run_timestamp": run_timestamp,
        "timestamp": "2026-07-07T00:05:00+00:00", "status": "success",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in (start, complete)),
                    encoding="utf-8")
    return path


def test_discover_runs_finds_jsonl_and_log_by_extension(tmp_path: Path) -> None:
    """Discovery finds *.jsonl and *.log run logs by extension, not by filename prefix."""
    from rey_lib.logs import discover_runs

    _write_run_log(tmp_path / "trade_analyzer.20260707_090000.jsonl", "20260707_090000")
    _write_run_log(tmp_path / "redact_trade_inbox.20260707_100000.log", "20260707_100000")

    names = {Path(run["run_log_path"]).name for run in discover_runs(tmp_path)}
    assert names == {
        "trade_analyzer.20260707_090000.jsonl",
        "redact_trade_inbox.20260707_100000.log",
    }


def test_discover_runs_includes_legacy_run_log_prefixed_files(tmp_path: Path) -> None:
    """Legacy run_log* files are discovered as ordinary *.jsonl/*.log files."""
    from rey_lib.logs import discover_runs

    _write_run_log(tmp_path / "run_log.20260707_120000.jsonl", "20260707_120000")

    runs = discover_runs(tmp_path)
    assert [Path(run["run_log_path"]).name for run in runs] == ["run_log.20260707_120000.jsonl"]


def test_discover_runs_searches_recursively(tmp_path: Path) -> None:
    """Discovery finds run logs nested beneath the scope's log folder."""
    from rey_lib.logs import discover_runs

    _write_run_log(tmp_path / "top.20260707_080000.jsonl", "20260707_080000")
    _write_run_log(tmp_path / "nested" / "deep.20260707_130000.jsonl", "20260707_130000")

    timestamps = {run["run_timestamp"] for run in discover_runs(tmp_path)}
    assert timestamps == {"20260707_080000", "20260707_130000"}


def test_discover_runs_skips_unparseable_and_untyped_logs(tmp_path: Path) -> None:
    """Files that cannot prove they are typed run logs never appear in discovery."""
    from rey_lib.logs import discover_runs

    _write_run_log(tmp_path / "valid.20260707_140000.jsonl", "20260707_140000")
    (tmp_path / "plain.20260707_150000.log").write_text("not json at all\n", encoding="utf-8")
    (tmp_path / "untyped.20260707_160000.jsonl").write_text(
        json.dumps({"note": "no record_type or run identity"}) + "\n", encoding="utf-8",
    )

    timestamps = [run["run_timestamp"] for run in discover_runs(tmp_path)]
    assert timestamps == ["20260707_140000"]


def test_discover_runs_derives_ownership_from_records_not_filename(tmp_path: Path) -> None:
    """Ownership/identity come from parsed log metadata, never the filename or path."""
    from rey_lib.logs import discover_runs

    _write_run_log(
        tmp_path / "misleading_name.20260707_170000.log", "20260707_170000",
        metadata={"app": "pipeline_coordinator", "pipeline": "trade_analyzer_generate_apply_ddl"},
    )

    run = discover_runs(tmp_path)[0]
    assert run["app"] == "pipeline_coordinator"
    assert run["pipeline"] == "trade_analyzer_generate_apply_ddl"
    assert run["workflow"] == ""
    assert run["run_id"] == "r-20260707_170000"


def test_get_run_section_and_file_reference(tmp_path: Path) -> None:
    """get_run_section returns one section; get_run_file_reference resolves a run file."""
    from rey_lib.logs import get_run_file_reference, get_run_section

    ctx = _ctx(tmp_path)
    report = tmp_path / "report.json"
    log_run_start(ctx)
    log_config_file_reference(ctx, str(tmp_path / "workflow.yaml"),
                              file_role="workflow_definition")
    log_artifact_reference(ctx, str(report), role="report", event="written")
    log_file_operation(ctx, "move", source_path=str(tmp_path / "a.csv"),
                       target_path=str(tmp_path / "b.csv"))
    log_run_complete(ctx, "success")
    path = ctx.run_log_path

    artifacts = get_run_section(path, "artifacts")
    assert artifacts["section"] == "artifacts"
    assert [Path(f["path"]).name for f in artifacts["files"]] == ["report.json"]

    config = get_run_section(path, "config_files")
    assert [Path(f["path"]).name for f in config["files"]] == ["workflow.yaml"]

    # A referenced artifact resolves to its log entry; an unknown path does not.
    ref = get_run_file_reference(path, str(report))
    assert ref is not None
    assert ref["section"] == "artifacts"
    assert ref["file_role"] == "report"
    assert get_run_file_reference(path, str(tmp_path / "nope.txt")) is None

    with pytest.raises(ValueError):
        get_run_section(path, "bogus")


def test_workflow_completion_appends_artifact_manifest(tmp_path: Path) -> None:
    """At completion the coordinator appends an ARTIFACT_MANIFEST of created artifacts only."""
    from rey_lib.logs import log_file_operation
    from rey_lib.workflow import run_workflow

    ctx = SimpleNamespace(log_file=str(tmp_path / "app.jsonl"))
    report = tmp_path / "report.json"

    def handler(_ctx: object, _config: dict, _run: object) -> None:
        # A created artifact and an unrelated file move within the same step.
        log_artifact_reference(ctx, str(report), role="report", event="written")
        log_file_operation(ctx, "move", source_path=str(tmp_path / "in.csv"),
                           target_path=str(tmp_path / "done.csv"))
        return None

    result = run_workflow(ctx, {
        "name": "wf", "processes": {"p1": {}},
        "steps": [{"id": "s1", "label": "One", "process": "p1"}],
    }, {"p1": handler})
    assert result.status == "success"

    records = _read(Path(ctx.run_log_path))
    # The manifest is appended once, at completion, after RUN_SUMMARY.
    assert records[-1]["record_type"] == "ARTIFACT_MANIFEST"
    manifest = records[-1]
    assert manifest["record_group"] == "files"
    assert manifest["record_subgroup"] == "artifacts"
    # Built from the run's own ARTIFACT_REFERENCE records; the moved file is excluded.
    names = [Path(item["path"]).name for item in manifest["artifacts"]]
    assert names == ["report.json"]
    assert manifest["artifacts"][0]["file_role"] == "report"


def test_run_log_sections_project_execution_files_and_results(tmp_path: Path) -> None:
    """Run logs are projected into execution/files/results without exposing file content."""
    log_path = tmp_path / "postgres_version_lint_comment.20260706_091845.jsonl"
    records = [
        {
            "record_type": "RUN_START",
            "record_group": "execution",
            "run_id": "run-1",
            "run_timestamp": "20260706_091845",
            "timestamp": "2026-07-06T13:18:45+00:00",
            "app": "rey_db_admin",
            "workflow": "postgres_version_lint_comment",
        },
        {
            "record_type": "CONFIG_FILE_MANIFEST",
            "record_group": "files",
            "record_subgroup": "config_files",
            "files": [
                {
                    "path": str(tmp_path / "workflow.yaml"),
                    "display_name": "workflow.yaml",
                    "file_role": "workflow_definition",
                }
            ],
        },
        {
            "record_type": "ARTIFACT_REFERENCE",
            "record_group": "files",
            "record_subgroup": "artifacts",
            "event": "generated",
            "artifact_path": str(tmp_path / "report.json"),
            "artifact_role": "report",
        },
        {
            "record_type": "RUN_SUMMARY",
            "record_group": "results",
            "summary": {"status": "success"},
        },
        {
            "record_type": "RUN_COMPLETE",
            "record_group": "execution",
            "timestamp": "2026-07-06T13:19:45+00:00",
            "status": "success",
        },
    ]
    log_path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")

    files = read_run_log_sections(log_path)["sections"]

    assert files["execution"]["count"] == 2
    assert files["files"]["config_files"]["files"][0]["display_name"] == "workflow.yaml"
    assert files["files"]["artifacts"]["files"][0]["display_name"] == "report.json"
    assert files["files"]["count"] == 2
    assert files["results"]["count"] == 1
    assert "content" not in files["files"]["config_files"]["files"][0]


def test_run_log_projection_ignores_moved_or_read_artifact_references(tmp_path: Path) -> None:
    """Only created/generated/written/exported/reported references become artifact files."""
    log_path = tmp_path / "transform_load.20260706_091845.jsonl"
    records = [
        {"record_type": "RUN_START", "run_id": "run-1", "run_timestamp": "20260706_091845"},
        {"record_type": "ARTIFACT_REFERENCE", "event": "read", "artifact_path": str(tmp_path / "input.csv")},
        {"record_type": "ARTIFACT_REFERENCE", "event": "moved", "artifact_path": str(tmp_path / "moved.csv")},
        {"record_type": "ARTIFACT_REFERENCE", "event": "written", "artifact_path": str(tmp_path / "out.csv")},
    ]
    log_path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")

    projection = project_run_log(log_path)

    artifacts = projection["sections"]["files"]["artifacts"]["files"]
    assert [Path(item["path"]).name for item in artifacts] == ["out.csv"]
    files_node = projection["tree"]["children"][1]
    assert files_node["label"] == "Files"
    artifacts_node = files_node["children"][2]
    assert artifacts_node["label"] == "Artifacts"
    assert artifacts_node["count"] == 1


def test_writer_helpers_group_records_by_view(tmp_path: Path) -> None:
    """Writer helpers stamp the SGC groups/subgroups and carry run identity."""
    ctx = _ctx(tmp_path)
    log_run_start(ctx)
    log_input_file_reference(ctx, str(tmp_path / "incoming" / "file.csv"),
                             file_role="source_data", consumed_by_step="validate_header")
    log_config_file_reference(ctx, str(tmp_path / "workflow.yaml"),
                              file_role="workflow_definition")
    log_file_operation(ctx, "move", source_path=str(tmp_path / "inbox" / "file.csv"),
                       target_path=str(tmp_path / "processing" / "file.csv"),
                       step_id="move_file_to_processing")
    log_artifact_reference(ctx, str(tmp_path / "report.json"), role="report",
                           event="generated", created_by_step="lint_sql")
    log_run_summary(ctx, {"status": "success"})
    log_run_complete(ctx, "success")

    records = _read(Path(ctx.run_log_path))
    by_type = {r["record_type"]: r for r in records}

    # Every record carries run identity.
    assert all(r["run_id"] == ctx.run_id for r in records)
    assert all(r["run_timestamp"] == ctx.run_timestamp for r in records)

    # Groups and subgroups per SGC_Rey_Log_Writer_Run_View_Groups.
    assert by_type["FILE_OPERATION"]["record_group"] == "execution"
    assert "record_subgroup" not in by_type["FILE_OPERATION"]
    assert by_type["INPUT_FILE_REFERENCE"]["record_group"] == "files"
    assert by_type["INPUT_FILE_REFERENCE"]["record_subgroup"] == "input_files"
    assert by_type["CONFIG_FILE_REFERENCE"]["record_subgroup"] == "config_files"
    assert by_type["CONFIG_FILE_REFERENCE"]["config_name"] == "workflow.yaml"
    assert by_type["CONFIG_FILE_REFERENCE"]["config_type"] == "workflow_definition"
    assert by_type["CONFIG_FILE_REFERENCE"]["exists"] is False
    assert by_type["CONFIG_FILE_REFERENCE"]["safe_to_preview"] is True
    assert by_type["ARTIFACT_REFERENCE"]["record_subgroup"] == "artifacts"
    assert by_type["RUN_SUMMARY"]["record_group"] == "results"

    # And the projection routes each into the right operator section.
    sections = read_run_log_sections(Path(ctx.run_log_path))["sections"]
    assert sections["files"]["input_files"]["files"][0]["display_name"] == "file.csv"
    assert sections["files"]["config_files"]["files"][0]["display_name"] == "workflow.yaml"
    assert sections["files"]["artifacts"]["files"][0]["display_name"] == "report.json"
    ops = sections["files"]["file_operations"]
    assert ops["count"] == 1
    assert ops["files"][0]["operation"] == "move"
    assert ops["files"][0]["destination_path"].endswith("processing/file.csv")
    assert ops["files"][0]["current_path"].endswith("processing/file.csv")
    # The move is execution history, not an artifact.
    assert [f["display_name"] for f in sections["files"]["artifacts"]["files"]] == ["report.json"]
    # FILE_OPERATION also remains in the execution audit trail.
    exec_types = [r["record_type"] for r in sections["execution"]["records"]]
    assert "FILE_OPERATION" in exec_types


def test_config_reference_normalized_fields(tmp_path: Path) -> None:
    """CONFIG_FILE_REFERENCE exposes CONFIG_REFERENCE semantics for consumers."""
    ctx = _ctx(tmp_path)
    config_path = tmp_path / "workflow.yaml"
    config_path.write_text("name: wf\n", encoding="utf-8")

    log_config_file_reference(
        ctx,
        str(config_path),
        config_name="workflow.yaml",
        config_type="workflow",
        exists=True,
        safe_to_preview=False,
        config_hash="abc123",
    )

    record = _read(Path(ctx.run_log_path))[0]
    assert record["record_type"] == "CONFIG_FILE_REFERENCE"
    assert record["record_subgroup"] == "config_files"
    assert record["config_name"] == "workflow.yaml"
    assert record["config_type"] == "workflow"
    assert record["source"] == "config_provenance"
    assert record["exists"] is True
    assert record["safe_to_preview"] is False
    assert record["config_hash"] == "abc123"
    assert record["hash"] == "abc123"
