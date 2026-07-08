"""
Tests for artifact evidence normalization
(SGC_Rey_Console_Run_Artifact_Evidence_And_File_Inspector, Phase 1).

normalize_artifacts turns grounded run-log records into producer-tagged
artifacts with movement lineage, related-record links, dedupe, redacted-output
preference, and secret redaction — from evidence only, never the filesystem.
"""

from __future__ import annotations

import json
from pathlib import Path

from rey_lib.logs import (
    group_artifacts_by_producer,
    log_artifact_reference,
    normalize_artifacts,
)


def _artifact(path: str, **fields):
    """Return an ARTIFACT_REFERENCE record."""
    return {"record_type": "ARTIFACT_REFERENCE", "event": "created", "path": path, **fields}


def _move(source: str, target: str):
    """Return a FILE_OPERATION move record."""
    return {"record_type": "FILE_OPERATION", "operation": "move",
            "source_path": source, "target_path": target, "status": "success"}


def test_parses_and_groups_by_producer() -> None:
    """Artifacts are parsed and grouped by their observed producer."""
    records = [
        _artifact("/a/customers.redacted.csv", producer="redactor",
                  artifact_type="redacted_file"),
        _artifact("/a/llm_result.md", producer="llm"),
        _artifact("/a/load_summary.json", producer="loader"),
    ]
    groups = group_artifacts_by_producer(normalize_artifacts(records))
    assert set(groups) == {"redactor", "llm", "loader"}
    assert [a["label"] for a in groups["redactor"]] == ["customers.redacted.csv"]
    assert groups["llm"][0]["path"] == "/a/llm_result.md"


def test_movement_events_update_current_path() -> None:
    """A moved artifact resolves to its final known location."""
    records = [
        _artifact("/inbox/a.csv", producer="loader"),
        _move("/inbox/a.csv", "/processing/a.csv"),
        _move("/processing/a.csv", "/done/a.csv"),
    ]
    artifact = normalize_artifacts(records)[0]
    assert artifact["path"] == "/inbox/a.csv"          # original creation path
    assert artifact["current_path"] == "/done/a.csv"   # final location via lineage


def test_destination_path_updates_current_path() -> None:
    """Lineage can use destination_path when target_path is not present."""
    records = [
        _artifact("/inbox/a.csv", producer="loader"),
        {
            "record_type": "FILE_OPERATION",
            "operation": "archive",
            "source_path": "/inbox/a.csv",
            "destination_path": "/archive/a.csv",
            "status": "success",
        },
    ]
    artifact = normalize_artifacts(records)[0]
    assert artifact["current_path"] == "/archive/a.csv"


def test_related_records_match_by_path_and_correlation_id() -> None:
    """Related records are grounded by path and correlation id, with source lines."""
    records = [
        _artifact("/a/out.csv", producer="loader", correlation_id="c1"),   # line 1
        _move("/a/out.csv", "/done/out.csv"),                              # line 2 (path)
        {"record_type": "STEP_END", "correlation_id": "c1"},               # line 3 (corr id)
        {"record_type": "STEP_END", "correlation_id": "other"},            # line 4 (no match)
    ]
    artifact = normalize_artifacts(records)[0]
    assert artifact["related_source_lines"] == [1, 2, 3]   # 1-based, line 4 excluded
    assert "c1" in artifact["related_log_record_ids"]


def test_duplicate_artifact_evidence_is_deduplicated() -> None:
    """Two records for the same file collapse into one merged artifact."""
    records = [
        _artifact("/a/report.json", producer="analyzer"),
        _artifact("/a/report.json", producer="analyzer", artifact_type="analysis_result"),
    ]
    artifacts = normalize_artifacts(records)
    assert len(artifacts) == 1
    assert artifacts[0]["artifact_type"] == "analysis_result"   # richer field merged in


def test_secrets_are_redacted_in_metadata() -> None:
    """Secret-like metadata values are masked in the outward package."""
    records = [_artifact("/a/x.json", producer="loader", metadata={
        "rows": 10, "api_key": "AKIA-XYZ", "nested": {"password": "hunter2"}},
    )]
    meta = normalize_artifacts(records)[0]["metadata"]
    assert meta["rows"] == 10
    assert meta["api_key"] == "***redacted***"
    assert meta["nested"]["password"] == "***redacted***"


