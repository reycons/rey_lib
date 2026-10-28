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


from tests.conftest import make_db_run_log, start_test_run

from rey_lib.logs import (
    bind_correlation,
    bind_step,
    clear_correlation,
    clear_step,
    log_app_execution,
    log_artifact_reference,
    log_config_file_reference,
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
    project_run_log,
    read_run_log_sections,
    sanitize_command_arguments,
    sanitize_log_value,
)
from rey_lib.errors.error_utils import build_process_failure_payload
from rey_lib.run_lifecycle import run_app_operation as lifecycle_run_app_operation
from rey_lib.data.errors import DataStructureError
from rey_lib.errors.error_utils import ConfigError


def _ctx(tmp_path: Path) -> SimpleNamespace:
    """A context whose log directory is tmp_path, identified as a launch would.

    Run logging consumes an identity; it never mints one. A launch establishes
    it through ``rey_lib.run`` before logging opens, so the fixture does the
    same rather than relying on the write path to invent one.
    """
    ctx = SimpleNamespace(
        log_file=str(tmp_path / "app.scan.jsonl"),
        # AppLogger's configuration resolves the run log and names it on both fields; the
        # fixture does the same so code reading the path as a fact about the
        # execution sees the file the owner writes.
        run_log_path=str(tmp_path / "app.scan.jsonl"),
        owner_app_name="data_engine",
        workflow_name="transform_load",
    )
    start_test_run(ctx)
    return ctx


def _log(ctx: SimpleNamespace, tmp_path: Path):
    """The run log this launch would have opened.

    Identity comes from the context because the launch boundary establishes it
    before the run log exists; everything after that is the run log's.
    """
    return make_db_run_log(
        tmp_path,
        app=str(getattr(ctx, "owner_app_name", "") or "data_engine"),
        run_id=ctx.run_id,
        run_timestamp=str(ctx.run_timestamp),
        path=str(getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None)),
    )


