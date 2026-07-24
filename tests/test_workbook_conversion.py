"""Focused tests for the application-neutral workbook conversion boundary."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Any

from openpyxl import Workbook
import polars as pl
import pytest

import rey_lib.files.workbook_conversion as conversion
from rey_lib.files import (
    EmptyWorkbookError,
    UnsupportedWorkbookError,
    WorkbookEncryptedError,
    WorkbookOutputCollisionError,
    WorkbookWriteError,
    convert_workbook_to_csv,
    is_supported_workbook,
)
from tests.fixtures.workbooks import mixed_workbook, sheet_only_workbook


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("book.xls", True),
        ("book.XLSX", True),
        ("book.xlsb", True),
        ("book.XlSm", True),
        ("book.csv", False),
        ("book", False),
    ],
)
def test_is_supported_workbook_is_case_insensitive(name: str, expected: bool) -> None:
    assert is_supported_workbook(name) is expected


def test_defined_tables_and_worksheet_fallback_are_not_duplicated(tmp_path: Path) -> None:
    source = mixed_workbook(tmp_path / "Holdings Report.xlsx")
    output_dir = tmp_path / "out"

    result = convert_workbook_to_csv(source, output_dir)

    assert result.conversion_status == "success_with_warnings"
    assert [item.artifact_name for item in result.outputs] == [
        "Holdings_Report.Table_Sheet.Current_Holdings.csv",
        "Holdings_Report.Notes_Data.csv",
    ]
    assert [item.extraction_kind for item in result.outputs] == [
        "defined_table",
        "worksheet",
    ]
    assert result.outputs[0].sheet_name == "Table Sheet"
    assert result.outputs[0].table_name == "Current_Holdings"
    assert result.outputs[0].row_count == 2
    assert result.outputs[0].column_names == ("Account", "Amount", "Memo")
    assert not (output_dir / "Holdings_Report.Table_Sheet.csv").exists()
    assert {warning.code for warning in result.warnings} == {
        "sheet_excluded",
        "empty_sheet_excluded",
    }


def test_csv_contract_is_exact_and_deterministic(tmp_path: Path) -> None:
    source = mixed_workbook(tmp_path / "Holdings Report.xlsx")

    result = convert_workbook_to_csv(source, tmp_path / "out")

    table_bytes = result.outputs[0].output_path.read_bytes()
    assert table_bytes == (
        b'Account,Amount,Memo\n'
        b'A-1,10.5,"x,y"\n'
        b'A-2,,"line\nbreak"\n'
    )
    assert not table_bytes.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in table_bytes


def test_xlsm_uses_xlsx_family_table_extraction(tmp_path: Path) -> None:
    xlsx = mixed_workbook(tmp_path / "source.xlsx")
    xlsm = tmp_path / "source.xlsm"
    shutil.copyfile(xlsx, xlsm)

    result = convert_workbook_to_csv(xlsm, tmp_path / "out")

    assert result.source_extension == ".xlsm"
    assert result.outputs[0].extraction_kind == "defined_table"


class _FakeSheet:
    visible = "visible"

    def __init__(self, frame: pl.DataFrame) -> None:
        self._frame = frame

    def to_polars(self) -> pl.DataFrame:
        return self._frame


class _LegacyReader:
    sheet_names = ["Legacy"]

    def __init__(self) -> None:
        self.table_names_called = False

    def table_names(self, _sheet_name: str) -> list[str]:
        self.table_names_called = True
        raise AssertionError("legacy workbook must not enumerate tables")

    def load_sheet(self, _name: str, *, n_rows: int | None = None) -> _FakeSheet:
        frame = pl.DataFrame({"Column": ["value"]})
        if n_rows == 0:
            frame = frame.head(0)
        return _FakeSheet(frame)


@pytest.mark.parametrize("extension", [".xls", ".xlsb"])
def test_legacy_formats_bypass_table_enumeration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extension: str,
) -> None:
    source = tmp_path / f"legacy{extension}"
    source.write_bytes(b"synthetic")
    reader = _LegacyReader()
    monkeypatch.setattr(conversion, "_load_dependencies", lambda _source: (object(), pl))
    monkeypatch.setattr(conversion, "_open_workbook", lambda _fastexcel, _source: reader)

    result = convert_workbook_to_csv(source, tmp_path / "out")

    assert reader.table_names_called is False
    assert result.outputs[0].artifact_name == "legacy.Legacy.csv"
    assert result.outputs[0].extraction_kind == "worksheet"


def test_workbook_is_opened_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = mixed_workbook(tmp_path / "book.xlsx")
    original = conversion._open_workbook
    calls = 0

    def counted_open(fastexcel: Any, source_path: Path) -> Any:
        nonlocal calls
        calls += 1
        return original(fastexcel, source_path)

    monkeypatch.setattr(conversion, "_open_workbook", counted_open)

    convert_workbook_to_csv(source, tmp_path / "out")

    assert calls == 1


def test_sanitized_name_collision_fails_before_writing(tmp_path: Path) -> None:
    source = sheet_only_workbook(tmp_path / "book.xlsx", ("A.B", "A B"))
    output_dir = tmp_path / "out"

    with pytest.raises(WorkbookOutputCollisionError):
        convert_workbook_to_csv(source, output_dir)

    assert not output_dir.exists() or list(output_dir.iterdir()) == []


def test_existing_destination_is_never_overwritten(tmp_path: Path) -> None:
    source = sheet_only_workbook(tmp_path / "book.xlsx", ("Sheet",))
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    existing = output_dir / "book.Sheet.csv"
    existing.write_text("keep me", encoding="utf-8")

    with pytest.raises(WorkbookOutputCollisionError):
        convert_workbook_to_csv(source, output_dir)

    assert existing.read_text(encoding="utf-8") == "keep me"


def test_publish_failure_removes_new_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = sheet_only_workbook(tmp_path / "book.xlsx", ("One", "Two"))
    output_dir = tmp_path / "out"
    original_link = os.link
    calls = 0

    def fail_second_link(source_path: Path, destination_path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic publish failure")
        original_link(source_path, destination_path)

    monkeypatch.setattr(conversion.os, "link", fail_second_link)

    with pytest.raises(WorkbookWriteError):
        convert_workbook_to_csv(source, output_dir)

    assert list(output_dir.iterdir()) == []


def test_password_protection_is_translated_and_chained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "protected.xlsx"
    source.write_bytes(b"synthetic")

    class _FastExcel:
        @staticmethod
        def read_excel(_source: Path) -> Any:
            raise RuntimeError("Workbook is password protected")

    monkeypatch.setattr(conversion, "_load_dependencies", lambda _source: (_FastExcel, pl))

    with pytest.raises(WorkbookEncryptedError) as exc_info:
        convert_workbook_to_csv(source, tmp_path / "out")

    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_empty_workbook_returns_no_partial_result(tmp_path: Path) -> None:
    source = tmp_path / "empty.xlsx"
    workbook = Workbook()
    workbook.save(source)

    with pytest.raises(EmptyWorkbookError):
        convert_workbook_to_csv(source, tmp_path / "out")


def test_unsupported_extension_is_structured(tmp_path: Path) -> None:
    source = tmp_path / "book.csv"
    source.write_text("a\n1\n", encoding="utf-8")

    with pytest.raises(UnsupportedWorkbookError) as exc_info:
        convert_workbook_to_csv(source, tmp_path / "out")

    assert exc_info.value.code == "unsupported_workbook"
    assert exc_info.value.source_path == source.resolve()