def test_redacted_output_is_preferred_over_original() -> None:
    """A redacted output demotes the original it was produced from."""
    records = [
        _artifact("/processing/customers.csv", producer="loader"),
        _artifact("/artifacts/customers.redacted.csv", producer="redactor",
                  artifact_type="redacted_file", source_path="/processing/customers.csv"),
    ]
    by_path = {a["path"]: a for a in normalize_artifacts(records)}
    original = by_path["/processing/customers.csv"]
    redacted = by_path["/artifacts/customers.redacted.csv"]
    assert original["preferred"] is False
    assert original["redacted_by"] == "/artifacts/customers.redacted.csv"
    assert redacted["preferred"] is True


def test_producer_falls_back_to_app_then_unknown() -> None:
    """Producer comes from the explicit tag, else the app, else 'unknown'."""
    records = [
        _artifact("/a/1.json", app="rey_analyzer"),       # app -> analyzer
        _artifact("/a/2.json"),                            # neither -> unknown
        _artifact("/a/3.json", producer="messaging", app="rey_loader"),  # explicit wins
    ]
    producers = {a["path"]: a["producer"] for a in normalize_artifacts(records)}
    assert producers["/a/1.json"] == "analyzer"
    assert producers["/a/2.json"] == "unknown"
    assert producers["/a/3.json"] == "messaging"


def test_safe_to_preview_defaults_true_and_honors_explicit_false() -> None:
    """safe_to_preview defaults True and is respected when explicitly false."""
    records = [
        _artifact("/a/ok.csv", producer="loader"),
        _artifact("/a/blob.bin", producer="loader", safe_to_preview=False),
    ]
    flags = {a["path"]: a["safe_to_preview"] for a in normalize_artifacts(records)}
    assert flags["/a/ok.csv"] is True
    assert flags["/a/blob.bin"] is False


def test_log_artifact_reference_tags_producer_fields(tmp_path: Path) -> None:
    """The emitter records producer/type/source_path/safe_to_preview when supplied."""
    from types import SimpleNamespace

    run_log = tmp_path / "run_log.20260708_000000.jsonl"
    ctx = SimpleNamespace(run_log_path=str(run_log), run_id="r1",
                          run_timestamp="20260708_000000", owner_app_name="file_redactor")
    log_artifact_reference(
        ctx, "/artifacts/customers.redacted.csv", role="output",
        producer="redactor", artifact_type="redacted_file",
        source_path="/processing/customers.csv", safe_to_preview=True,
    )
    record = next(json.loads(line) for line in run_log.read_text().splitlines()
                  if '"ARTIFACT_REFERENCE"' in line)
    assert record["producer"] == "redactor"
    assert record["artifact_type"] == "redacted_file"
    assert record["source_path"] == "/processing/customers.csv"
    assert record["safe_to_preview"] is True

    # And the normalizer reads those fields straight back.
    artifact = normalize_artifacts([record])[0]
    assert artifact["producer"] == "redactor"
    assert artifact["artifact_type"] == "redacted_file"


def test_log_artifact_reference_records_direct_file_metadata(tmp_path: Path) -> None:
    """The shared artifact helper records direct metadata for the referenced file."""
    from types import SimpleNamespace

    artifact_path = tmp_path / "report.json"
    artifact_path.write_text('{"ok": true}\n', encoding="utf-8")
    run_log = tmp_path / "run_log.20260708_000000.jsonl"
    ctx = SimpleNamespace(run_log_path=str(run_log), run_id="r1",
                          run_timestamp="20260708_000000")

    log_artifact_reference(ctx, str(artifact_path), role="report", event="generated")

    record = next(json.loads(line) for line in run_log.read_text().splitlines()
                  if '"ARTIFACT_REFERENCE"' in line)
    assert record["exists"] is True
    assert record["size_bytes"] == artifact_path.stat().st_size
    assert record["modified_at"]
    artifact = normalize_artifacts([record])[0]
    assert artifact["exists"] is True
    assert artifact["size_bytes"] == artifact_path.stat().st_size