def _read(path: Path) -> list[dict]:
    """Read all JSONL records from a run-log file."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_run_log_named_with_run_timestamp(tmp_path: Path) -> None:
    """The JSONL file is named beside the configured log path, with the run timestamp.

    The path is the JSONL writer's, resolved when the logging writer is
    assembled (backlog logging.app_logger.s8_json_output_cleanup).
    """
    from rey_lib.logs.log_writer import build_log_writer

    ctx = SimpleNamespace(
        run_timestamp="20260822_000000",
        log_path=str(tmp_path / "transform_load.{operation}.{timestamp}.log"),
        logging=SimpleNamespace(run_store="jsonl"),
    )
    path = build_log_writer(ctx, "scan").json_path()
    assert path.name == "transform_load.scan.20260822_000000.jsonl"
    assert path.parent == tmp_path


def test_log_execution_plan_writes_execution_grouped_record(tmp_path: Path) -> None:
    """log_execution_plan emits one EXECUTION_PLAN record grouped as execution."""
    ctx = _ctx(tmp_path)
    run_log = _log(ctx, tmp_path)
    log_run_start(run_log)
    steps = [
        {"sequence": 1, "step_id": "a", "step_name": "a", "app": "tool"},
        {"sequence": 2, "step_id": "b", "step_name": "b", "app": "tool"},
    ]
    log_execution_plan(run_log, total_steps=2, steps=steps)

    records = _read(Path(run_log.path()))
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
    run_log = _log(ctx, tmp_path)
    log_run_start(run_log)
    log_step_start(run_log, "load_data", 1, step_type="loader")
    log_step_end(run_log, "load_data", "success")
    log_run_summary(run_log, {"steps": 1, "status": "success"})
    log_run_complete(run_log, "success")

    records = _read(Path(run_log.path()))
    assert all(r["run_id"] == ctx.run_id for r in records)
    by_type = {r["record_type"]: r for r in records}
    assert by_type["RUN_START"]["record_group"] == "execution"
    assert by_type["STEP_END"]["status"] == "success"
    assert by_type["RUN_SUMMARY"]["record_group"] == "results"
    assert by_type["RUN_SUMMARY"]["summary"] == {"steps": 1, "status": "success"}


def test_append_only_accumulates(tmp_path: Path) -> None:
    """Records accumulate; the log is never rewritten."""
    ctx = _ctx(tmp_path)
    run_log = _log(ctx, tmp_path)
    log_run_start(run_log)
    log_artifact_reference(run_log, str(tmp_path / "out.csv"), role="output")
    log_run_complete(run_log, "success")
    assert len(_read(Path(run_log.path()))) == 3


def test_run_app_operation_success_records_lifecycle(tmp_path: Path) -> None:
    """The shared app-run helper owns public command lifecycle records."""
    ctx = SimpleNamespace(log_file=str(tmp_path / "app.log"), app_name="data_engine")
    start_test_run(ctx)
    run_log = _log(ctx, tmp_path)
    result = lifecycle_run_app_operation(ctx, run_log, "transform", lambda: 0)

    assert result == 0
    records = _read(Path(run_log.path()))
    # The execution JSONL stays the authoritative event log — no summary record.
    assert [record["record_type"] for record in records] == ["RUN_START", "RUN_COMPLETE"]
    assert records[0]["operation"] == "transform"
    assert records[1]["status"] == "success"
    # Generic app-operation lifecycle is not the run owner; the top-level app
    # performs summary/manifest finalization exactly once.
    assert not any(record["record_type"] == "RESULTS_SUMMARY" for record in records)


def test_run_app_operation_records_what_it_was_asked_to_do(tmp_path: Path) -> None:
    """RUN_START carries the settings, BEFORE the body can refuse them.

    A command that refuses its own arguments otherwise records only that an
    operation failed and why -- never what it was asked to do. The refusal
    "--table names a destination with no way to reach it" is readable only
    beside a connection the record shows was empty.
    """
    ctx = SimpleNamespace(log_file=str(tmp_path / "app.log"), app_name="data_engine")
    start_test_run(ctx)
    run_log = _log(ctx, tmp_path)

    def _refuses() -> int:
        raise ValueError("--table names a destination with no way to reach it.")

    with pytest.raises(ValueError):
        lifecycle_run_app_operation(
            ctx, run_log, "load", _refuses,
            settings={"table": "test", "connection": "", "apply": False},
        )

    start = _read(Path(run_log.path()))[0]
    assert start["record_type"] == "RUN_START"
    # The empty one is the point: it is what explains the refusal.
    assert start["settings"] == {"table": "test", "connection": "", "apply": False}


def test_run_app_operation_records_no_settings_when_given_none(
    tmp_path: Path,
) -> None:
    """An app that passes none records exactly what it recorded before."""
    ctx = SimpleNamespace(log_file=str(tmp_path / "app.log"), app_name="data_engine")
    start_test_run(ctx)
    run_log = _log(ctx, tmp_path)
    lifecycle_run_app_operation(ctx, run_log, "transform", lambda: 0)

    start = _read(Path(run_log.path()))[0]
    assert "settings" not in start


def test_process_failure_payload_sanitizes_and_summarizes_stderr() -> None:
    """Process failure evidence includes bounded sanitized stderr details."""
    payload = build_process_failure_payload(
        message="Application exited with code 1",
        exit_code=1,
        stderr="database failed password=hunter2",
        failed_step_id="load",
    )

    # The canonical fields, and nothing wrapping them: this builds what a
    # record is made from, and the record writer makes the record.
    assert "error_message" not in payload
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


def test_run_app_operation_failure_records_error_and_reraises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A propagated failure is not reported here; the run completes against its STEP_FAILURE.

    run_app_operation re-raises, so the AppError boundary in app_runtime
    reports the error once (rule 79) -- this records the lifecycle only.
    """
    ctx = SimpleNamespace(log_file=str(tmp_path / "app.log"), app_name="data_engine")
    start_test_run(ctx)
    run_log = _log(ctx, tmp_path)

    def fail() -> None:
        raise ValueError("password=hunter2 failed")

    with caplog.at_level("ERROR"), pytest.raises(ValueError) as raised:
        lifecycle_run_app_operation(ctx, run_log, "load", fail)

    assert raised.value.args == ("password=hunter2 failed",)
    assert [r for r in caplog.records if r.levelname == "ERROR"] == []
    records = _read(Path(run_log.path()))
    by_type = {record["record_type"]: record for record in records}
    assert [record["record_type"] for record in records] == [
        "RUN_START",
        "STEP_FAILURE",
        "RUN_COMPLETE",
    ]
    failure = by_type["STEP_FAILURE"]["error_message"]
    assert failure["failed_step_id"] == "load"
    assert failure["error_type"] == "ValueError"
    assert "hunter2" not in json.dumps(by_type["STEP_FAILURE"])
    assert by_type["RUN_COMPLETE"]["status"] == "failed"
    assert by_type["RUN_COMPLETE"]["failure_record_id"] == failure["failure_record_id"]
    assert not any(record["record_type"] == "RESULTS_SUMMARY" for record in records)


