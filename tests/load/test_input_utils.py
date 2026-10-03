"""parse_column_list, the one input helper the profile path calls.

Copied from the legacy file_operator tests and pointed at the Loader-owned
copy, rey_lib.load.input_utils (row 589, step 5).
"""

from __future__ import annotations

from rey_lib.load.input_utils import parse_column_list


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
