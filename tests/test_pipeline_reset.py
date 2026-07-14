"""Shared pipeline reset from execution-log evidence.

Reset planning is driven exclusively by the single PIPELINE_RESTORE_POLICY record
and this run's move FILE_OPERATION records (the production file_utils move schema).
INPUT_FILE_REFERENCE / ARTIFACT_MANIFEST remain valid provenance records but are no
longer authoritative for restore
(SGC_Rey_Pipeline_Reset_From_Run_Log_Combined).
"""

from __future__ import annotations

import json
from pathlib import Path

from rey_lib.files import (
    latest_pipeline_run,
    preview_pipeline_reset_from_run,
    reset_pipeline_from_run,
)


def _move_record(run_id: str, *, source: Path, destination: Path,
                 timestamp: str = "20260713_120000") -> dict:
    """One move FILE_OPERATION in the production file_utils schema."""
    return {
        "record_type": "FILE_OPERATION", "record_group": "execution",
        "run_id": run_id, "run_timestamp": timestamp, "pipeline_name": "daily",
        "operation": "move", "action": "move",
        "source_abs": str(source), "destination_abs": str(destination),
        "original_source_abs": str(source),
    }


def _write_run(
    path: Path,
    *,
    run_id: str,
    restore_from: Path,
    restore_to: Path,
    original: Path,
    current: Path,
    timestamp: str = "20260713_120000",
    extra_records: list[dict] | None = None,
) -> Path:
    """Write a run log: a single PIPELINE_RESTORE_POLICY plus a move of ``original``
    (under ``restore_to``) to ``current`` (under ``restore_from``). Reset should
    restore the file from its latest location back under ``restore_to``.
    """
    records = [
        {
            "record_type": "RUN_START", "record_group": "execution",
            "run_id": run_id, "run_timestamp": timestamp,
            "pipeline_name": "daily", "timestamp": "2026-07-13T12:00:00+00:00",
        },
        {
            "record_type": "PIPELINE_RESTORE_POLICY", "record_group": "execution",
            "run_id": run_id, "run_timestamp": timestamp, "pipeline_name": "daily",
            "restore_rules": [{"from": str(restore_from), "to": str(restore_to)}],
        },
        _move_record(run_id, source=original, destination=current, timestamp=timestamp),
        *(extra_records or []),
        {
            "record_type": "RUN_COMPLETE", "record_group": "execution",
            "run_id": run_id, "run_timestamp": timestamp,
            "pipeline_name": "daily", "status": "success",
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return path


def test_historical_run_restores_from_move_records_and_creates_audit_log(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "inbox"
    processed = tmp_path / "processed"
    original = inbox / "feed.csv"
    current = processed / "feed.csv"
    current.parent.mkdir(parents=True)
    current.write_text("input", encoding="utf-8")
    run_log = _write_run(
        tmp_path / "logs" / "daily.20260713_120000.jsonl", run_id="historical",
        restore_from=processed, restore_to=inbox, original=original, current=current,
    )

    result = reset_pipeline_from_run(run_log, reason="operator request")

    assert result["source_run_id"] == "historical"
    assert result["restored_count"] == 1
    assert original.read_text(encoding="utf-8") == "input"
    assert not current.exists()
    audit = Path(result["audit_log_path"])
    assert audit.is_file() and audit != run_log
    types = [json.loads(line)["record_type"] for line in audit.read_text().splitlines()]
    assert types[0] == "RUN_START"
    assert "PIPELINE_RESET_FILE" in types
    assert types[-1] == "RUN_COMPLETE"
    # The reset audit is a separate operation, not a newer pipeline execution.
    assert latest_pipeline_run(run_log.parent, "daily") == str(run_log)


def test_preview_reports_missing_source_and_destination_conflict(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    processed = tmp_path / "processed"

    # Missing source: the tracked file's latest location no longer exists.
    missing_log = _write_run(
        tmp_path / "missing.jsonl", run_id="missing",
        restore_from=processed, restore_to=inbox,
        original=inbox / "missing.csv", current=processed / "missing.csv",
    )
    assert preview_pipeline_reset_from_run(missing_log)["skipped"][0]["reason"] == (
        "recoverable input source is missing"
    )

    # Destination conflict: the restore target already exists.
    processed.mkdir(parents=True, exist_ok=True)
    inbox.mkdir(parents=True, exist_ok=True)
    (processed / "conflict.csv").write_text("recoverable", encoding="utf-8")
    (inbox / "conflict.csv").write_text("existing", encoding="utf-8")
    conflict_log = _write_run(
        tmp_path / "conflict.jsonl", run_id="conflict",
        restore_from=processed, restore_to=inbox,
        original=inbox / "conflict.csv", current=processed / "conflict.csv",
    )
    assert preview_pipeline_reset_from_run(conflict_log)["skipped"][0]["reason"] == (
        "destination conflict"
    )


def test_partial_reset_continues_after_file_error(tmp_path: Path, monkeypatch) -> None:
    inbox = tmp_path / "inbox"
    processed = tmp_path / "processed"
    for name in ("first.csv", "second.csv"):
        (processed / name).parent.mkdir(parents=True, exist_ok=True)
        (processed / name).write_text(name, encoding="utf-8")
    log = _write_run(
        tmp_path / "partial.jsonl", run_id="partial",
        restore_from=processed, restore_to=inbox,
        original=inbox / "first.csv", current=processed / "first.csv",
        extra_records=[_move_record(
            "partial", source=inbox / "second.csv", destination=processed / "second.csv",
        )],
    )

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
    assert (inbox / "second.csv").exists()


def test_file_with_no_matching_rule_is_skipped(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    processed = tmp_path / "processed"
    archive = tmp_path / "archive"  # not covered by any restore rule
    (archive / "orphan.csv").parent.mkdir(parents=True, exist_ok=True)
    (archive / "orphan.csv").write_text("x", encoding="utf-8")
    log = _write_run(
        tmp_path / "norule.jsonl", run_id="norule",
        restore_from=processed, restore_to=inbox,
        original=inbox / "orphan.csv", current=archive / "orphan.csv",
    )

    preview = preview_pipeline_reset_from_run(log)

    assert preview["move_count"] == 0
    assert preview["skipped"][0]["reason"] == "not restorable: no matching restore rule"


def test_only_the_selected_runs_move_records_are_used(tmp_path: Path) -> None:
    """A shared archive holds files from many runs; resetting run A restores only
    A's file, never B's, because candidates come solely from A's move records."""
    inbox = tmp_path / "inbox"
    processed = tmp_path / "processed"  # shared archive across runs
    for name in ("a.csv", "b.csv"):
        (processed / name).parent.mkdir(parents=True, exist_ok=True)
        (processed / name).write_text(name, encoding="utf-8")
    log_a = _write_run(
        tmp_path / "a.jsonl", run_id="A",
        restore_from=processed, restore_to=inbox,
        original=inbox / "a.csv", current=processed / "a.csv",
    )
    # Run B moved b.csv into the same shared archive; not part of A's log.
    _write_run(
        tmp_path / "b.jsonl", run_id="B",
        restore_from=processed, restore_to=inbox,
        original=inbox / "b.csv", current=processed / "b.csv",
    )

    result = reset_pipeline_from_run(log_a)

    assert result["restored_count"] == 1
    assert (inbox / "a.csv").exists()          # A's file restored
    assert not (inbox / "b.csv").exists()      # B's file never restored
    assert (processed / "b.csv").exists()      # B's file untouched in shared archive


def test_latest_pipeline_run_selects_newest_matching_run(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    processed = tmp_path / "processed"
    for timestamp, run_id in (("20260713_100000", "old"), ("20260713_130000", "new")):
        _write_run(
            tmp_path / f"daily.{timestamp}.jsonl", run_id=run_id,
            restore_from=processed, restore_to=inbox,
            original=inbox / f"{run_id}.csv", current=processed / f"{run_id}.csv",
            timestamp=timestamp,
        )
    selected = latest_pipeline_run(tmp_path, "daily")
    assert selected is not None
    assert Path(selected).name == "daily.20260713_130000.jsonl"


def test_generated_outputs_are_never_restored(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    processed = tmp_path / "processed"
    output = tmp_path / "work" / "result.json"  # generated output, never moved/restored
    (processed / "feed.csv").parent.mkdir(parents=True, exist_ok=True)
    (processed / "feed.csv").write_text("input", encoding="utf-8")
    output.parent.mkdir(parents=True)
    output.write_text("output", encoding="utf-8")
    log = _write_run(
        tmp_path / "run.jsonl", run_id="r1",
        restore_from=processed, restore_to=inbox,
        original=inbox / "feed.csv", current=processed / "feed.csv",
    )

    result = reset_pipeline_from_run(log)

    assert result["deleted_count"] == 0
    assert output.exists()
