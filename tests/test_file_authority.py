"""
Tests for the shared file-creation authority (SGC_Rey_System_File_Creation_Standard).

Cover the JsonFile write options (#1133: one JSON owner, both existing byte
contracts preserved) and the centralized append_jsonl helper (for run-log/JSONL
records).
"""

from __future__ import annotations

import json
from pathlib import Path

from rey_lib.files.data_file import DataFile
from rey_lib.files.file_utils import append_jsonl


def _json(path: Path):
    return DataFile.for_path(path, file_type="JSON")


def test_a_document_write_can_sort_its_keys(tmp_path: Path) -> None:
    """sort_keys=True orders object keys deterministically (state files)."""
    out = tmp_path / "state.json"
    _json(out).write({"b": 1, "a": 2, "c": 3}, sort_keys=True, atomic=False, trailing_newline=False)
    assert list(json.loads(out.read_text(encoding="utf-8")).keys()) == ["a", "b", "c"]


def test_a_document_write_keeps_the_callers_key_order(tmp_path: Path) -> None:
    """sort_keys=False preserves insertion order."""
    out = tmp_path / "state.json"
    _json(out).write({"b": 1, "a": 2}, sort_keys=False, atomic=False, trailing_newline=False)
    assert list(json.loads(out.read_text(encoding="utf-8")).keys()) == ["b", "a"]


def test_the_document_callers_bytes_are_unchanged(tmp_path: Path) -> None:
    """Indent 2, default=str, the caller's order, no trailing newline -- write_file's bytes."""
    out = tmp_path / "doc.json"
    value = {"b": 1, "a": Path("/x")}
    _json(out).write(value, sort_keys=False, atomic=False, trailing_newline=False)
    assert out.read_text(encoding="utf-8") == json.dumps(value, default=str, indent=2)


def test_the_loader_targets_bytes_are_unchanged(tmp_path: Path) -> None:
    """The default is the loader's file target: sorted, indent 2, trailing newline."""
    out = tmp_path / "rows.json"
    rows = [{"b": 1, "a": 2}]
    assert _json(out).write(rows) == 1
    assert out.read_text(encoding="utf-8") == json.dumps(rows, default=str, indent=2, sort_keys=True) + "\n"


def test_append_jsonl_writes_one_line_per_record(tmp_path: Path) -> None:
    """append_jsonl appends one JSON object per line and creates parents."""
    out = tmp_path / "deep" / "run_log.20260706_091845.jsonl"
    append_jsonl(out, {"run_id": "abc", "seq": 1})
    append_jsonl(out, {"run_id": "abc", "seq": 2})
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["seq"] == 1
    assert json.loads(lines[1])["seq"] == 2
