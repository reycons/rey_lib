"""open_csv streams data rows by default, or every physical line on request.

The default is the behaviour every existing caller has, and these tests pin it
rather than trusting it: a widening parameter that quietly widened the default
would change what every streaming reader sees.

include_all_rows exists for a caller that must account for every line in the
file -- a profile asserting that its type counts sum to the source row count
cannot do so if the preamble and header never reach it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.files.csv import open_csv, read_csv

# A preamble line, a blank, the header, two data rows, and a totals row.
SOURCE = (
    "Monthly statement\n"
    "\n"
    "Account,Name,Amount\n"
    "1,Alice,10\n"
    "2,Bob,20\n"
    "Total,,30\n"
)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    target = tmp_path / "statement.csv"
    target.write_text(SOURCE, encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# The default is unchanged
# ---------------------------------------------------------------------------

def test_the_default_still_streams_data_rows_only(source: Path) -> None:
    """Regression: the parameter must not widen what existing callers receive."""
    rows = list(open_csv(source).rows)

    assert [row.physical_line_number for row in rows] == [4, 5, 6]
    assert [row.text for row in rows] == ["1,Alice,10", "2,Bob,20", "Total,,30"]


def test_the_default_is_reached_without_naming_the_parameter(source: Path) -> None:
    named = list(open_csv(source, include_all_rows=False).rows)
    unnamed = list(open_csv(source).rows)

    assert [r.physical_line_number for r in named] == [
        r.physical_line_number for r in unnamed
    ]


# ---------------------------------------------------------------------------
# include_all_rows
# ---------------------------------------------------------------------------

def test_every_physical_line_is_yielded_exactly_once(source: Path) -> None:
    """One row per line, in order, nothing skipped and nothing repeated."""
    rows = list(open_csv(source, include_all_rows=True).rows)
    numbers = [row.physical_line_number for row in rows]

    assert numbers == [1, 2, 3, 4, 5, 6]
    assert len(numbers) == len(set(numbers))
    assert len(rows) == len(SOURCE.splitlines())


def test_the_preamble_blank_and_header_rows_all_arrive(source: Path) -> None:
    """The three kinds of row the default mode drops."""
    rows = {r.physical_line_number: r for r in open_csv(source, include_all_rows=True).rows}

    assert rows[1].text == "Monthly statement"
    assert rows[2].text == ""
    assert rows[2].is_blank is True
    assert rows[3].text == "Account,Name,Amount"


def test_yielded_text_is_the_source_line(source: Path) -> None:
    """Fidelity: what streams is what the file holds, line for line."""
    rows = list(open_csv(source, include_all_rows=True).rows)

    assert [row.text for row in rows] == SOURCE.splitlines()


def test_fields_are_parsed_for_every_row_including_the_header(source: Path) -> None:
    rows = {r.physical_line_number: r for r in open_csv(source, include_all_rows=True).rows}

    assert rows[3].fields == ("Account", "Name", "Amount")
    assert rows[1].fields == ("Monthly statement",)
    assert rows[6].fields == ("Total", "", "30")


# ---------------------------------------------------------------------------
# Structural decisions are identical in both modes
# ---------------------------------------------------------------------------

def test_the_structure_reported_is_the_same_either_way(source: Path) -> None:
    """The parameter widens iteration; it decides nothing."""
    default = open_csv(source)
    widened = open_csv(source, include_all_rows=True)

    for attribute in (
        "delimiter",
        "encoding",
        "has_header",
        "header_line_number",
        "header_fields",
        "header_matched_all",
    ):
        assert getattr(default, attribute) == getattr(widened, attribute), attribute


def test_the_header_is_known_before_any_row_streams(source: Path) -> None:
    """What lets a caller derive expected width without a second pass."""
    stream = open_csv(source, include_all_rows=True)

    assert stream.header_fields == ("Account", "Name", "Amount")
    assert stream.header_line_number == 3
    assert list(stream.rows)  # the iterator was still untouched above


def test_a_supplied_delimiter_is_honoured_in_both_modes(tmp_path: Path) -> None:
    target = tmp_path / "piped.csv"
    target.write_text("title\nA|B\n1|2\n", encoding="utf-8")

    default = open_csv(target, delimiter="|")
    widened = open_csv(target, delimiter="|", include_all_rows=True)

    assert default.delimiter == widened.delimiter == "|"
    assert [r.physical_line_number for r in widened.rows] == [1, 2, 3]


def test_a_headerless_file_streams_every_row_in_both_modes(tmp_path: Path) -> None:
    """With no header located, the default already yields everything."""
    target = tmp_path / "raw.csv"
    target.write_text("1,2\n3,4\n", encoding="utf-8")

    default = list(open_csv(target).rows)
    widened = list(open_csv(target, include_all_rows=True).rows)

    assert [r.physical_line_number for r in default] == [1, 2]
    assert [r.physical_line_number for r in widened] == [1, 2]


# ---------------------------------------------------------------------------
# Interaction with the other switch
# ---------------------------------------------------------------------------

def test_skip_blank_lines_remains_independent(source: Path) -> None:
    """Two switches, two concerns.

    A caller needing one row per physical line leaves skip_blank_lines unset;
    the widening parameter does not override it.
    """
    kept = list(open_csv(source, include_all_rows=True).rows)
    skipped = list(open_csv(source, include_all_rows=True, skip_blank_lines=True).rows)

    assert [r.physical_line_number for r in kept] == [1, 2, 3, 4, 5, 6]
    assert [r.physical_line_number for r in skipped] == [1, 3, 4, 5, 6]


# ---------------------------------------------------------------------------
# Agreement with the whole-file reader
# ---------------------------------------------------------------------------

def test_the_widened_stream_matches_read_csv_all_rows(source: Path) -> None:
    """The two readers must not disagree about what the file contains."""
    whole = read_csv(source)
    streamed = list(open_csv(source, include_all_rows=True).rows)

    assert [r.physical_line_number for r in streamed] == [
        r.physical_line_number for r in whole.all_rows
    ]
    assert [r.text for r in streamed] == [r.text for r in whole.all_rows]
    assert [r.fields for r in streamed] == [r.fields for r in whole.all_rows]


def test_the_default_stream_matches_read_csv_data_rows(source: Path) -> None:
    whole = read_csv(source)
    streamed = list(open_csv(source).rows)

    assert [r.physical_line_number for r in streamed] == [
        r.physical_line_number for r in whole.rows
    ]


def test_a_file_larger_than_the_header_window_streams_completely(
    tmp_path: Path,
) -> None:
    """Header location reads a bounded window; iteration must not stop there."""
    target = tmp_path / "long.csv"
    body = "".join(f"{i},name{i},{i * 2}\n" for i in range(1, 501))
    target.write_text("preamble\nA,B,C\n" + body, encoding="utf-8")

    rows = list(open_csv(target, include_all_rows=True).rows)

    assert [r.physical_line_number for r in rows] == list(range(1, 503))
    assert rows[-1].text == "500,name500,1000"
