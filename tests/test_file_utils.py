"""Tests for generic file utility helpers."""

from __future__ import annotations

from pathlib import Path

from types import SimpleNamespace

from rey_lib.files.file_utils import (
    discover_inbox_files,
    input_files,
    matches_file_pattern,
    move_to_failed,
    move_to_processing,
    move_to_success,
    pattern_to_glob,
)


def _source_cfg(tmp_path: Path, pattern: str | list[str] = "*.jsonl") -> SimpleNamespace:
    paths = SimpleNamespace(
        inbox_path=str(tmp_path / "inbox"),
        processing_path=str(tmp_path / "processing"),
        success_path=str(tmp_path / "success"),
        failed_path=str(tmp_path / "failed"),
    )
    return SimpleNamespace(name="test", file_pattern=pattern, paths=paths)


def test_pattern_to_glob_replaces_tokens() -> None:
    assert pattern_to_glob("tran_{yyyymmdd}.csv") == "tran_*.csv"


def test_input_files_accepts_multiple_patterns(tmp_path: Path) -> None:
    (tmp_path / "a.ps1").write_text("a", encoding="utf-8")
    (tmp_path / "b.tr1").write_text("b", encoding="utf-8")
    (tmp_path / "c.csv").write_text("c", encoding="utf-8")

    files = input_files(tmp_path, ["*.ps1", "*.tr1"])

    assert [path.name for path in files] == ["a.ps1", "b.tr1"]


def test_input_files_expands_pattern_tokens(tmp_path: Path) -> None:
    (tmp_path / "tran_20260501.csv").write_text("a", encoding="utf-8")
    (tmp_path / "pos_20260501.csv").write_text("b", encoding="utf-8")

    files = input_files(tmp_path, "tran_{yyyymmdd}.csv")

    assert [path.name for path in files] == ["tran_20260501.csv"]


def test_matches_file_pattern_accepts_multiple_patterns_and_relative_paths(tmp_path: Path) -> None:
    nested = tmp_path / "bny_mellon"
    nested.mkdir()
    file_path = nested / "feed.tr1"
    file_path.write_text("x", encoding="utf-8")

    assert matches_file_pattern(file_path, ["*.ps1", "bny_mellon/*.tr1"], tmp_path)


def test_discover_inbox_files_uses_source_pattern(tmp_path: Path) -> None:
    source_cfg = _source_cfg(tmp_path, ["*.jsonl", "*.yaml"])
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.jsonl").write_text("a", encoding="utf-8")
    (inbox / "b.yaml").write_text("b", encoding="utf-8")
    (inbox / "c.txt").write_text("c", encoding="utf-8")

    files = discover_inbox_files(source_cfg)

    assert [path.name for path in files] == ["a.jsonl", "b.yaml"]


def test_stage_moves_use_configured_paths(tmp_path: Path) -> None:
    source_cfg = _source_cfg(tmp_path)
    inbox_file = tmp_path / "inbox" / "run.jsonl"
    inbox_file.parent.mkdir()
    inbox_file.write_text("data", encoding="utf-8")

    processing = move_to_processing(inbox_file, source_cfg)
    success = move_to_success(processing, source_cfg)
    failed_source = tmp_path / "processing" / "failed.jsonl"
    failed_source.write_text("data", encoding="utf-8")
    failed = move_to_failed(failed_source, source_cfg)

    assert processing.parent.name == "processing"
    assert success.parent.name == "success"
    assert failed.parent.name == "failed"
