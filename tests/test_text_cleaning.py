"""One authoritative control-character cleaning, shared by every format.

The point of these tests is agreement: a value carrying the same corruption
must come out identical whether it arrived from a spreadsheet cell or a
delimited field, and the characters that carry a file's structure must survive
until the parser has used them.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

from rey_lib.files.csv import parse_delimited_line, read_csv
from rey_lib.files.text import clean_text_value

# A value carrying one of everything: control, format, and legitimate content.
CORRUPT_VALUE = "Ac\x00co\x07un﻿t​No"
CLEAN_VALUE = "AccountNo"


def test_control_and_format_characters_are_removed() -> None:
    assert clean_text_value(CORRUPT_VALUE) == CLEAN_VALUE


@pytest.mark.parametrize(
    "character",
    ["\x00", "\x01", "\x07", "\x1a", "\t", "\n", "\r", "﻿", "​"],
)
def test_every_cc_and_cf_character_is_removed(character: str) -> None:
    assert unicodedata.category(character) in {"Cc", "Cf"}
    assert clean_text_value(f"a{character}b") == "ab"


@pytest.mark.parametrize("character", [" ", " ", "-", "é", "字"])
def test_other_characters_are_preserved_exactly(character: str) -> None:
    """No trimming, no collapsing: a non-breaking space is content, not noise."""
    assert clean_text_value(f"a{character}b") == f"a{character}b"


def test_leading_and_trailing_whitespace_is_untouched() -> None:
    assert clean_text_value("  spaced  ") == "  spaced  "
    assert clean_text_value("double  space") == "double  space"


def test_an_excel_cell_and_a_csv_field_clean_identically(tmp_path: Path) -> None:
    """The regression this shared primitive exists to prevent."""
    from rey_lib.files.workbook_conversion import _normalize_extracted_frame

    polars = pytest.importorskip("polars")
    frame = polars.DataFrame({"Column": [CORRUPT_VALUE]})
    excel_cell = _normalize_extracted_frame(frame)["Column"][0]

    # Two columns, so the header is detectable and row one is data.
    source = tmp_path / "native.csv"
    source.write_text(
        f"Column,Other\n{CORRUPT_VALUE},x\n{CORRUPT_VALUE},y\n", encoding="utf-8"
    )
    csv_field = read_csv(source).rows[0].fields[0]

    assert excel_cell == csv_field == CLEAN_VALUE


def test_structure_characters_survive_until_parsing(tmp_path: Path) -> None:
    """Tabs and newlines separate values; they are only noise inside one."""
    source = tmp_path / "t.tsv"
    source.write_text("A\x00cc\tSym﻿bol\nA1\tIBM\nA2\tMSFT\n", encoding="utf-8")

    read = read_csv(source)

    # The tab still delimited, the newlines still split lines.
    assert read.delimiter == "\t"
    assert read.header_fields == ("Acc", "Symbol")
    assert read.data_line_numbers == (2, 3)
    assert [row.fields for row in read.rows] == [("A1", "IBM"), ("A2", "MSFT")]


def test_the_raw_line_is_preserved_as_evidence(tmp_path: Path) -> None:
    """Fields are cleaned; the physical line is what the file actually held."""
    source = tmp_path / "f.csv"
    source.write_text("A,B\nx\x01y,z\n", encoding="utf-8")

    row = read_csv(source).rows[0]

    assert row.fields == ("xy", "z")
    assert row.text == "x\x01y,z"


def test_cleaning_happens_after_the_split_not_before() -> None:
    """A delimiter inside the line is used first, then each field is cleaned."""
    assert parse_delimited_line("a\x00b,c\x07d", ",") == ["ab", "cd"]
