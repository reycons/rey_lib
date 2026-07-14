"""Shared pipeline reset from execution-log evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rey_lib.files import (
    latest_pipeline_run,
    preview_pipeline_reset_from_run,
    reset_pipeline_from_run,
)


def _write_run(
    path: Path,
    *,
    run_id: str,
    original: Path,
    current: Path,
    output: Path | None = None,
    timestamp: str = "20260713_120000",
) -> Path:
    artifacts = [{
        "path": str(original),
        "artifact_group": "input_files",
        "display_name": original.name,
    }]
    if output is not None:
        artifacts.append({
            "path": str(output),
            "artifact_group": "output_files",
            "display_name": output.name,
        })
    records = [
        {
            "record_type": "RUN_START", "record_group": "execution",
            "run_id": run_id, "run_timestamp": timestamp,
            "pipeline_name": "daily", "timestamp": "2026-07-13T12:00:00+00:00",
        },
        {
            "record_type": "INPUT_FILE_REFERENCE", "record_group": "files",
            "run_id": run_id, "run_timestamp": timestamp,
            "pipeline_name": "daily", "path": str(original),
            "restore_source_path": str(current),
            "restore_destination_path": str(original),
        },
        {
            "record_type": "FILE_OPERATION", "record_group": "execution",
            "run_id": run_id, "run_timestamp": timestamp,
            "pipeline_name": "daily", "operation": "move", "status": "success",
            "source_path": str(original), "target_path": str(current),
        },
        {
            "record_type": "ARTIFACT_MANIFEST", "record_group": "files",
            "run_id": run_id, "run_timestamp": timestamp,
            "pipeline_name": "daily", "artifacts": artifacts,
        },
        {
            "record_type": "RUN_COMPLETE", "record_group": "execution",
            "run_id": run_id, "run_timestamp": timestamp,
            "pipeline_name": "daily", "status": "success",
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return path


def test_historical_run_restores_only_declared_input_and_creates_audit_log(
    tmp_path: Path,
) -> None:
    original = tmp_path / "inbox" / "client" / "feed.csv"
    current = tmp_path / "processed" / "feed.csv"
    output = tmp_path / "archive" / "generated.json"
    current.parent.mkdir(parents=True)
    current.write_text("input", encoding="utf-8")
    output.parent.mkdir(parents=True)
    output.write_text("output", encoding="utf-8")
    run_log = _write_run(
        tmp_path / "logs" / "daily.20260713_120000.jsonl",
        run_id="historical", original=original, current=current, output=output,
    )

    result = reset_pipeline_from_run(run_log, reason="operator request")

    assert result["source_run_id"] == "historical"
    assert result["restored_count"] == 1
    assert original.read_text(encoding="utf-8") == "input"
    assert not current.exists()
    assert output.read_text(encoding="utf-8") == "output"
    audit = Path(result["audit_log_path"])
    assert audit.is_file() and audit != run_log
    types = [json.loads(line)["record_type"] for line in audit.read_text().splitlines()]
    assert types[0] == "RUN_START"
    assert "PIPELINE_RESET_FILE" in types
    assert types[-1] == "RUN_COMPLETE"
    # The reset audit is a separate operation, not a newer pipeline execution.
    assert latest_pipeline_run(run_log.parent, "daily") == str(run_log)


def test_preview_reports_missing_source_and_destination_conflict(tmp_path: Path) -> None:
    missing_original = tmp_path / "inbox" / "missing.csv"
    missing_current = tmp_path / "processed" / "missing.csv"
    missing_log = _write_run(
        tmp_path / "missing.jsonl", run_id="missing",
        original=missing_original, current=missing_current,
    )
    assert preview_pipeline_reset_from_run(missing_log)["skipped"][0]["reason"] == (
        "recoverable input source is missing"
    )

    conflict_original = tmp_path / "inbox" / "conflict.csv"
    conflict_current = tmp_path / "processed" / "conflict.csv"
    conflict_original.parent.mkdir(parents=True, exist_ok=True)
    conflict_current.parent.mkdir(parents=True, exist_ok=True)
    conflict_original.write_text("existing", encoding="utf-8")
    conflict_current.write_text("recoverable", encoding="utf-8")
    conflict_log = _write_run(
        tmp_path / "conflict.jsonl", run_id="conflict",
        original=conflict_original, current=conflict_current,
    )
    assert preview_pipeline_reset_from_run(conflict_log)["skipped"][0]["reason"] == (
        "destination conflict"
    )


def test_partial_reset_continues_after_file_error(tmp_path: Path, monkeypatch) -> None:
    first_original = tmp_path / "inbox" / "first.csv"
    first_current = tmp_path / "processed" / "first.csv"
    second_original = tmp_path / "inbox" / "second.csv"
    second_current = tmp_path / "processed" / "second.csv"
    for path in (first_current, second_current):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")
    log = _write_run(tmp_path / "partial.jsonl", run_id="partial",
                     original=first_original, current=first_current)
    records = [json.loads(line) for line in log.read_text().splitlines()]
    records[3]["artifacts"].append({
        "path": str(second_original), "artifact_group": "input_files",
    })
    records.insert(2, {
        "record_type": "INPUT_FILE_REFERENCE", "record_group": "files",
        "run_id": "partial", "run_timestamp": "20260713_120000",
        "pipeline_name": "daily", "path": str(second_original),
        "restore_source_path": str(second_current),
        "restore_destination_path": str(second_original),
    })
    records.insert(4, {
        "record_type": "FILE_OPERATION", "record_group": "execution",
        "run_id": "partial", "run_timestamp": "20260713_120000",
        "pipeline_name": "daily", "operation": "move", "status": "success",
        "source_path": str(second_original), "target_path": str(second_current),
    })
    log.write_text("\n".join(json.dumps(record) for record in records) + "\n")

    from rey_lib.files import pipeline_reset
    real_move = pipeline_reset.move_file

    def _move(source, *args, **kwargs):
        if Path(source).name == "first.csv":
            raise OSError("blocked")
        return real_move(source, *args, **kwargs)

    monkeypatch.setattr(pipeline_reset, "move_file", _move)
    result = reset_pipeline_from_run(log)
    assert result["failed_count"] == 1
    assert result["restored_count"] == 1
    assert second_original.exists()


def test_latest_pipeline_run_selects_newest_matching_run(tmp_path: Path) -> None:
    for timestamp, run_id in (("20260713_100000", "old"), ("20260713_130000", "new")):
        _write_run(
            tmp_path / f"daily.{timestamp}.jsonl", run_id=run_id,
            original=tmp_path / "inbox" / f"{run_id}.csv",
            current=tmp_path / "processed" / f"{run_id}.csv",
            timestamp=timestamp,
        )
    selected = latest_pipeline_run(tmp_path, "daily")
    assert selected is not None
    assert Path(selected).name == "daily.20260713_130000.jsonl"


def test_manifest_output_without_input_reference_is_never_restored(tmp_path: Path) -> None:
    original = tmp_path / "inbox" / "feed.csv"
    current = tmp_path / "processed" / "feed.csv"
    output = tmp_path / "archive" / "result.json"
    current.parent.mkdir(parents=True)
    current.write_text("input")
    output.parent.mkdir(parents=True)
    output.write_text("output")
    log = _write_run(tmp_path / "run.jsonl", run_id="r1", original=original,
                     current=current, output=output)
    result = reset_pipeline_from_run(log)
    assert result["deleted_count"] == 0
    assert output.exists()


def test_file_operation_without_explicit_restore_source_is_not_restorable(
    tmp_path: Path,
) -> None:
    original = tmp_path / "inbox" / "feed.csv"
    current = tmp_path / "processed" / "feed.csv"
    current.parent.mkdir(parents=True)
    current.write_text("input")
    log = _write_run(tmp_path / "no-metadata.jsonl", run_id="r2",
                     original=original, current=current)
    records = [json.loads(line) for line in log.read_text().splitlines()]
    input_reference = next(
        record for record in records
        if record["record_type"] == "INPUT_FILE_REFERENCE"
    )
    input_reference.pop("restore_source_path")
    input_reference.pop("restore_destination_path")
    log.write_text("\n".join(json.dumps(record) for record in records) + "\n")

    preview = preview_pipeline_reset_from_run(log)

    assert preview["move_count"] == 0
    assert preview["skipped"][0]["reason"].startswith("not restorable")
    assert current.exists()


def test_input_reference_is_consumed_when_manifest_omits_input_files(
    tmp_path: Path,
) -> None:
    original = tmp_path / "inbox" / "feed.csv"
    current = tmp_path / "processed" / "feed.csv"
    current.parent.mkdir(parents=True)
    current.write_text("input")
    log = _write_run(
        tmp_path / "reference-only.jsonl", run_id="reference-only",
        original=original, current=current,
    )
    records = [json.loads(line) for line in log.read_text().splitlines()]
    manifest = next(
        record for record in records if record["record_type"] == "ARTIFACT_MANIFEST"
    )
    manifest["artifacts"] = []
    reference = next(
        record for record in records if record["record_type"] == "INPUT_FILE_REFERENCE"
    )
    reference["artifact_group"] = "input_files"
    log.write_text("\n".join(json.dumps(record) for record in records) + "\n")

    preview = preview_pipeline_reset_from_run(log)

    assert preview["move_count"] == 1
    assert preview["moves"][0]["source_path"] == str(current)
    assert preview["moves"][0]["destination_path"] == str(original)


def test_input_reference_without_restore_source_reports_exact_rejection(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "work" / "profile.json"
    input_path.parent.mkdir(parents=True)
    input_path.write_text("{}")
    log = _write_run(
        tmp_path / "incomplete-reference.jsonl", run_id="incomplete-reference",
        original=input_path, current=tmp_path / "unused" / "profile.json",
    )
    records = [json.loads(line) for line in log.read_text().splitlines()]
    reference = next(
        record for record in records if record["record_type"] == "INPUT_FILE_REFERENCE"
    )
    reference.pop("restore_source_path")
    reference.pop("restore_destination_path")
    reference["artifact_group"] = "input_files"
    manifest = next(
        record for record in records if record["record_type"] == "ARTIFACT_MANIFEST"
    )
    manifest["artifacts"] = []
    log.write_text("\n".join(json.dumps(record) for record in records) + "\n")

    preview = preview_pipeline_reset_from_run(log)

    assert preview["move_count"] == 0
    assert preview["skipped"] == [{
        "file": "profile.json",
        "reason": "not restorable: explicit restore source is missing",
    }]
