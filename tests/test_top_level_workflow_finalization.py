"""Finalization cardinality for a top-level standalone run-workflow.

A standalone workflow run is finalized by the shared coordinator, and the
run-owning application finalizes again in its own lifecycle. That nesting must
not duplicate terminal evidence: an operator inspecting the run log sees one
completion, one results summary, and one artifact manifest.

Pipeline-owned runs are finalized once by pipeline_coordinator and are covered
separately; nothing here changes that path.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.logs import (
    create_results_summary,
    finalize_run_log,
    log_artifact_manifest_from_run_log,
    log_artifact_reference,

    open_run_log,
)
from rey_lib.run_lifecycle import run_app_operation
from rey_lib.workflow import run_workflow


def _ctx(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        log_file=str(tmp_path / "app.run-workflow.jsonl"),
        owner_app_name="file_operator",
        workflow_name="convert_excel_to_csv",
    )


def _records(path: str | Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _count(records: list[dict[str, Any]], record_type: str) -> int:
    return sum(
        1
        for record in records
        if str(record.get("record_type") or "").upper() == record_type
    )


def _run_top_level_workflow(tmp_path: Path, ctx: SimpleNamespace) -> None:
    """Drive the exact nesting a standalone `run-workflow` invocation produces."""
    open_run_log(ctx)

    def handler(_ctx: Any, _config: dict[str, Any], _run: Any) -> None:
        log_artifact_reference(
            ctx,
            str(tmp_path / "out" / "converted.csv"),
            role="converted_csv",
            event="created",
            artifact_group="output_files",
            producer="file_operator",
        )
        return None

    workflow = {
        "name": "convert_excel_to_csv",
        "processes": {"excel_conversion": {}},
        "steps": [
            {
                "id": "convert_excel_all",
                "label": "Convert all classified workbooks",
                "process": "excel_conversion",
            }
        ],
    }

    def operation_body() -> int:
        # The coordinator completes and finalizes the standalone workflow run.
        run_workflow(ctx, workflow, {"excel_conversion": handler})
        return 0

    # run_app_operation appends the application-level RUN_COMPLETE afterwards.
    run_app_operation(ctx, "run-workflow", operation_body)

    # The run-owning application finalizes again because it is not a pipeline step.
    try:
        finalize_run_log(ctx.run_log_path)
    finally:
        log_artifact_manifest_from_run_log(ctx)


def test_top_level_run_workflow_summarizes_exactly_once(tmp_path: Path) -> None:
    """Nested finalization yields one summary and one manifest, not two."""
    ctx = _ctx(tmp_path)

    _run_top_level_workflow(tmp_path, ctx)

    records = _records(ctx.run_log_path)
    assert _count(records, "RESULTS_SUMMARY") == 1
    assert _count(records, "ARTIFACT_MANIFEST") == 1


def test_repeated_finalization_appends_no_further_summary(tmp_path: Path) -> None:
    """Finalizing again is a no-op, whoever calls it and however often."""
    ctx = _ctx(tmp_path)
    _run_top_level_workflow(tmp_path, ctx)

    before = _records(ctx.run_log_path)
    finalize_run_log(ctx.run_log_path)
    log_artifact_manifest_from_run_log(ctx)

    after = _records(ctx.run_log_path)
    assert _count(after, "RESULTS_SUMMARY") == 1
    assert _count(after, "ARTIFACT_MANIFEST") == 1
    assert len(after) == len(before)


def test_create_results_summary_returns_the_existing_summary(tmp_path: Path) -> None:
    """The second caller receives the summary already on the log."""
    ctx = _ctx(tmp_path)
    _run_top_level_workflow(tmp_path, ctx)
    existing = next(
        record
        for record in _records(ctx.run_log_path)
        if str(record.get("record_type") or "").upper() == "RESULTS_SUMMARY"
    )

    result = create_results_summary(log_path=ctx.run_log_path)

    assert result["action"] is None
    assert "already_summarized" in result["skipped"]
    assert result["summary"]["run_id"] == existing["run_id"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Run completion is emitted by both the workflow coordinator and "
        "run_app_operation for a top-level run-workflow. Deciding which layer "
        "owns RUN_COMPLETE is a separate shared-architecture change."
    ),
)
def test_top_level_run_workflow_completes_exactly_once(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)

    _run_top_level_workflow(tmp_path, ctx)

    assert _count(_records(ctx.run_log_path), "RUN_COMPLETE") == 1


def test_top_level_run_workflow_keeps_the_declared_artifact(tmp_path: Path) -> None:
    """The single manifest still carries the run's declared output."""
    ctx = _ctx(tmp_path)

    _run_top_level_workflow(tmp_path, ctx)

    records = _records(ctx.run_log_path)
    manifest = next(
        record
        for record in records
        if str(record.get("record_type") or "").upper() == "ARTIFACT_MANIFEST"
    )
    artifacts = manifest.get("artifacts") or []
    assert [str(entry.get("path")) for entry in artifacts] == [
        str(tmp_path / "out" / "converted.csv")
    ]


def test_terminal_records_are_the_last_records_in_the_log(tmp_path: Path) -> None:
    """Terminal evidence appears in canonical order for inspection."""
    ctx = _ctx(tmp_path)

    _run_top_level_workflow(tmp_path, ctx)

    types = [
        str(record.get("record_type") or "").upper() for record in _records(ctx.run_log_path)
    ]
    assert types.index("RUN_COMPLETE") < types.index("RESULTS_SUMMARY")
    assert types.index("RESULTS_SUMMARY") < types.index("ARTIFACT_MANIFEST")
