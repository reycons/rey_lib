"""Where a field SITS in a file, answered before a transform sees it.

The counterpart of moving column transformation to the transform object. A
transform names a source by key; a positional or fixed-width file names one by
index or by offsets. Reconciling those is file parsing, so it happens here and
the transform takes no positional branch at all.
"""

from __future__ import annotations

from rey_lib.files.transformer import keyed_record


class TestAKeyedRowIsAlreadyDone:

    def test_a_delimited_header_row_passes_through(self) -> None:
        row = {"Account Number": "1", "Amount": "2"}

        assert keyed_record(row, {"file_type": "delimited_header"}) == row

    def test_it_keeps_fields_the_declaration_never_names(self) -> None:
        """REBUILDING IT FROM THE DECLARED SOURCES WOULD DROP THEM.

        `regex_extract`, `prefix_map` and `regex_date` are handed the whole
        record rather than one value, so a rule may read a field no column
        declares. Narrowing the record here would break them from a distance.
        """
        kept = keyed_record(
            {"A": "1", "unnamed": "keep me"},
            {"file_type": "CSV", "columns": [{"name": "a", "source": "A"}]},
        )

        assert kept["unnamed"] == "keep me"


class TestAPositionalRowIsKeyedHere:

    def test_a_headerless_row_is_keyed_by_its_declared_source(self) -> None:
        kept = keyed_record(
            ["first", "second", "third"],
            {"file_type": "delimited_no_header",
             "columns": [{"name": "b", "source": 2}]},
        )

        assert kept == {"2": "second"}

    def test_a_fixed_width_line_is_keyed_by_its_offsets(self) -> None:
        kept = keyed_record(
            "ABCDEFGHIJ",
            {"file_type": "fixed_width",
             "columns": [{"name": "mid", "source": {"start": 3, "end": 6}}]},
        )

        assert kept == {"{'start': 3, 'end': 6}": "CDEF"}

    def test_a_column_naming_no_source_is_not_keyed(self) -> None:
        """A constant or a context value has nowhere in the file to come from."""
        kept = keyed_record(
            ["only"],
            {"file_type": "delimited_no_header",
             "columns": [{"name": "c", "transform": {"type": "constant", "value": "x"}}]},
        )

        assert kept == {}


class TestWhatTheBoundaryProduces:

    def test_a_transform_reads_it_without_knowing_the_format(self) -> None:
        """THE POINT OF THE SPLIT, end to end.

        The same declaration, two formats, one transform object -- and the
        object is told nothing about either file.
        """
        from rey_lib.data import ColumnTransform

        declaration = {"columns": [{"name": "out", "source": 1}]}
        transform = ColumnTransform(declaration)

        positional = keyed_record(
            ["v"], {"file_type": "delimited_no_header", **declaration},
        )

        assert transform.transform([positional]) == [{"out": "v"}]
