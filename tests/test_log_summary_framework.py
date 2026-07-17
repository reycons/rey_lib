"""Behavior tests for explicit RESULTS_SUMMARY creation in the run JSONL."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.logs import (
    create_results_summary,
    log_artifact_manifest,
    log_error,
    log_file_operation,
    log_run_complete,
    log_run_start,
    log_step_end,
    log_step_failure,
    log_step_start,
    set_nest_level,
)
from rey_lib.logs.record_enrichment import log_run_record


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _ctx(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        log_file=str(tmp_path / "demo.log"),
        run_id="r1", run_timestamp="20260711_120000", owner_app_name="demo_app",
    )


def _completed_run(
    tmp_path: Path,
    status: str = "success",
    *,
    semantic_level: str = "app",
    complete: bool = True,
) -> tuple[SimpleNamespace, Path]:
    ctx = _ctx(tmp_path)
    set_nest_level(ctx, semantic_level)
    log_run_start(ctx, run_started_at="2026-07-11T12:00:00+00:00")
    log_step_start(ctx, "one", 1, step_id="one")
    if status == "failed":
        log_error(
            ctx, message="boom", error_type="RuntimeError", error_id="error-1",
        )
        log_step_failure(
            ctx, failed_step_id="one", failed_step_name="one", message="boom",
            error_type="RuntimeError", error_message="boom",
            failure_record_id="error-1",
        )
    log_step_end(ctx, "one", status, step_id="one", duration_ms=1200)
    if complete:
        complete_fields = {}
        if status == "failed":
            complete_fields = {
                "failure_record_id": "error-1", "failed_step_id": "one",
                "failed_step_name": "one", "failure_message": "boom",
            }
        log_run_complete(ctx, status, **complete_fields)
    return ctx, Path(ctx.run_log_path)


def test_results_summary_is_appended_to_completed_run_log(tmp_path: Path) -> None:
    ctx, log = _completed_run(tmp_path)
    result = create_results_summary(ctx)

    records = _records(log)
    assert result["action"] == "created"
    assert records[-1]["record_type"] == "RESULTS_SUMMARY"
    assert records[-1]["status"] == "success"
    assert records[-1]["run_id"] == "r1"
    assert records[-1]["record_id"] == records[-2]["record_id"] + 1
    assert records[-1]["parent_record_id"] == 0
    assert records[-1]["nest_level"] == 3


def test_results_summary_requires_terminal_run(tmp_path: Path) -> None:
    ctx, log = _completed_run(tmp_path, complete=False)
    result = create_results_summary(ctx)
    assert result["action"] is None
    assert result["skipped"] == ["no_terminal_record"]
    assert not any(record["record_type"] == "RESULTS_SUMMARY" for record in _records(log))


def test_failed_summary_preserves_failure_evidence(tmp_path: Path) -> None:
    ctx, log = _completed_run(tmp_path, "failed")
    create_results_summary(ctx)
    summary = _records(log)[-1]
    assert summary["record_type"] == "RESULTS_SUMMARY"
    assert summary["status"] == "failed"
    assert "error-1" in summary["diagnostics"]["failure_record_ids"]


def test_explicit_log_path_uses_authoritative_companion_state(tmp_path: Path) -> None:
    _, log = _completed_run(tmp_path)
    result = create_results_summary(log_path=log)
    assert result["action"] == "created"
    assert _records(log)[-1]["record_type"] == "RESULTS_SUMMARY"


def test_results_summary_payload_is_unchanged_apart_from_hierarchy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, log = _completed_run(tmp_path)
    captured: dict[str, dict] = {}
    from rey_lib.logs import summary as summary_module

    original = summary_module.build_results_summary

    def capture(*args: object, **kwargs: object) -> dict:
        built = original(*args, **kwargs)
        captured["built"] = deepcopy(built)
        return built

    monkeypatch.setattr(summary_module, "build_results_summary", capture)
    result = create_results_summary(ctx)
    emitted = dict(result["summary"])
    for field in ("record_id", "parent_record_id", "nest_level"):
        emitted.pop(field)
    assert emitted == captured["built"]
    assert _records(log)[-1] == result["summary"]


@pytest.mark.parametrize(
    ("semantic_level", "expected_nest"),
    (("pipeline", 1), ("app", 3)),
)
def test_results_summary_uses_active_scope(
    tmp_path: Path, semantic_level: str, expected_nest: int,
) -> None:
    ctx, _ = _completed_run(tmp_path, semantic_level=semantic_level)
    summary = create_results_summary(ctx)["summary"]
    assert summary["nest_level"] == expected_nest
    assert summary["parent_record_id"] == 0


def test_results_summary_uses_active_workflow_parentage(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    set_nest_level(ctx, "app")
    log_run_start(ctx, run_started_at="2026-07-11T12:00:00+00:00")
    app_record_id = _records(Path(ctx.run_log_path))[0]["record_id"]
    set_nest_level(ctx, "workflow")
    log_step_start(ctx, "one", 1, step_id="one")
    log_step_end(ctx, "one", "success", step_id="one")
    log_run_complete(ctx, "success")

    summary = create_results_summary(ctx)["summary"]
    assert summary["nest_level"] == 4
    assert summary["parent_record_id"] == app_record_id


def test_durable_result_record_matrix_preserves_hierarchy_invariant(
    tmp_path: Path,
) -> None:
    ctx, log = _completed_run(tmp_path, semantic_level="pipeline")
    assert create_results_summary(ctx)["action"] == "created"

    result_types = (
        "LLM_PACKAGE", "LLM_INTERPRETATION", "LLM_ANALYSIS_FAILURE",
        "DYNAMIC_CONFIGURED_RESULT", "DYNAMIC_CONFIGURED_FAILURE",
        "RUN_SUMMARY", "EMAIL_SUMMARY", "LLM_ANALYSIS_PACKAGE",
        "LLM_ANALYSIS_RESULT", "MANUAL_REVIEW", "POST_MORTEM",
    )
    for record_type in result_types:
        log_run_record(ctx, record_type, record_group="results", value=record_type)
    log_artifact_manifest(ctx, [])
    log_file_operation(ctx, "read", source_path=str(log))

    records = _records(log)
    ids = [record["record_id"] for record in records]
    assert ids == list(range(1, len(records) + 1))
    assert len(ids) == len(set(ids))
    preceding_ids: set[int] = set()
    for record in records:
        assert {"record_id", "parent_record_id", "nest_level"} <= record.keys()
        parent = record["parent_record_id"]
        assert parent == 0 or parent in preceding_ids
        preceding_ids.add(record["record_id"])
