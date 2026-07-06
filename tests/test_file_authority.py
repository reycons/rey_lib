"""
Tests for the shared file-creation authority (SGC_Rey_System_File_Creation_Standard).

Cover the write_file JSON sort_keys option (for deterministic state files) and the
centralized append_jsonl helper (for run-log/JSONL records).
"""

from __future__ import annotations

import json
from pathlib import Path

from rey_lib.files.file_utils import append_jsonl, write_file


def test_write_file_json_sort_keys(tmp_path: Path) -> None:
    """JSON output with sort_keys=True orders object keys deterministically."""
    out = tmp_path / "state.json"
    write_file(out, {"b": 1, "a": 2, "c": 3}, "JSON", sort_keys=True)
    assert list(json.loads(out.read_text(encoding="utf-8")).keys()) == ["a", "b", "c"]


def test_write_file_json_default_unsorted(tmp_path: Path) -> None:
    """Without sort_keys the insertion order is preserved."""
    out = tmp_path / "state.json"
    write_file(out, {"b": 1, "a": 2}, "JSON")
    assert list(json.loads(out.read_text(encoding="utf-8")).keys()) == ["b", "a"]


def test_append_jsonl_writes_one_line_per_record(tmp_path: Path) -> None:
    """append_jsonl appends one JSON object per line and creates parents."""
    out = tmp_path / "deep" / "run_log.20260706_091845.jsonl"
    append_jsonl(out, {"run_id": "abc", "seq": 1})
    append_jsonl(out, {"run_id": "abc", "seq": 2})
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["seq"] == 1
    assert json.loads(lines[1])["seq"] == 2
