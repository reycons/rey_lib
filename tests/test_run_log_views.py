"""
Tests for human-readable run views rendered from the JSONL run log
(SGC_Rey_Log_Utils_JSONL_Only_Human_View_Cleanup).

Human-readable execution output is a projection of the append-only JSONL run
log, produced on demand by log_utils — never a second durable execution log.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from rey_lib.logs import (
    log_artifact_reference,
    log_config_file_reference,
    log_file_operation,
    log_run_complete,
    log_run_record,
    log_run_start,
    log_run_summary,
    log_step_end,
    log_step_start,
    render_error_warning_view,
    render_execution_view,
    render_files_view,
    render_results_view,
    render_run_view,
    render_summary_view,
)


def _run_log(tmp_path: Path) -> str:
    """Produce a small but representative run log and return its path."""
    ctx = SimpleNamespace(
        log_file=str(tmp_path / "app.run.jsonl"),
        owner_app_name="rey_db_admin",
        workflow_name="postgres_lint",
    )
    log_run_start(ctx)
    log_step_start(ctx, "export", 1)
    log_config_file_reference(ctx, str(tmp_path / "workflow.yaml"),
                              file_role="workflow_definition")
    log_artifact_reference(ctx, str(tmp_path / "lint_report.json"), role="lint_report",
                           event="written")
    log_file_operation(ctx, "move", source_path=str(tmp_path / "in.sql"),
                       target_path=str(tmp_path / "out.sql"))
    log_run_record(ctx, "WARNING", message="deprecated option used")
    log_step_end(ctx, "export", "success")
    log_run_summary(ctx, {"steps": 1, "status": "success"})
    log_run_complete(ctx, "success")
    return ctx.run_log_path


def test_render_run_view_includes_all_groups(tmp_path: Path) -> None:
    """The full run view renders header plus Execution, Files, and Results groups."""
    view = render_run_view(_run_log(tmp_path))
    assert "Run " in view
    assert "== Execution ==" in view
    assert "== Files ==" in view
    assert "== Results ==" in view
    # Files group surfaces the config file and artifact by display name.
    assert "workflow.yaml" in view
    assert "lint_report.json" in view


def test_render_execution_view_from_path_and_records(tmp_path: Path) -> None:
    """Execution view renders the audit trail and accepts a path or records."""
    path = _run_log(tmp_path)
    view = render_execution_view(path)
    assert "RUN_START" in view
    assert "STEP_START" in view
    # Accepts an already-read record list too.
    from rey_lib.logs import read_run_log_sections
    records = read_run_log_sections(path)["records"]
    assert render_execution_view(records) == view


def test_render_files_view_groups_files(tmp_path: Path) -> None:
    """Files view renders each subgroup, keeping moved files out of Artifacts."""
    view = render_files_view(_run_log(tmp_path))
    assert "Config Files (1)" in view
    assert "Artifacts (1)" in view
    assert "File Operations (1)" in view
    assert "Input Files (0)" in view
    # The moved file appears under File Operations, not Artifacts.
    assert "out.sql" in view
    assert "lint_report.json" in view


def test_render_results_and_summary_views(tmp_path: Path) -> None:
    """Results view shows RUN_SUMMARY; summary view renders the deterministic summary."""
    path = _run_log(tmp_path)
    assert "RUN_SUMMARY" in render_results_view(path)
    summary = render_summary_view(path)
    assert "Summary" in summary
    assert "status: success" in summary


def test_render_error_warning_view_filters(tmp_path: Path) -> None:
    """The error/warning view surfaces only WARNING/ERROR records."""
    view = render_error_warning_view(_run_log(tmp_path))
    assert "deprecated option used" in view
    assert "RUN_START" not in view


def test_error_warning_view_empty_when_clean(tmp_path: Path) -> None:
    """A clean run renders an explicit no-warnings message."""
    ctx = SimpleNamespace(log_file=str(tmp_path / "clean.run.jsonl"), owner_app_name="x")
    log_run_start(ctx)
    log_run_complete(ctx, "success")
    assert render_error_warning_view(ctx.run_log_path) == "(no warnings or errors)"
