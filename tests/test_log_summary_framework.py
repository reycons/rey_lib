"""Tests for the run-results finalization framework and RESULTS_SUMMARY builder.

Increment 2: schema boundary, .results.json file output, migration from RUN_SUMMARY,
idempotency/determinism, and successful/failed-run behavior
(SGC_Rey_Lib_Results_Summary_Diagnostic_Package_Correction)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from rey_lib.logs import finalize_run_log


def _write_log(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _ctx(path: Path) -> SimpleNamespace:
    return SimpleNamespace(run_log_path=str(path), run_id="r1",
                           run_timestamp="20260711_120000", owner_app_name="demo_app")


def _results_path(log: Path) -> Path:
    return log.parent / (log.stem + ".results.json")


def _completed_records(status: str = "success") -> list[dict]:
    return [
        {"record_type": "RUN_START", "record_group": "execution", "run_id": "r1",
         "run_timestamp": "20260711_120000", "run_started_at": "2026-07-11T12:00:00+00:00",
         "pipeline": "daily", "app": "pipeline_coordinator"},
        {"record_type": "STEP_START", "record_group": "execution", "run_id": "r1",
         "step_id": "one", "step_name": "one", "step_sequence": 1},
        {"record_type": "STEP_END", "record_group": "execution", "run_id": "r1",
         "step_name": "one", "status": "success", "duration_ms": 1200},
        {"record_type": "RUN_COMPLETE", "record_group": "execution", "run_id": "r1",
         "status": status, "timestamp": "2026-07-11T12:00:03+00:00"},
    ]


def _doc(log: Path) -> dict:
    return json.loads(_results_path(log).read_text(encoding="utf-8"))


def test_results_summary_success_run(tmp_path: Path) -> None:
    """A successful run writes a RESULTS_SUMMARY .results.json with full accounting."""
    log = tmp_path / "daily.20260711_120000.jsonl"
    _write_log(log, _completed_records())
    result = finalize_run_log(_ctx(log))
    assert result["results_written"] is True
    assert result["results_path"] == str(_results_path(log))
    doc = _doc(log)
    assert doc["record_type"] == "RESULTS_SUMMARY" and doc["record_schema_version"] == 1
    assert doc["status"] == "success" and doc["execution"]["outcome"] == "success"
    assert doc["run"]["steps_total"] == 1 and doc["run"]["steps_succeeded"] == 1
    assert doc["run"]["duration_ms"] == 3000
    assert [s["step_id"] for s in doc["step_results"]] == ["one"]
    # A successful run needs no failure fields populated.
    assert doc["execution"]["failed_step_ids"] == []
    assert doc["diagnostics"]["full_error_output"] == ""


def test_results_summary_is_a_projection_not_a_jsonl_record(tmp_path: Path) -> None:
    """RESULTS_SUMMARY is a separate .results.json file, not appended to the JSONL."""
    log = tmp_path / "daily.20260711_120000.jsonl"
    original = log.read_text if log.exists() else None
    _write_log(log, _completed_records())
    before_types = [json.loads(l)["record_type"] for l in log.read_text().splitlines() if l.strip()]
    finalize_run_log(_ctx(log))
    after_types = [json.loads(l)["record_type"] for l in log.read_text().splitlines() if l.strip()]
    # The execution JSONL is unchanged; no RESULTS_SUMMARY / RUN_SUMMARY record added.
    assert after_types == before_types
    assert "RESULTS_SUMMARY" not in after_types and "RUN_SUMMARY" not in after_types
    assert _results_path(log).exists()


def test_results_summary_requires_terminal_run(tmp_path: Path) -> None:
    """An incomplete log (no RUN_COMPLETE) is not finalized."""
    log = tmp_path / "daily.20260711_120000.jsonl"
    _write_log(log, _completed_records()[:-1])
    result = finalize_run_log(_ctx(log))
    assert result["results_written"] is False
    assert result["skipped"] == ["no_terminal_record"]
    assert not _results_path(log).exists()


def test_results_summary_is_deterministic(tmp_path: Path) -> None:
    """Identical JSONL input produces identical content except the summary timestamp."""
    log = tmp_path / "daily.20260711_120000.jsonl"
    _write_log(log, _completed_records())
    finalize_run_log(_ctx(log))
    first = _doc(log)
    finalize_run_log(_ctx(log))
    second = _doc(log)
    first.pop("timestamp"), second.pop("timestamp")
    assert first == second


def test_results_summary_failed_run(tmp_path: Path) -> None:
    """A failed run records outcome, failed step, and full error output."""
    log = tmp_path / "daily.20260711_120000.jsonl"
    _write_log(log, [
        _completed_records()[0],
        {"record_type": "STEP_START", "record_group": "execution", "run_id": "r1",
         "step_id": "a", "step_name": "a", "step_sequence": 1},
        {"record_type": "STEP_END", "record_group": "execution", "run_id": "r1",
         "step_name": "a", "status": "success", "duration_ms": 100},
        {"record_type": "STEP_START", "record_group": "execution", "run_id": "r1",
         "step_id": "b", "step_name": "b", "step_sequence": 2},
        {"record_type": "ERROR", "record_group": "execution", "run_id": "r1",
         "error_id": "e1", "error_message": "boom", "stderr_summary": "Traceback: boom line 1"},
        {"record_type": "STEP_FAILURE", "record_group": "execution", "run_id": "r1",
         "failed_step_id": "b", "failure_record_id": "f1"},
        {"record_type": "STEP_END", "record_group": "execution", "run_id": "r1",
         "step_name": "b", "status": "failed", "duration_ms": 200},
        {"record_type": "RUN_COMPLETE", "record_group": "execution", "run_id": "r1",
         "status": "failed", "failed_step_id": "b", "failed_step_name": "b",
         "timestamp": "2026-07-11T12:00:03+00:00"},
    ])
    finalize_run_log(_ctx(log))
    doc = _doc(log)
    assert doc["status"] == "failed"
    assert doc["execution"]["outcome"] == "partial_failure"
    assert doc["execution"]["partial_success"] is True
    assert doc["execution"]["failed_step_ids"] == ["b"]
    assert doc["diagnostics"]["failed_step_id"] == "b"
    assert doc["diagnostics"]["failure_record_ids"] == ["f1"]
    assert "Traceback: boom line 1" in doc["diagnostics"]["full_error_output"]


def test_results_summary_preserves_full_error_output_verbatim(tmp_path: Path) -> None:
    """Large multi-line error output is preserved byte-for-byte (no sanitize/truncate)."""
    log = tmp_path / "daily.20260711_120000.jsonl"
    big = "\n".join(f"2026-07-11 09:49:{i:02d} ERROR line {i} password=hunter2" for i in range(60))
    _write_log(log, [
        _completed_records()[0],
        {"record_type": "ERROR", "record_group": "execution", "run_id": "r1",
         "error_id": "e1", "full_error_output": big},
        {"record_type": "RUN_COMPLETE", "record_group": "execution", "run_id": "r1",
         "status": "failed", "timestamp": "2026-07-11T12:00:03+00:00"},
    ])
    finalize_run_log(_ctx(log))
    doc = _doc(log)
    # Verbatim: every line preserved, not sanitized (this increment adds no redaction).
    # The assembled string is now labelled by stream, so the payload is contained
    # within it, while the block carries the untouched byte-for-byte text.
    assert big in doc["diagnostics"]["full_error_output"]
    block = doc["diagnostics"]["error_blocks"][0]
    assert block["text"] == big
    assert block["truncated"] is False
    assert doc["diagnostics"]["error_output_truncated"] is False


def test_results_summary_marks_upstream_truncation_honestly(tmp_path: Path) -> None:
    """When a source record was already truncated, the summary reports it, not completeness."""
    log = tmp_path / "daily.20260711_120000.jsonl"
    _write_log(log, [
        _completed_records()[0],
        {"record_type": "ERROR", "record_group": "execution", "run_id": "r1",
         "record_id": "e1", "stderr_summary": "...cut", "output_truncated": True},
        {"record_type": "RUN_COMPLETE", "record_group": "execution", "run_id": "r1",
         "status": "failed", "timestamp": "2026-07-11T12:00:03+00:00"},
    ])
    finalize_run_log(_ctx(log))
    d = _doc(log)["diagnostics"]
    assert d["error_output_truncated"] is True
    assert d["truncated_source_record_ids"] == ["e1"]


def test_results_summary_dedupes_repeated_subprocess_transcript(tmp_path: Path) -> None:
    """The same stderr copied across records is retained once; provenance stays honest."""
    log = tmp_path / "daily.20260711_120000.jsonl"
    transcript = "Traceback (most recent call last):\n  File a.py\nRuntimeError: boom"
    _write_log(log, [
        _completed_records()[0],
        {"record_type": "STEP_START", "record_group": "execution", "run_id": "r1",
         "step_id": "b", "step_name": "b", "step_sequence": 1},
        {"record_type": "APP_EXECUTION", "record_group": "execution", "run_id": "r1",
         "record_id": "a1", "failed_step_id": "b", "stderr_summary": transcript},
        {"record_type": "ERROR", "record_group": "execution", "run_id": "r1",
         "error_id": "e1", "sanitized_exception": transcript,
         "error_message": "step b aborted"},
        {"record_type": "STEP_FAILURE", "record_group": "execution", "run_id": "r1",
         "record_id": "s1", "failed_step_id": "b", "failure_record_id": "e1",
         "sanitized_exception": transcript},
        {"record_type": "RUN_COMPLETE", "record_group": "execution", "run_id": "r1",
         "status": "failed", "failed_step_id": "b", "failed_step_name": "b",
         "timestamp": "2026-07-11T12:00:03+00:00"},
    ])
    finalize_run_log(_ctx(log))
    d = _doc(log)["diagnostics"]
    # The transcript appears three times; only the highest-precedence (stderr) survives.
    transcript_blocks = [b for b in d["error_blocks"] if b["text"] == transcript]
    assert len(transcript_blocks) == 1
    assert transcript_blocks[0]["stream"] == "stderr"
    assert d["full_error_output"].count(transcript) == 1
    assert d["error_statistics"]["duplicate_blocks_removed"] == 2
    # STEP_FAILURE correlates to ERROR e1 via failure_record_id -> one logical failure.
    assert d["error_statistics"]["logical_failures"] == 1
    # A message that is not contained in the transcript is preserved as its own block.
    assert any(b["text"] == "step b aborted" for b in d["error_blocks"])


def test_results_summary_keeps_streams_labelled_and_distinct(tmp_path: Path) -> None:
    """stdout, stderr, structured errors, and tracebacks stay separated and labelled."""
    log = tmp_path / "daily.20260711_120000.jsonl"
    _write_log(log, [
        _completed_records()[0],
        {"record_type": "APP_EXECUTION", "record_group": "execution", "run_id": "r1",
         "record_id": "a1", "stdout_summary": "wrote 3 rows", "stderr_summary": "disk warn"},
        {"record_type": "ERROR", "record_group": "execution", "run_id": "r1",
         "error_id": "e1", "error_message": "load failed",
         "sanitized_traceback": "Traceback:\n  ValueError: bad"},
        {"record_type": "RUN_COMPLETE", "record_group": "execution", "run_id": "r1",
         "status": "failed", "timestamp": "2026-07-11T12:00:03+00:00"},
    ])
    finalize_run_log(_ctx(log))
    d = _doc(log)["diagnostics"]
    out = d["full_error_output"]
    # Deterministic section order: stderr, stdout, structured error, traceback.
    assert out.index("===== STDERR =====") < out.index("===== STDOUT =====")
    assert out.index("===== STDOUT =====") < out.index("===== STRUCTURED ERROR =====")
    assert out.index("===== STRUCTURED ERROR =====") < out.index("===== TRACEBACK =====")
    stats = d["error_statistics"]
    assert (stats["stderr_blocks"], stats["stdout_blocks"]) == (1, 1)
    assert (stats["structured_blocks"], stats["traceback_blocks"]) == (1, 1)
    assert stats["duplicate_blocks_removed"] == 0


def test_results_summary_truncation_marker_detected_without_flag(tmp_path: Path) -> None:
    """The ...[truncated] marker alone marks the output truncated (no explicit flag)."""
    log = tmp_path / "daily.20260711_120000.jsonl"
    _write_log(log, [
        _completed_records()[0],
        {"record_type": "ERROR", "record_group": "execution", "run_id": "r1",
         "record_id": "e1", "stderr_summary": "line1\nline2...[truncated]"},
        {"record_type": "RUN_COMPLETE", "record_group": "execution", "run_id": "r1",
         "status": "failed", "timestamp": "2026-07-11T12:00:03+00:00"},
    ])
    finalize_run_log(_ctx(log))
    d = _doc(log)["diagnostics"]
    assert d["error_output_truncated"] is True
    assert d["truncated_source_record_ids"] == ["e1"]
    assert d["error_blocks"][0]["truncated"] is True


def test_results_summary_no_error_text_reports_none_recorded(tmp_path: Path) -> None:
    """A failed run with no error text is honest: empty output, none_recorded source."""
    log = tmp_path / "daily.20260711_120000.jsonl"
    _write_log(log, [
        _completed_records()[0],
        {"record_type": "STEP_FAILURE", "record_group": "execution", "run_id": "r1",
         "failed_step_id": "b", "failure_record_id": "f1"},
        {"record_type": "RUN_COMPLETE", "record_group": "execution", "run_id": "r1",
         "status": "failed", "failed_step_id": "b", "timestamp": "2026-07-11T12:00:03+00:00"},
    ])
    finalize_run_log(_ctx(log))
    d = _doc(log)["diagnostics"]
    assert d["full_error_output"] == ""
    assert d["error_blocks"] == []
    assert d["error_output_source"] == "none_recorded"
    assert d["error_statistics"]["logical_failures"] == 1


def test_results_summary_conflicting_evidence_is_preserved(tmp_path: Path) -> None:
    """Two genuinely different stderr captures are both kept (no merge, no pick)."""
    log = tmp_path / "daily.20260711_120000.jsonl"
    _write_log(log, [
        _completed_records()[0],
        {"record_type": "APP_EXECUTION", "record_group": "execution", "run_id": "r1",
         "record_id": "a1", "stderr_summary": "connection refused"},
        {"record_type": "APP_EXECUTION", "record_group": "execution", "run_id": "r1",
         "record_id": "a2", "stderr_summary": "permission denied"},
        {"record_type": "RUN_COMPLETE", "record_group": "execution", "run_id": "r1",
         "status": "failed", "timestamp": "2026-07-11T12:00:03+00:00"},
    ])
    finalize_run_log(_ctx(log))
    d = _doc(log)["diagnostics"]
    texts = [b["text"] for b in d["error_blocks"]]
    assert "connection refused" in texts
    assert "permission denied" in texts
    assert d["error_statistics"]["duplicate_blocks_removed"] == 0


def test_results_summary_step_results_enriched_by_execution_details(tmp_path: Path) -> None:
    """execution_details enriches step_results with app/exit_code without fabrication."""
    log = tmp_path / "daily.20260711_120000.jsonl"
    _write_log(log, _completed_records())
    details = {"kind": "pipeline", "pipeline": {"mode": "full", "aborted": False,
               "invoked_apps": ["rey_loader"],
               "steps": [{"name": "one", "app": "rey_loader", "status": "success",
                          "exit_code": 0, "finalizer": False}]}}
    finalize_run_log(_ctx(log), execution_details=details)
    step = _doc(log)["step_results"][0]
    assert step["app"] == "rey_loader" and step["exit_code"] == 0
    assert step["step_sequence"] == 1 and step["duration_ms"] == 1200


# --- Increment 3: item / artifact / warning correlation ----------------------

def _analyzer_run(tmp_path: Path) -> Path:
    """A pipeline log with an analyzer step: v01 fails (no output), v02 succeeds."""
    v01, v02 = "/in/fidelity_v01.profile.json", "/in/fidelity_v02.profile.json"
    out02 = "/out/fidelity_v02.rey_loader.yaml"
    log = tmp_path / "trade.20260711_094744.jsonl"
    _write_log(log, [
        {"record_type": "RUN_START", "record_group": "execution", "run_id": "r1",
         "run_timestamp": "20260711_094744", "run_started_at": "2026-07-11T09:47:44+00:00",
         "pipeline": "trade_analyzer_generate_apply_ddl", "app": "pipeline_coordinator"},
        {"record_type": "STEP_START", "record_group": "execution", "run_id": "r1",
         "step_id": "generate_staging_load_config", "step_name": "generate_staging_load_config",
         "step_sequence": 6},
        {"record_type": "INPUT_FILE_REFERENCE", "record_group": "files", "run_id": "r1",
         "record_subgroup": "input_files", "path": v01, "display_name": "fidelity_v01.profile.json",
         "file_role": "analysis_input", "source_name": "fidelity", "analysis_name": "loader"},
        {"record_type": "INPUT_FILE_REFERENCE", "record_group": "files", "run_id": "r1",
         "record_subgroup": "input_files", "path": v02, "display_name": "fidelity_v02.profile.json",
         "file_role": "analysis_input", "source_name": "fidelity", "analysis_name": "loader"},
        {"record_type": "WARNING", "record_group": "execution", "run_id": "r1",
         "message": "LLM response extraction failed", "source_file": v01},
        {"record_type": "WARNING", "record_group": "execution", "run_id": "r1",
         "message": "generic warning with no file key"},
        {"record_type": "VALIDATION_RESULT", "record_group": "execution", "run_id": "r1",
         "validation_name": "analysis_result", "status": "failed", "input_file": v01},
        {"record_type": "ARTIFACT_REFERENCE", "record_group": "files", "run_id": "r1",
         "record_subgroup": "artifacts", "event": "written", "path": "/out/v02.result.json",
         "artifact_type": "analysis_result", "producer": "analyzer", "source_path": v02,
         "producing_step": "generate_staging_load_config"},
        {"record_type": "ARTIFACT_REFERENCE", "record_group": "files", "run_id": "r1",
         "record_subgroup": "artifacts", "event": "written", "path": "/out/v02.context.json",
         "artifact_type": "analysis_context", "producer": "analyzer", "source_path": v02},
        {"record_type": "ARTIFACT_REFERENCE", "record_group": "files", "run_id": "r1",
         "record_subgroup": "artifacts", "event": "written", "path": out02,
         "artifact_type": "llm_result", "role": "raw_output", "producer": "llm", "source_path": v02,
         "producing_step": "generate_staging_load_config"},
        {"record_type": "VALIDATION_RESULT", "record_group": "execution", "run_id": "r1",
         "validation_name": "analysis_result", "status": "success", "input_file": v02},
        {"record_type": "STEP_FAILURE", "record_group": "execution", "run_id": "r1",
         "failed_step_id": "generate_staging_load_config", "failure_record_id": "f1"},
        {"record_type": "STEP_END", "record_group": "execution", "run_id": "r1",
         "step_name": "generate_staging_load_config", "status": "failed", "duration_ms": 111024},
        {"record_type": "RUN_COMPLETE", "record_group": "execution", "run_id": "r1",
         "status": "failed", "failed_step_id": "generate_staging_load_config",
         "timestamp": "2026-07-11T09:49:00+00:00"},
    ])
    return log


def test_results_summary_item_results_and_partial_success(tmp_path: Path) -> None:
    """item_results are keyed by input; failed step keeps a successful sibling item."""
    log = _analyzer_run(tmp_path)
    finalize_run_log(_ctx(log))
    doc = _doc(log)
    items = {i["input_path"]: i for i in doc["item_results"]}
    assert set(items) == {"/in/fidelity_v01.profile.json", "/in/fidelity_v02.profile.json"}
    assert items["/in/fidelity_v01.profile.json"]["status"] == "failed"
    assert items["/in/fidelity_v02.profile.json"]["status"] == "success"
    # A failed step containing a successful item is a partial failure.
    assert doc["execution"]["partial_success"] is True


def test_results_summary_links_result_context_output_by_source_path(tmp_path: Path) -> None:
    """Result/context/output artifacts link to their item by exact source_path."""
    log = _analyzer_run(tmp_path)
    finalize_run_log(_ctx(log))
    v02 = next(i for i in _doc(log)["item_results"] if i["input_path"].endswith("v02.profile.json"))
    assert v02["result_path"] == "/out/v02.result.json"
    assert v02["context_path"] == "/out/v02.context.json"
    assert v02["output_path"] == "/out/fidelity_v02.rey_loader.yaml"
    assert v02["output_created"] is True
    assert v02["producing_step"] == "generate_staging_load_config"
    assert v02["lineage_resolved"] is True


def test_results_summary_expected_but_missing_output_is_honest(tmp_path: Path) -> None:
    """A failed item with no output is output_created:false and expected_output_known:false."""
    log = _analyzer_run(tmp_path)
    finalize_run_log(_ctx(log))
    doc = _doc(log)
    v01 = next(i for i in doc["item_results"] if i["input_path"].endswith("v01.profile.json"))
    assert v01["output_created"] is False
    assert "expected_output" not in v01  # name not in evidence -> not fabricated
    missing = doc["artifacts"]["failed_or_missing"]
    assert any(m["source_path"] == "/in/fidelity_v01.profile.json"
               and m["expected_output_known"] is False and m["status"] == "missing"
               for m in missing)


def test_results_summary_artifact_grouping_and_lineage(tmp_path: Path) -> None:
    """Artifacts group into inputs/created with source_path lineage and producing app/step."""
    log = _analyzer_run(tmp_path)
    finalize_run_log(_ctx(log))
    art = _doc(log)["artifacts"]
    assert {a["path"] for a in art["inputs"]} == {
        "/in/fidelity_v01.profile.json", "/in/fidelity_v02.profile.json"}
    created = {a["path"]: a for a in art["created"]}
    assert "/out/v02.result.json" in created
    result = created["/out/v02.result.json"]
    assert result["source_path"] == "/in/fidelity_v02.profile.json"
    assert result["producing_app"] == "analyzer"
    assert result["lineage_resolved"] is True
    assert result["status"]  # never blank


def test_results_summary_warning_attribution(tmp_path: Path) -> None:
    """A warning with a file key attaches to its item; a keyless one stays unattributed."""
    log = _analyzer_run(tmp_path)
    finalize_run_log(_ctx(log))
    doc = _doc(log)
    v01 = next(i for i in doc["item_results"] if i["input_path"].endswith("v01.profile.json"))
    assert any(w["attributed"] is True for w in v01["warnings"])
    # The keyless warning stays at run level, explicitly unattributed.
    assert any(w["attributed"] is False for w in doc["warnings"])


def test_results_summary_analysis_id_absent_without_structured_field(tmp_path: Path) -> None:
    """analysis_id is not fabricated when no record carries it."""
    log = _analyzer_run(tmp_path)
    finalize_run_log(_ctx(log))
    for item in _doc(log)["item_results"]:
        assert item["analysis_id_known"] is False
        assert "analysis_id" not in item


def test_results_summary_ambiguous_basename_marks_lineage_unresolved(tmp_path: Path) -> None:
    """Two inputs sharing a basename can't be resolved by basename -> lineage unresolved."""
    a, b = "/in/a/data.json", "/in/b/data.json"
    log = tmp_path / "trade.20260711_094744.jsonl"
    _write_log(log, [
        {"record_type": "RUN_START", "record_group": "execution", "run_id": "r1",
         "run_timestamp": "20260711_094744", "pipeline": "p", "app": "pipeline_coordinator"},
        {"record_type": "INPUT_FILE_REFERENCE", "record_group": "files", "run_id": "r1",
         "path": a, "file_role": "analysis_input"},
        {"record_type": "INPUT_FILE_REFERENCE", "record_group": "files", "run_id": "r1",
         "path": b, "file_role": "analysis_input"},
        # Validation references only the basename -> ambiguous, cannot attribute.
        {"record_type": "VALIDATION_RESULT", "record_group": "execution", "run_id": "r1",
         "validation_name": "analysis_result", "status": "success", "input_file": "data.json"},
        {"record_type": "RUN_COMPLETE", "record_group": "execution", "run_id": "r1",
         "status": "success", "timestamp": "2026-07-11T09:49:00+00:00"},
    ])
    finalize_run_log(_ctx(log))
    items = _doc(log)["item_results"]
    # Both inputs present; neither got the ambiguous status; both lineage unresolved.
    assert {i["input_path"] for i in items} == {a, b}
    assert all(i["status"] == "unknown" and i["lineage_resolved"] is False for i in items)
