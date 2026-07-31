"""Structural answers returned by rey_lib.files.csv.read_csv.

The caller supplies criteria — encoding, an optional delimiter, required header
text, a sample limit — and reads the answer. These tests state what each
question returns, including the ones whose answer is "no".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.files.csv import (
    CsvReadError,
    looks_like_csv,
    read_csv,
    read_csv_text,
)


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Header presence and position
# ---------------------------------------------------------------------------

def test_header_is_located_past_a_junk_preamble(tmp_path: Path) -> None:
    source = _write(
        tmp_path,
        "preamble.csv",
        "Acme Holdings Report\n\nAccount,Symbol,Qty\nA1,IBM,10\nA2,MSFT,20\n",
    )

    read = read_csv(source)

    assert read.has_header is True
    assert read.header_line_number == 3
    assert read.header_fields == ("Account", "Symbol", "Qty")
    assert read.data_line_numbers == (4, 5)


def test_a_headerless_file_reports_no_header_rather_than_failing(
    tmp_path: Path,
) -> None:
    """Whether a header exists is a question, so 'no' is an answer."""
    source = _write(tmp_path, "numbers.csv", "1,2,3\n4,5,6\n7,8,9\n")

    read = read_csv(source)

    assert read.has_header is False
    assert read.header_line_number is None
    assert read.header_fields == ()
    # Every row is data when no header separates them.
    assert read.data_line_numbers == (1, 2, 3)


def test_a_minimal_two_column_file_still_resolves_its_header(
    tmp_path: Path,
) -> None:
    """One plausible candidate is enough; only a tie or none returns no header."""
    source = _write(tmp_path, "small.csv", "Alpha,Beta\nGamma,Delta\n")

    read = read_csv(source)

    assert read.has_header is True
    assert read.header_line_number == 1


# ---------------------------------------------------------------------------
# Delimiter
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("name", "delimiter"),
    [("commas.csv", ","), ("tabs.tsv", "\t"), ("semis.csv", ";"), ("pipes.csv", "|")],
)
def test_delimiter_is_detected_when_not_supplied(
    tmp_path: Path,
    name: str,
    delimiter: str,
) -> None:
    header = delimiter.join(["Account", "Symbol", "Qty"])
    body = delimiter.join(["A1", "IBM", "10"])
    source = _write(tmp_path, name, f"{header}\n{body}\n{body}\n")

    read = read_csv(source)

    assert read.delimiter == delimiter
    assert read.delimiter_supplied is False
    assert read.header_fields == ("Account", "Symbol", "Qty")


def test_a_supplied_delimiter_is_used_verbatim(tmp_path: Path) -> None:
    source = _write(tmp_path, "tabs.tsv", "Account\tSymbol\nA1\tIBM\nA2\tMSFT\n")

    read = read_csv(source, delimiter="\t")

    assert read.delimiter == "\t"
    assert read.delimiter_supplied is True


# ---------------------------------------------------------------------------
# Required header criteria
# ---------------------------------------------------------------------------

def test_required_header_text_is_reported_as_found(tmp_path: Path) -> None:
    source = _write(tmp_path, "f.csv", "Account,Symbol,Qty\nA1,IBM,10\nA2,X,2\n")

    read = read_csv(source, required_header=["Account", "Qty"])

    assert read.header_matched_all is True
    assert [(m.text, m.found, m.field) for m in read.header_matches] == [
        ("Account", True, "Account"),
        ("Qty", True, "Qty"),
    ]


def test_missing_required_header_text_is_reported_not_raised(
    tmp_path: Path,
) -> None:
    source = _write(tmp_path, "f.csv", "Account,Symbol,Qty\nA1,IBM,10\nA2,X,2\n")

    read = read_csv(source, required_header=["Cusip"])

    assert read.header_matched_all is False
    assert read.header_matches[0].found is False
    assert read.header_matches[0].field is None
    # The header itself is still located and reported.
    assert read.has_header is True


def test_required_text_locates_a_header_structure_would_miss(
    tmp_path: Path,
) -> None:
    """The caller supplies the criterion; this module decides where it matches."""
    source = _write(tmp_path, "f.csv", "Alpha,Beta\nGamma,Delta\n")

    read = read_csv(source, required_header=["Alpha"])

    assert read.has_header is True
    assert read.header_line_number == 1
    assert read.header_matched_all is True


# ---------------------------------------------------------------------------
# Rows, positions, raggedness, blanks
# ---------------------------------------------------------------------------

def test_rows_carry_their_physical_line_numbers(tmp_path: Path) -> None:
    source = _write(
        tmp_path, "f.csv", "Preamble\nAccount,Qty\nA1,10\nA2,20\n"
    )

    read = read_csv(source)

    assert [row.physical_line_number for row in read.rows] == [3, 4]
    assert [row.physical_line_number for row in read.all_rows] == [1, 2, 3, 4]


def test_ragged_rows_are_counted_and_flagged(tmp_path: Path) -> None:
    source = _write(
        tmp_path, "f.csv", "Account,Symbol,Qty\nA1,IBM,10\nA2,X,30,EXTRA\nA3,Y,1\n"
    )

    read = read_csv(source)

    assert read.ragged_row_count == 1
    assert [row.is_ragged for row in read.rows] == [False, True, False]
    assert read.rows[1].physical_line_number == 3


def test_only_data_region_blank_rows_are_counted(tmp_path: Path) -> None:
    """A blank preamble line is structural noise, not a blank data row."""
    source = _write(
        tmp_path, "f.csv", "Report\n\nAccount,Qty\nA1,10\n\nA2,20\n"
    )

    read = read_csv(source)

    assert read.header_line_number == 3
    assert read.blank_row_count == 1
    assert read.total_line_count == 6


def test_blank_data_rows_can_be_excluded_but_stay_counted(
    tmp_path: Path,
) -> None:
    source = _write(tmp_path, "f.csv", "Account,Qty\nA1,10\n\nA2,20\n")

    kept = read_csv(source)
    skipped = read_csv(source, skip_blank_lines=True)

    assert kept.blank_row_count == skipped.blank_row_count == 1
    assert kept.data_line_numbers == (2, 3, 4)
    assert skipped.data_line_numbers == (2, 4)


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------

def test_sample_takes_the_opening_middle_and_closing_rows(
    tmp_path: Path,
) -> None:
    body = "".join(f"{i},{i * 2}\n" for i in range(1, 31))
    source = _write(tmp_path, "f.csv", f"A,B\n{body}")

    sample = read_csv(source, sample_size=6).sample
    lines = [row.physical_line_number for row in sample]

    assert lines == [2, 3, 16, 17, 30, 31]
    assert lines == sorted(lines)


def test_sampling_is_deterministic(tmp_path: Path) -> None:
    body = "".join(f"{i},{i * 2}\n" for i in range(1, 101))
    source = _write(tmp_path, "f.csv", f"A,B\n{body}")

    first = [row.physical_line_number for row in read_csv(source, sample_size=9).sample]
    second = [row.physical_line_number for row in read_csv(source, sample_size=9).sample]

    assert first == second


def test_no_sample_is_returned_without_a_limit(tmp_path: Path) -> None:
    source = _write(tmp_path, "f.csv", "A,B\n1,2\n3,4\n")

    assert read_csv(source).sample == ()


def test_a_limit_above_the_row_count_returns_every_row(tmp_path: Path) -> None:
    source = _write(tmp_path, "f.csv", "A,B\n1,2\n3,4\n")

    read = read_csv(source, sample_size=100)

    assert read.sample == read.rows


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------

def test_an_empty_file_answers_without_failing(tmp_path: Path) -> None:
    read = read_csv(_write(tmp_path, "empty.csv", ""))

    assert read.has_header is False
    assert read.rows == ()
    assert read.total_line_count == 0
    assert read.source_text_sha256


def test_an_unreadable_path_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(CsvReadError, match="Cannot read"):
        read_csv(tmp_path / "does_not_exist.csv")


# ---------------------------------------------------------------------------
# Text-level recognition
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("delimiter", [",", "\t", ";", "|"])
def test_looks_like_csv_recognises_each_common_delimiter(delimiter: str) -> None:
    header = delimiter.join(["Account", "Symbol", "Qty"])
    row = delimiter.join(["A1", "IBM", "10"])

    assert looks_like_csv(f"{header}\n{row}\n{row}") is True


def test_looks_like_csv_agrees_with_the_reader_on_a_preamble(
    tmp_path: Path,
) -> None:
    """The text test is the reader's test, so they cannot disagree."""
    text = "Acme Holdings Report\n\nAccount,Symbol,Qty\nA1,IBM,10\nA2,MSFT,20\n"
    source = _write(tmp_path, "preamble.csv", text)

    assert read_csv(source).has_header is True
    assert looks_like_csv(text) is True


