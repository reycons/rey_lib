"""Tests for the shared post-execution log-summary framework and run_summary builder
(SGC_Rey_Lib_Log_Summary_Framework_And_Run_Summary)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from rey_lib.logs import finalize_run_log
from rey_lib.logs import summary as summary_mod


def _write_log(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _ctx(path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        run_log_path=str(path),
        run_id="r1",
        run_timestamp="20260711_120000",
        owner_app_name="demo_app",
    )


def _completed_records(status: str = "success") -> list[dict]:
    return [
        {"record_type": "RUN_START", "record_group": "execution", "run_id": "r1",
         "run_timestamp": "20260711_120000", "run_started_at": "2026-07-11T12:00:00+00:00",
         "app": "demo_app"},
        {"record_type": "STEP_START", "record_group": "execution", "run_id": "r1",
         "step_name": "one"},
        {"record_type": "STEP_END", "record_group": "execution", "run_id": "r1",
         "step_name": "one", "status": "success"},
        {"record_type": "RUN_COMPLETE", "record_group": "execution", "run_id": "r1",
         "status": status, "timestamp": "2026-07-11T12:00:03+00:00"},
    ]


def _summaries(path: Path) -> list[dict]:
    return [json.loads(line)["summary"]
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("record_type") == "RUN_SUMMARY"]


def test_log_summary_framework_invokes_registered_builders(tmp_path: Path) -> None:
    """The framework dispatches builders and generates RUN_SUMMARY (TEST-001)."""
    log = tmp_path / "run.jsonl"
    _write_log(log, _completed_records())
    result = finalize_run_log(_ctx(log))
    assert result["builders_invoked"] == ["RUN_SUMMARY"]
    assert result["sections_generated"] == ["RUN_SUMMARY"]
    assert result["log_changed"] is True and result["records_appended"] == 1


def test_log_summary_framework_requires_terminal_run(tmp_path: Path) -> None:
    """An incomplete log (no RUN_COMPLETE) is not finalized (TEST-002)."""
    log = tmp_path / "run.jsonl"
    _write_log(log, _completed_records()[:-1])  # drop RUN_COMPLETE
    result = finalize_run_log(_ctx(log))
    assert result["log_changed"] is False
    assert result["sections_skipped"] == [{"section": "*", "reason": "no_terminal_record"}]
    assert not _summaries(log)


def test_log_summary_framework_appends_to_same_jsonl_log(tmp_path: Path) -> None:
    """The RUN_SUMMARY is appended to the same durable log (TEST-003)."""
    log = tmp_path / "run.jsonl"
    _write_log(log, _completed_records())
    finalize_run_log(_ctx(log))
    types = [json.loads(l)["record_type"] for l in log.read_text().splitlines() if l.strip()]
    assert types[-1] == "RUN_SUMMARY" and types[0] == "RUN_START"


def test_run_summary_derives_deterministic_authoritative_facts(tmp_path: Path) -> None:
    """Common fields come only from structured evidence (TEST-004/005)."""
    log = tmp_path / "run.jsonl"
    _write_log(log, _completed_records())
    finalize_run_log(_ctx(log))
    s = _summaries(log)[0]
    assert s["execution_kind"] == "app"
    assert s["status"] == "success"
    assert s["steps_total"] == 1 and s["steps_succeeded"] == 1
    assert s["elapsed_ms"] == 3000
    assert s["terminal_outcome"] == {"status": "success"}
    assert s["execution_details"] == {"kind": "app"}


def test_log_summary_framework_is_idempotent(tmp_path: Path) -> None:
    """Repeated finalization does not duplicate RUN_SUMMARY (TEST-006)."""
    log = tmp_path / "run.jsonl"
    _write_log(log, _completed_records())
    finalize_run_log(_ctx(log))
    second = finalize_run_log(_ctx(log))
    assert second["sections_already_present"] == ["RUN_SUMMARY"]
    assert second["log_changed"] is False
    assert len(_summaries(log)) == 1


def test_run_summary_handles_missing_optional_evidence(tmp_path: Path) -> None:
    """Absent steps/timestamps yield zeros/None, never invented values (TEST-007)."""
    log = tmp_path / "run.jsonl"
    _write_log(log, [
        {"record_type": "RUN_START", "record_group": "execution", "run_id": "r1",
         "run_timestamp": "20260711_120000"},
        {"record_type": "RUN_COMPLETE", "record_group": "execution", "run_id": "r1",
         "status": "success"},
    ])
    finalize_run_log(_ctx(log))
    s = _summaries(log)[0]
    assert s["steps_total"] == 0 and s["failed_step_ids"] == []
    assert s["elapsed_ms"] is None


def test_run_summary_sanitizes_sensitive_values(tmp_path: Path) -> None:
    """Secret-keyed execution_details values are redacted in the record (TEST-008)."""
    log = tmp_path / "run.jsonl"
    _write_log(log, _completed_records())
    details = {"kind": "workflow", "workflow": {"password": "hunter2", "steps": []}}
    finalize_run_log(_ctx(log), execution_details=details)
    raw = log.read_text(encoding="utf-8")
    assert "hunter2" not in raw
    s = _summaries(log)[0]
    assert s["execution_details"]["workflow"]["password"] == "[REDACTED]"


def test_summary_append_failure_preserves_original_log(tmp_path: Path, monkeypatch) -> None:
    """A builder failure preserves the log and is reported (TEST-009)."""
    log = tmp_path / "run.jsonl"
    _write_log(log, _completed_records())
    before = log.read_text(encoding="utf-8")

    def _boom(ctx, summary):
        raise RuntimeError("append failed")

    monkeypatch.setattr(summary_mod, "log_run_summary", _boom)
    result = finalize_run_log(_ctx(log))
    assert result["failures"] and result["failures"][0]["section"] == "RUN_SUMMARY"
    assert result["log_changed"] is False
    assert log.read_text(encoding="utf-8") == before  # unchanged, still valid


def test_additional_summary_builder_requires_no_lifecycle_change(tmp_path: Path, monkeypatch) -> None:
    """A newly registered builder is invoked without any terminal-path change (TEST-010)."""
    log = tmp_path / "run.jsonl"
    _write_log(log, _completed_records())
    invoked: list[str] = []

    def _extra(ctx, *, sections, identity, records, execution_details):
        invoked.append("EMAIL_SUMMARY")

    monkeypatch.setattr(
        summary_mod, "_SUMMARY_BUILDERS",
        list(summary_mod._SUMMARY_BUILDERS) + [("EMAIL_SUMMARY", _extra)],
    )
    result = finalize_run_log(_ctx(log))
    assert "EMAIL_SUMMARY" in result["builders_invoked"]
    assert invoked == ["EMAIL_SUMMARY"]


def test_workflow_execution_details_matches_locked_contract(tmp_path: Path) -> None:
    """Workflow execution_details is namespaced with mode/selection/ordered steps (TEST-014)."""
    log = tmp_path / "run.jsonl"
    _write_log(log, _completed_records())
    details = {
        "kind": "workflow",
        "workflow": {
            "mode": "dry_run",
            "selection": {"only": None, "step": "b", "from_step": None, "to_step": None},
            "steps": [
                {"id": "a", "label": "A", "process": "p1", "status": "ok"},
                {"id": "b", "label": "B", "process": "p2", "status": "failed", "detail": "x"},
            ],
        },
    }
    finalize_run_log(_ctx(log), execution_details=details)
    ed = _summaries(log)[0]["execution_details"]
    assert ed["kind"] == "workflow"
    assert ed["workflow"]["mode"] == "dry_run"
    assert [s["id"] for s in ed["workflow"]["steps"]] == ["a", "b"]
    assert ed["workflow"]["selection"]["step"] == "b"


def test_execution_details_ordering_size_and_app_kind(tmp_path: Path) -> None:
    """Oversized step lists truncate+flag; a plain app emits kind=app (TEST-016)."""
    log = tmp_path / "run.jsonl"
    _write_log(log, _completed_records())
    big = {"kind": "pipeline", "pipeline": {
        "mode": "full", "aborted": False, "invoked_apps": ["x"],
        "steps": [{"name": f"s{i}", "app": "x", "status": "success", "finalizer": False}
                  for i in range(summary_mod._MAX_EMBEDDED_STEPS + 5)],
    }}
    finalize_run_log(_ctx(log), execution_details=big)
    ed = _summaries(log)[0]["execution_details"]
    assert "steps" not in ed["pipeline"]
    assert ed["pipeline"]["steps_truncated"] is True

    # A plain app (no execution_details) records kind=app with no domain object.
    log2 = tmp_path / "run2.jsonl"
    _write_log(log2, _completed_records())
    finalize_run_log(_ctx(log2))
    assert _summaries(log2)[0]["execution_details"] == {"kind": "app"}
