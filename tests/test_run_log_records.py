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
    log_artifact_reference,
    log_config_file_reference,
    log_file_operation,
    log_input_file_reference,
    log_run_complete,
    log_run_start,
    log_run_summary,
    log_step_end,
    log_step_start,
    open_run_log,
    project_run_log,
    read_run_log_sections,
)


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
    """The run log is run_log.<run_timestamp>.jsonl in the log directory."""
    ctx = _ctx(tmp_path)
    path = open_run_log(ctx)
    assert path.name == f"run_log.{ctx.run_timestamp}.jsonl"
    assert path.parent == tmp_path


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


def test_run_log_sections_project_execution_files_and_results(tmp_path: Path) -> None:
    """Run logs are projected into execution/files/results without exposing file content."""
    log_path = tmp_path / "run_log.20260706_091845.jsonl"
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
    log_path = tmp_path / "run_log.20260706_091845.jsonl"
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
    # The move is execution history, not an artifact.
    assert [f["display_name"] for f in sections["files"]["artifacts"]["files"]] == ["report.json"]
    # FILE_OPERATION also remains in the execution audit trail.
    exec_types = [r["record_type"] for r in sections["execution"]["records"]]
    assert "FILE_OPERATION" in exec_types
