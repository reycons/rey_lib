"""Tests for JSONL event-table normalization."""

from __future__ import annotations

import json

from rey_lib.logs import build_jsonl_event_table


def test_valid_jsonl_becomes_event_rows() -> None:
    """JSONL text normalizes to default table columns and rows."""
    raw = "\n".join([
        json.dumps({"timestamp": "2026-07-08T01:00:00Z", "level": "INFO",
                    "record_type": "RUN_START", "step_name": "prepare", "message": "start"}),
        json.dumps({"timestamp": "2026-07-08T01:01:00Z", "level": "ERROR",
                    "record_type": "STEP_END", "status": "failure", "message": "failed"}),
    ])

    package = build_jsonl_event_table(raw_text=raw, include_raw=True)

    assert [column["id"] for column in package["columns"]] == [
        "timestamp", "level", "event", "step", "status", "message", "source_line",
    ]
    assert all(column["filter"] is True for column in package["columns"])
    assert package["rows"][0]["event"] == "RUN_START"
    assert package["rows"][0]["step"] == "prepare"
    assert package["rows"][1]["status"] == "failure"
    assert package["summary"] == {"record_count": 2, "error_count": 1, "warning_count": 0}
    assert package["raw_text"] == raw


def test_blank_and_malformed_lines_do_not_abort_package() -> None:
    """Bad JSONL lines are reported while valid lines still render."""
    raw = "\n\n{\"level\":\"WARNING\",\"event\":\"warn\"}\nnot-json\n{\"level\":\"INFO\"}\n"

    package = build_jsonl_event_table(raw_text=raw)

    assert len(package["rows"]) == 2
    assert package["rows"][0]["source_line"] == 3
    assert package["rows"][1]["source_line"] == 5
    assert package["parse_errors"][0]["line"] == 4
    assert package["summary"]["warning_count"] == 1


def test_records_input_preserves_source_index_and_redacts_secret_values() -> None:
    """Already parsed records normalize without exposing secret-like keys."""
    package = build_jsonl_event_table(records=[
        {"record_id": "abc", "event": "TOKEN_TEST", "api_key": "plain-secret",
         "nested": {"password": "also-secret"}, "message": {"token": "hidden"}},
    ], include_raw=True)

    row = package["rows"][0]
    assert row["id"] == "abc"
    assert row["source_line"] == 0
    assert "plain-secret" not in package["raw_text"]
    assert "also-secret" not in package["raw_text"]
    assert "[REDACTED]" in package["raw_text"]
