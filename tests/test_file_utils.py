"""Tests for generic file utility helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.files.file_utils import (
    bounded_text_preview,
    discover_inbox_files,
    folder_children,
    input_files,
    input_tree_files,
    is_hidden_path,
    matches_file_pattern,
    move_to_failed,
    move_to_processing,
    move_to_success,
    pattern_to_glob,
    resolve_safe_file,
    visible_files,
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


def test_input_files_can_recurse(tmp_path: Path) -> None:
    nested = tmp_path / "client"
    nested.mkdir()
    (nested / "feed.csv").write_text("x", encoding="utf-8")

    files = input_files(tmp_path, "*.csv", recursive=True)

    assert [path.relative_to(tmp_path).as_posix() for path in files] == ["client/feed.csv"]


def test_input_tree_files_skips_hidden_and_yaml(tmp_path: Path) -> None:
    nested = tmp_path / "client"
    nested.mkdir()
    (nested / "feed.csv").write_text("x", encoding="utf-8")
    (nested / "redact.yaml").write_text("x", encoding="utf-8")
    (nested / ".hidden.csv").write_text("x", encoding="utf-8")

    files = input_tree_files(tmp_path)

    assert [path.name for path in files] == ["feed.csv"]


def test_visible_files_skips_hidden_segments(tmp_path: Path) -> None:
    nested = tmp_path / "client"
    hidden = tmp_path / ".hidden"
    nested.mkdir()
    hidden.mkdir()
    (nested / "feed.csv").write_text("x", encoding="utf-8")
    (hidden / "secret.csv").write_text("x", encoding="utf-8")

    files = visible_files(tmp_path, "*.csv")

    assert [path.relative_to(tmp_path).as_posix() for path in files] == ["client/feed.csv"]
    assert is_hidden_path(hidden / "secret.csv", tmp_path) is True


def test_folder_children_returns_recursive_tree(tmp_path: Path) -> None:
    nested = tmp_path / "client"
    nested.mkdir()
    (nested / "feed.csv").write_text("x", encoding="utf-8")

    tree = folder_children(tmp_path)

    assert tree[0]["type"] == "directory"
    assert tree[0]["file_count"] == 1
    assert tree[0]["children"][0]["relative_path"] == "client/feed.csv"


def test_resolve_safe_file_rejects_outside_root(tmp_path: Path) -> None:
    file_path = tmp_path / "allowed.txt"
    file_path.write_text("x", encoding="utf-8")

    assert resolve_safe_file(file_path, tmp_path) == file_path.resolve()

    outside = tmp_path.parent / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        resolve_safe_file(outside, tmp_path)
    outside.unlink(missing_ok=True)


def test_bounded_text_preview_truncates_content(tmp_path: Path) -> None:
    file_path = tmp_path / "sample.txt"
    file_path.write_text("abcdef", encoding="utf-8")

    result = bounded_text_preview(file_path, 3)

    assert result["name"] == "sample.txt"
    assert result["content"] == "abc"
    assert result["truncated"] is True


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
