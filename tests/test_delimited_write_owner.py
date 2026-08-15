"""Behavioural pin for the delimited-write ownership migration.

Contract: rey_architecture_enforcement_layer.sgc.yaml (INC-007)
Migration: delimited_write_to_csv_owner

The migration moved ownership, not behaviour. This module writes real feed
files, so the bytes must be identical to what file_utils._csv_writer produced.
Both quoting modes are pinned, because the default QUOTE_NONE exists so
configured constants carrying their own quote characters are written verbatim —
re-quoting them would corrupt live data.
"""

from __future__ import annotations

from pathlib import Path

from rey_lib.files.csv import write_delimited_rows


def test_default_quoting_writes_values_verbatim(tmp_path: Path) -> None:
    """QUOTE_NONE: a value that already carries quotes is not re-quoted."""
    out = tmp_path / "out.csv"

    write_delimited_rows(out, [{"a": '"quoted"', "b": "plain"}])

    # Bytes, not text: read_text applies universal-newline translation and would
    # hide a change to the line terminator, which is part of the format.
    assert out.read_bytes() == b'a,b\r\n"quoted",plain\r\n'


def test_minimal_quoting_protects_embedded_delimiters(tmp_path: Path) -> None:
    """QUOTE_MINIMAL: free text containing the delimiter stays one field."""
    out = tmp_path / "out.csv"

    write_delimited_rows(out, [{"a": "x,y", "b": "z"}], minimal_quoting=True)

    assert out.read_bytes() == b'a,b\r\n"x,y",z\r\n'


def test_the_first_row_fixes_the_header_and_column_order(tmp_path: Path) -> None:
    """Header order is the first row's key order, not sorted."""
    out = tmp_path / "out.csv"

    write_delimited_rows(out, [{"z": "1", "a": "2"}, {"z": "3", "a": "4"}])

    assert out.read_bytes().split(b"\r\n")[0] == b"z,a"


def test_parent_directories_are_created(tmp_path: Path) -> None:
    """The capability moved with the writer."""
    out = tmp_path / "nested" / "deep" / "out.csv"

    write_delimited_rows(out, [{"a": "1"}])

    assert out.is_file()


def test_the_retired_owner_is_gone(tmp_path: Path) -> None:
    """Proven zero callers, then deleted. It must not come back as a shim."""
    from rey_lib.files import file_utils

    assert not hasattr(file_utils, "_csv_writer")
