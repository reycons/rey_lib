"""Tests for file_operator.input_utils."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from rey_lib.errors.error_utils import ConfigError
from rey_lib.logs.app_logger import AppLogger

from rey_lib.files.error_utils import InputError, LayoutError
from rey_lib.files.input_utils import (
    parse_column_list,
    parse_ranges,
    resolve_input_files,
    resolve_layout,
)

_log = AppLogger(__name__)


class TestResolveLayout:
    """resolve_layout — layout detection and validation."""

    def test_explicit_layout_returned(self, tmp_path: Path) -> None:
        f = tmp_path / "file.txt"
        f.touch()
        assert resolve_layout(f, "fixed_width") == "fixed_width"

    def test_auto_csv(self, tmp_path: Path) -> None:
        f = tmp_path / "file.csv"
        assert resolve_layout(f, "auto") == "delimited"

    def test_auto_xlsx(self, tmp_path: Path) -> None:
        f = tmp_path / "report.xlsx"
        assert resolve_layout(f, "auto") == "excel"

    def test_auto_txt_defaults_delimited(self, tmp_path: Path) -> None:
        f = tmp_path / "data.txt"
        assert resolve_layout(f, "auto") == "delimited"

    def test_unknown_extension_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "file.parquet"
        with pytest.raises(LayoutError, match="Cannot auto-detect"):
            resolve_layout(f, "auto")

    def test_unknown_explicit_layout_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "file.csv"
        with pytest.raises(LayoutError, match="Unknown layout"):
            resolve_layout(f, "parquet")


class TestResolveInputFiles:
    """resolve_input_files — file enumeration from --input and --inbox."""

    def _args(self, **kwargs: object) -> argparse.Namespace:
        """Build a minimal Namespace from keyword args."""
        return argparse.Namespace(**kwargs)

    def test_single_file_returned(self, tmp_path: Path) -> None:
        f = tmp_path / "feed.csv"
        f.touch()
        args  = self._args(input=str(f), inbox=None, file_pattern="*")
        files = resolve_input_files(args, _log)
        assert files == [f]

    def test_missing_single_file_raises(self, tmp_path: Path) -> None:
        args = self._args(input=str(tmp_path / "missing.csv"), inbox=None, file_pattern="*")
        with pytest.raises(InputError, match="not found"):
            resolve_input_files(args, _log)

    def test_inbox_returns_sorted_files(self, tmp_path: Path) -> None:
        (tmp_path / "b.csv").touch()
        (tmp_path / "a.csv").touch()
        args  = self._args(input=None, inbox=str(tmp_path), file_pattern="*.csv")
        files = resolve_input_files(args, _log)
        assert [f.name for f in files] == ["a.csv", "b.csv"]

    def test_inbox_pattern_filters(self, tmp_path: Path) -> None:
        (tmp_path / "tran_001.csv").touch()
        (tmp_path / "bal_001.csv").touch()
        args  = self._args(input=None, inbox=str(tmp_path), file_pattern="tran_*.csv")
        files = resolve_input_files(args, _log)
        assert len(files) == 1
        assert files[0].name == "tran_001.csv"

    def test_inbox_accepts_multiple_patterns(self, tmp_path: Path) -> None:
        (tmp_path / "feed.ps1").touch()
        (tmp_path / "feed.tr1").touch()
        (tmp_path / "feed.csv").touch()
        args = self._args(input=None, inbox=str(tmp_path), file_pattern=["*.ps1", "*.tr1"])
        files = resolve_input_files(args, _log)
        assert [f.name for f in files] == ["feed.ps1", "feed.tr1"]

    def test_missing_inbox_raises(self, tmp_path: Path) -> None:
        args = self._args(input=None, inbox=str(tmp_path / "no_such_dir"), file_pattern="*")
        with pytest.raises(InputError, match="not found"):
            resolve_input_files(args, _log)


class TestParseColumnList:
    """parse_column_list — comma-separated column name parsing."""

    def test_none_returns_empty(self) -> None:
        assert parse_column_list(None) == []

    def test_blank_returns_empty(self) -> None:
        assert parse_column_list("") == []

    def test_single_column(self) -> None:
        assert parse_column_list("ACCOUNT NUMBER") == ["ACCOUNT NUMBER"]

    def test_multiple_columns_stripped(self) -> None:
        result = parse_column_list(" ACCOUNT NUMBER , MASTER ACCOUNT ")
        assert result == ["ACCOUNT NUMBER", "MASTER ACCOUNT"]

    def test_empty_tokens_ignored(self) -> None:
        result = parse_column_list("A,,B,")
        assert result == ["A", "B"]


class TestParseRanges:
    """parse_ranges — fixed-width range string parsing."""

    def test_none_returns_empty(self) -> None:
        ranges, names = parse_ranges(None)
        assert ranges == []
        assert names  == []

    def test_single_range(self) -> None:
        ranges, names = parse_ranges("2-13")
        assert ranges == [(2, 13)]
        assert names  == ["RANGE_2_13"]

    def test_multiple_ranges(self) -> None:
        ranges, names = parse_ranges("2-13,42-53")
        assert ranges == [(2, 13), (42, 53)]
        assert names  == ["RANGE_2_13", "RANGE_42_53"]

    def test_invalid_token_raises(self) -> None:
        with pytest.raises(ConfigError, match="Invalid range token"):
            parse_ranges("2:13")