def test_looks_like_csv_rejects_prose_and_single_columns() -> None:
    assert looks_like_csv("Hello there.\nThis is prose, with a comma.") is False
    assert looks_like_csv("alpha\nbeta\ngamma") is False
    assert looks_like_csv("a,b,c") is False
    assert looks_like_csv("") is False


def test_a_named_delimiter_constrains_the_question() -> None:
    assert looks_like_csv("a,b\n1,2") is True
    assert looks_like_csv("a,b\n1,2", "\t") is False


def test_a_file_and_its_text_produce_the_same_analysis(tmp_path: Path) -> None:
    """read_csv is read_csv_text plus opening a file, and nothing more."""
    text = "Acme Holdings Report\n\nAccount,Symbol,Qty\nA1,IBM,10\nA2,X,30,EXTRA\n"
    source = _write(tmp_path, "preamble.csv", text)

    from_file = read_csv(source, sample_size=2)
    from_text = read_csv_text(text, sample_size=2)

    # Everything except where the content came from is identical.
    assert from_text.path == ""
    assert from_file.path.endswith("preamble.csv")
    for field in (
        "delimiter",
        "has_header",
        "header_line_number",
        "header_fields",
        "all_rows",
        "rows",
        "data_line_numbers",
        "total_line_count",
        "blank_row_count",
        "ragged_row_count",
        "sample",
        "source_text_sha256",
    ):
        assert getattr(from_file, field) == getattr(from_text, field), field


def test_looks_like_csv_is_the_reader_s_own_answer(tmp_path: Path) -> None:
    text = "Report\n\nAccount,Qty\nA1,10\nA2,20\n"

    assert looks_like_csv(text) is read_csv_text(text).has_header is True
