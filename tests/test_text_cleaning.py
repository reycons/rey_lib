"""One authoritative control-character cleaning, available to every format.

Cleaning is a governed workflow step, not something a reader does on its own:
a CSV read returns exactly what the file holds, and the step applies
clean_text_value to the fields it has decided to clean. These tests state that
the primitive is shared, that Excel and a cleaning step agree on the same
corrupted value, and that a reader never cleans behind a caller's back.
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


def test_an_excel_cell_and_a_cleaned_csv_field_agree(tmp_path: Path) -> None:
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
    # A cleaning step reads the source as it is, then cleans what it chose to.
    csv_field = clean_text_value(read_csv(source).rows[0].fields[0])

    assert excel_cell == csv_field == CLEAN_VALUE


def test_structure_characters_are_used_before_any_cleaning(tmp_path: Path) -> None:
    """Tabs and newlines separate values; a step only cleans inside one."""
    source = tmp_path / "t.tsv"
    source.write_text(f"A\x00cc\tSym\ufeffbol\nA1\tIBM\nA2\tMSFT\n", encoding="utf-8")

    read = read_csv(source)

    # The tab still delimited, the newlines still split lines.
    assert read.delimiter == "\t"
    assert read.data_line_numbers == (2, 3)
    # The reader reports the header exactly as the file holds it.
    assert read.header_fields == ("A\x00cc", "Sym\ufeffbol")
    assert tuple(clean_text_value(f) for f in read.header_fields) == ("Acc", "Symbol")


def test_a_read_never_cleans_behind_the_caller(tmp_path: Path) -> None:
    """Both the fields and the physical line are what the file actually held."""
    source = tmp_path / "f.csv"
    source.write_text("A,B\nx\x01y,z\n", encoding="utf-8")

    row = read_csv(source).rows[0]

    assert row.fields == ("x\x01y", "z")
    assert row.text == "x\x01y,z"


def test_a_step_cleans_fields_after_the_split_never_raw_text() -> None:
    """Structure is used first; cleaning applies to values, one at a time."""
    fields = parse_delimited_line("a\x00b,c\x07d", ",")

    assert fields == ["a\x00b", "c\x07d"]
    assert [clean_text_value(field) for field in fields] == ["ab", "cd"]


def test_the_difference_between_read_and_cleaned_is_the_step_s_evidence(
    tmp_path: Path,
) -> None:
    """What a cleaning step records: which values it changed, and where."""
    source = tmp_path / "corrupt.csv"
    source.write_text(
        f"Column,Other\n{CORRUPT_VALUE},clean\nplain,also clean\n", encoding="utf-8"
    )

    read = read_csv(source)
    changed = [
        (row.physical_line_number, field)
        for row in read.rows
        for field in row.fields
        if clean_text_value(field) != field
    ]

    assert changed == [(2, CORRUPT_VALUE)]