def test_run_app_operation_nonzero_result_records_failed_lifecycle(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A nonzero integer return is failed evidence but still returned unchanged."""
    from rey_lib.errors.error_utils import AppError

    ctx = SimpleNamespace(log_file=str(tmp_path / "app.log"), app_name="data_engine")
    start_test_run(ctx)
    run_log = _log(ctx, tmp_path)

    with caplog.at_level("ERROR"):
        result = lifecycle_run_app_operation(ctx, run_log, "redact", lambda: 1)

    assert result == 1
    logged = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(logged) == 1 and isinstance(logged[0].exc_info[1], AppError)
    records = _read(Path(run_log.path()))
    by_type = {record["record_type"]: record for record in records}
    assert [record["record_type"] for record in records] == [
        "RUN_START",
        "STEP_FAILURE",
        "RUN_COMPLETE",
    ]
    failure = by_type["STEP_FAILURE"]["error_message"]
    assert failure["error_type"] == "AppOperationFailed"
    assert by_type["RUN_COMPLETE"]["status"] == "failed"
    assert by_type["RUN_COMPLETE"]["failure_record_id"] == failure["failure_record_id"]
    assert not any(record["record_type"] == "RESULTS_SUMMARY" for record in records)


def test_a_run_log_without_jsonl_has_no_path(tmp_path: Path) -> None:
    """With JSONL not selected there is no JSONL path; none is manufactured."""
    from tests.conftest import make_run_log

    assert make_run_log(tmp_path, destination="db").path() is None


def test_record_append_is_fail_safe(tmp_path: Path) -> None:
    """A record whose value is not JSON-serialisable is still written (default=str)."""
    ctx = _ctx(tmp_path)
    run_log = _log(ctx, tmp_path)
    log_run_start(run_log, weird=object())
    records = _read(Path(run_log.path()))
    assert records[0]["record_type"] == "RUN_START"


def test_typed_records_inherit_step_and_correlation_context(tmp_path: Path) -> None:
    """All typed helpers flow through the shared enrichment path."""
    ctx = _ctx(tmp_path)
    run_log = _log(ctx, tmp_path)
    bind_step(step_id="prepare", step_name="Prepare", step_sequence=2,
              app="data_engine", workflow_name="transform_load")
    bind_correlation("corr-1")
    try:
        log_input_discovered(run_log, input_name="trades", path=str(tmp_path / "trades.csv"),
            pattern="*.csv", source_config="source.trades", exists=True,
            safe_to_preview=True,
        )
    finally:
        clear_correlation()
        clear_step()

    record = _read(Path(run_log.path()))[0]
    assert record["record_type"] == "INPUT_DISCOVERED"
    assert record["record_group"] == "files"
    assert record["record_subgroup"] == "input_files"
    assert record["step_id"] == "prepare"
    assert record["step_name"] == "Prepare"
    assert record["step_sequence"] == 2
    assert record["correlation_id"] == "corr-1"
    assert record["app"] == "data_engine"


def test_write_side_sanitizer_masks_secret_like_keys(tmp_path: Path) -> None:
    """Secret-like helper fields are sanitized before JSONL persistence."""
    ctx = _ctx(tmp_path)
    run_log = _log(ctx, tmp_path)
    log_app_execution(run_log,
        app="data_engine",
        entrypoint="python -m rey_loader",
        arguments_redacted=["--config-path", "safe.yaml"],
        working_directory=str(tmp_path),
        status="started",
        metadata={"api_key": "abc", "nested": {"password": "pw"}, "safe": "ok"},
    )

    record = _read(Path(run_log.path()))[0]
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
    run_log = _log(ctx, tmp_path)
    with pytest.raises(DataStructureError, match="requires structured failure evidence"):
        log_run_complete(run_log, "failed")


def test_step_failure_returns_record_id_for_failed_run_complete(tmp_path: Path) -> None:
    """A step failure creates failure evidence that completion then references."""
    ctx = _ctx(tmp_path)
    run_log = _log(ctx, tmp_path)
    failure_id = log_step_failure(run_log,
        failed_step_id="load",
        failed_step_name="Load",
        message="loader failed",
        error_type="RuntimeError",
        sanitized_exception="RuntimeError: loader failed",
        exit_code=1,
    )
    log_run_complete(run_log,
        "failed",
        failure_record_id=failure_id,
        failed_step_id="load",
        failed_step_name="Load",
        failure_message="loader failed",
    )

    records = _read(Path(run_log.path()))
    assert records[0]["record_type"] == "STEP_FAILURE"
    assert records[0]["error_message"]["failure_record_id"] == failure_id
    assert records[1]["record_type"] == "RUN_COMPLETE"
    assert records[1]["failure_record_id"] == failure_id


def test_new_event_helpers_emit_approved_record_types(tmp_path: Path) -> None:
    """Phase 2 helpers emit narrow event semantics through log_run_record."""
    ctx = _ctx(tmp_path)
    run_log = _log(ctx, tmp_path)
    log_sql_execution(run_log, connection_name="local", database="db",
                      sql_path=str(tmp_path / "apply.sql"), operation="apply",
                      status="success", duration_ms=12)
    log_row_count(run_log, count_name="loaded", count=10, subject="trades")
    log_validation_result(run_log, validation_name="headers", status="success")

    types = [record["record_type"] for record in _read(Path(run_log.path()))]
    assert types == ["SQL_EXECUTION", "ROW_COUNT", "VALIDATION_RESULT"]


def test_discover_runs_lists_newest_first_without_raw_data(tmp_path: Path) -> None:
    """discover_runs returns per-run summaries, newest first, with no raw records."""
    from rey_lib.logs import discover_runs

    for ts, status in [("20260706_090000", "success"), ("20260706_100000", "failed")]:
        (tmp_path / f"daily.{ts}.jsonl").write_text(
            "\n".join(json.dumps(record) for record in [
                {"record_type": "RUN_START", "run_id": f"r-{ts}", "run_timestamp": ts,
                 "timestamp": "2026-07-06T00:00:00+00:00", "app": "data_engine"},
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
    run_log = _log(ctx, tmp_path)
    report = tmp_path / "report.json"
    log_run_start(run_log)
    log_config_file_reference(run_log, str(tmp_path / "workflow.yaml"),
                              file_role="workflow_definition")
    log_artifact_reference(run_log, str(report), role="report", event="written")
    log_file_operation(run_log, "move", source_path=str(tmp_path / "a.csv"),
                       target_path=str(tmp_path / "b.csv"))
    log_run_complete(run_log, "success")
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

    with pytest.raises(ConfigError):
        get_run_section(path, "bogus")


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
    run_log = _log(ctx, tmp_path)
    log_run_start(run_log)
    log_input_file_reference(run_log, str(tmp_path / "incoming" / "file.csv"),
                             file_role="source_data", consumed_by_step="validate_header")
    log_config_file_reference(run_log, str(tmp_path / "workflow.yaml"),
                              file_role="workflow_definition")
    log_file_operation(run_log, "move", source_path=str(tmp_path / "inbox" / "file.csv"),
                       target_path=str(tmp_path / "processing" / "file.csv"),
                       step_id="move_file_to_processing")
    log_artifact_reference(run_log, str(tmp_path / "report.json"), role="report",
                           event="generated", created_by_step="lint_sql")
    log_run_summary(run_log, {"status": "success"})
    log_run_complete(run_log, "success")

    records = _read(Path(run_log.path()))
    by_type = {r["record_type"]: r for r in records}

    # Every record carries run identity.
    assert all(r["run_id"] == ctx.run_id for r in records)
    assert all(r["run_timestamp"] == ctx.run_timestamp for r in records)

    # Groups and subgroups per SGC_Rey_Log_Writer_Run_View_Groups.
    # Files are their own top-level group beside execution and results.
    assert by_type["FILE_OPERATION"]["record_group"] == "files"
    # And each files record names which kind of file evidence it is. The
    # subgroup is the map's, not a word chosen at the writer.
    assert by_type["FILE_OPERATION"]["record_subgroup"] == "file_operations"
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
    run_log = _log(ctx, tmp_path)
    config_path = tmp_path / "workflow.yaml"
    config_path.write_text("name: wf\n", encoding="utf-8")

    log_config_file_reference(run_log,
        str(config_path),
        config_name="workflow.yaml",
        config_type="workflow",
        exists=True,
        safe_to_preview=False,
        config_hash="abc123",
    )

    record = _read(Path(run_log.path()))[0]
    assert record["record_type"] == "CONFIG_FILE_REFERENCE"
    assert record["record_subgroup"] == "config_files"
    assert record["config_name"] == "workflow.yaml"
    assert record["config_type"] == "workflow"
    assert record["source"] == "config_provenance"
    assert record["exists"] is True
    assert record["safe_to_preview"] is False
    assert record["config_hash"] == "abc123"
    assert record["hash"] == "abc123"


def test_nested_app_operation_does_not_emit_restore_policy(tmp_path: Path) -> None:
    """A nested app operation never writes a RUN_RESTORE_POLICY (run owner only)."""
    ctx = SimpleNamespace(log_file=str(tmp_path / "app.log"), app_name="data_engine")
    start_test_run(ctx)
    run_log = _log(ctx, tmp_path)
    lifecycle_run_app_operation(ctx, run_log, "transform", lambda: 0)
    records = _read(Path(run_log.path()))
    assert records  # lifecycle records were written
    assert not any(r["record_type"] == "RUN_RESTORE_POLICY" for r in records)


def test_sql_execution_records_are_marked_sensitive(tmp_path: Path) -> None:
    """Every SQL_EXECUTION record, with no parameter for a caller to unset.

    The flag describes content, not the feature that wrote it -- the same SQL
    must not be sensitive in one path and ordinary in another.
    """
    from rey_lib.logs.jsonl_handler import SENSITIVE_FIELD
    from rey_lib.logs.sql_records import log_sql_execution

    ctx = _ctx(tmp_path)
    run_log = _log(ctx, tmp_path)
    log_run_start(run_log)
    log_sql_execution(
        run_log, connection_name="warehouse", operation="select",
        status="succeeded", sql_label="a label",
    )

    written = [r for r in _read(Path(run_log.path()))
               if r["record_type"] == "SQL_EXECUTION"]
    assert len(written) == 1
    assert written[0][SENSITIVE_FIELD] is True


def test_the_marked_record_is_written_whole(tmp_path: Path) -> None:
    """Classification, not suppression. Marking removes nothing."""
    from rey_lib.logs.jsonl_handler import SENSITIVE_FIELD
    from rey_lib.logs.sql_records import log_sql_execution

    ctx = _ctx(tmp_path)
    run_log = _log(ctx, tmp_path)
    log_run_start(run_log)
    log_sql_execution(
        run_log, connection_name="warehouse", operation="select",
        status="failed", error_message='column "salary" does not exist',
    )

    record = [r for r in _read(Path(run_log.path()))
              if r["record_type"] == "SQL_EXECUTION"][0]
    assert record[SENSITIVE_FIELD] is True
    # The driver's own wording is still there, for whoever may read it.
    assert "salary" in json.dumps(record)


def test_an_unmarked_record_type_says_nothing(tmp_path: Path) -> None:
    """A second record type, and the absence that matters.

    Unclassified is not "not sensitive": a record that never said carries no
    flag at all, and a reader must treat that conservatively.
    """
    from rey_lib.logs.jsonl_handler import SENSITIVE_FIELD

    ctx = _ctx(tmp_path)
    run_log = _log(ctx, tmp_path)
    log_run_start(run_log)
    log_execution_plan(run_log, total_steps=1, steps=[
        {"sequence": 1, "step_id": "a", "step_name": "a", "app": "tool"},
    ])

    plan = [r for r in _read(Path(run_log.path()))
            if r["record_type"] == "EXECUTION_PLAN"][0]
    assert SENSITIVE_FIELD not in plan


def test_the_flag_is_a_shared_fact_not_a_payload_key(tmp_path: Path) -> None:
    """It is queried across record types, so it cannot live inside whichever
    jsonb column a type happens to use."""
    from rey_lib.logs.jsonl_handler import SENSITIVE_FIELD
    from rey_lib.logs.run_log import _SHARED_FIELDS, TYPE_PAYLOAD_COLUMNS

    assert SENSITIVE_FIELD in _SHARED_FIELDS
    assert SENSITIVE_FIELD not in TYPE_PAYLOAD_COLUMNS.values()


def test_a_caller_may_classify_any_record(tmp_path: Path) -> None:
    """The second record type of the round trip, marked by its writer."""
    from rey_lib.logs.jsonl_handler import SENSITIVE_FIELD

    ctx = _ctx(tmp_path)
    run_log = _log(ctx, tmp_path)
    log_run_start(run_log)
    run_log.append("INFO", message="a note", info={"note": "x"},
                   **{SENSITIVE_FIELD: True})

    note = [r for r in _read(Path(run_log.path()))
            if r["record_type"] == "INFO"][0]
    assert note[SENSITIVE_FIELD] is True
    assert note["info"] == {"note": "x"}


def test_row_value_records_route_to_the_shared_column(tmp_path: Path) -> None:
    """Not into a payload blob: an access control asks every record type at
    once, which it cannot do if the answer is inside a per-type jsonb key."""
    from rey_lib.logs.jsonl_handler import SENSITIVE_FIELD
    from rey_lib.logs.run_log import _ENVELOPE_FIELDS, _SHARED_FIELDS, TYPE_PAYLOAD_COLUMNS

    assert SENSITIVE_FIELD in _SHARED_FIELDS
    assert SENSITIVE_FIELD not in _ENVELOPE_FIELDS
    assert SENSITIVE_FIELD not in TYPE_PAYLOAD_COLUMNS.values()
    # And the column exists to route to, named exactly as the field is.
    assert SENSITIVE_FIELD == "contains_sensitive_data"


@pytest.mark.parametrize("record_type", ["ERROR", "STEP_FAILURE", "error"])
def test_a_failure_record_payload_is_its_error_message(record_type: str) -> None:
    """The reader's answer for the two types the map leaves out on purpose."""
    from rey_lib.logs import payload_column

    assert payload_column(record_type) == "error_message"


def test_every_other_type_answers_the_maps_column() -> None:
    from rey_lib.logs import TYPE_PAYLOAD_COLUMNS, payload_column

    assert {kind: payload_column(kind) for kind in TYPE_PAYLOAD_COLUMNS} == TYPE_PAYLOAD_COLUMNS
    assert payload_column("MYSTERY") is None
