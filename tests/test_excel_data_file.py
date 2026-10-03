"""Excel is a DataFile family (row 589, step 2).

An inventoried workbook is a valid DataFile: it resolves by suffix or by
declared type, reads its first worksheet through FastExcel with the header row
as its declaration, and is refused by name where it cannot be read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.data.errors import DataStructureError
from rey_lib.errors.error_utils import ConfigError
from rey_lib.files.data_file import data_file_for
from rey_lib.files.data_file.excel import (
    ExcelDataFile,
    XlsbFile,
    XlsFile,
    XlsmFile,
    XlsxFile,
)
from tests.fixtures.workbooks.synthetic import mixed_workbook, sheet_only_workbook


class TestTheFamilyResolves:
    """Every workbook suffix the estate accepts names an Excel DataFile."""

    @pytest.mark.parametrize(("name", "subtype", "token"), [
        ("book.xls", XlsFile, "XLS"),
        ("book.xlsx", XlsxFile, "XLSX"),
        ("book.xlsb", XlsbFile, "XLSB"),
        ("book.xlsm", XlsmFile, "XLSM"),
    ])
    def test_by_suffix(self, tmp_path: Path, name: str,
                       subtype: type, token: str) -> None:
        resolved = data_file_for(tmp_path / name)

        assert type(resolved) is subtype
        assert isinstance(resolved, ExcelDataFile)
        assert resolved.file_type == token

    def test_a_declared_type_wins_over_the_suffix(self, tmp_path: Path) -> None:
        assert type(data_file_for(tmp_path / "book.bin", file_type="xls")) is XlsFile


class TestReading:
    """The first worksheet, header first, every cell as text."""

    def test_reads_the_first_worksheet_keyed_by_its_header(self, tmp_path: Path) -> None:
        book = data_file_for(mixed_workbook(tmp_path / "mixed.xlsx"))

        assert book.declares_structure is True
        assert book.source_structure() == ["Account", "Amount", "Memo"]
        assert book.read() == [
            {"Account": "A-1", "Amount": "10.5", "Memo": "x,y"},
            {"Account": "A-2", "Amount": "", "Memo": "line\nbreak"},
        ]

    def test_reads_an_xlsm_workbook(self, tmp_path: Path) -> None:
        book = data_file_for(sheet_only_workbook(tmp_path / "macro.xlsm", ("Only",)))

        assert book.read() == [{"Value": "1"}]

    def test_sample_stops_at_its_limit(self, tmp_path: Path) -> None:
        book = data_file_for(mixed_workbook(tmp_path / "mixed.xlsx"))

        assert book.sample(1) == [{"Account": "A-1", "Amount": "10.5", "Memo": "x,y"}]
        assert book.sample(0) == []


class TestValidation:
    """The header row is checked as a delimited header is: ordered."""

    def test_a_matching_header_reads(self, tmp_path: Path) -> None:
        book = data_file_for(mixed_workbook(tmp_path / "mixed.xlsx"))

        assert len(book.read_validated(["Account", "Amount", "Memo"])) == 2

    def test_a_reordered_header_is_a_mismatch(self, tmp_path: Path) -> None:
        book = data_file_for(mixed_workbook(tmp_path / "mixed.xlsx"))

        with pytest.raises(DataStructureError) as raised:
            book.validate(["Amount", "Account", "Memo"])

        assert raised.value.validation_name == "load_header"

    def test_an_unreadable_workbook_is_refused_by_name(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.xlsx"
        broken.write_bytes(b"not a workbook")

        with pytest.raises(DataStructureError) as raised:
            data_file_for(broken).read()

        assert "broken.xlsx" in str(raised.value)

    def test_a_workbook_cannot_be_written(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as raised:
            data_file_for(tmp_path / "out.xlsx").write([{"a": "1"}])

        assert "XLSX" in str(raised.value)
