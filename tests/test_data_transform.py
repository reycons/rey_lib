"""What the produced records contain.

The middle of source -> transform -> sink. A file says what it physically
holds; a transform says what the records made from it hold. Those differ in
general -- one configured feed declares 56 output columns over a 47-field
file -- which is why this is not a question a DataFile answers.

On the load path the transform is IDENTITY, because the transform stage ran
earlier and wrote its output to the file being loaded. So what is asserted
here is mostly ``logical_schema``: identity still has to say what the records
contain, and that answer is what a destination gets created from.
"""

from __future__ import annotations

import pytest

from rey_lib.files.data_transform import DataTransform, IdentityTransform


class TestIdentityIsActuallyIdentity:
    """It changes nothing, and that is a property worth pinning."""

    def test_the_records_come_back_unchanged(self) -> None:
        records = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]

        assert IdentityTransform().transform(records) == records

    def test_it_does_not_copy_or_reorder(self) -> None:
        """The load path materialises rows once and hands them on.

        A transform that rebuilt the list would double the memory a load
        holds for no gain, and one that reordered would break the insert
        order the column list was derived from.
        """
        records = [{"a": 1}, {"a": 2}]

        assert IdentityTransform().transform(records) is records


class TestTheLogicalSchema:
    """What a destination is created from when there is no table yet."""

    def test_columns_come_from_the_first_record(self) -> None:
        """What the loader has always done.

        Which schema is authoritative -- the file's or the configured column
        list -- is a separate decision, deliberately not taken while this is
        only being moved.
        """
        defs = IdentityTransform().logical_schema([{"b": "x", "a": 1}])

        assert [name for name, _type in defs] == ["b", "a"]

    def test_no_records_is_no_schema(self) -> None:
        """Not an error: a load rejects an empty file before reaching here."""
        assert IdentityTransform().logical_schema([]) == []

    @pytest.mark.parametrize(
        "declared,expected",
        [
            ({"type": "date"}, "DATE"),
            ({"type": "datetime"}, "DATETIME2"),
            ({"type": "numeric"}, "DECIMAL(18, 6)"),
            ({"type": "regex_extract", "cast_to": "float"}, "DECIMAL(18, 6)"),
            ({"type": "regex_extract", "cast_to": "integer"}, "INTEGER"),
        ],
    )
    def test_a_DECLARED_type_wins_over_the_values(
        self, declared: dict, expected: str
    ) -> None:
        """Configuration knows things the data does not.

        A column of "1", "2", "3" that a config calls a date is a date; the
        values would otherwise infer as an integer and the destination would
        be wrong in a way nothing later could detect.
        """
        transform = IdentityTransform({"value": declared})

        defs = transform.logical_schema([{"value": "1"}, {"value": "2"}])

        assert defs == [("value", expected)]

    def test_an_undeclared_column_is_inferred_from_its_values(self) -> None:
        """The fallback, and the common case: nothing declares anything."""
        defs = IdentityTransform().logical_schema(
            [{"n": "1"}, {"n": "2"}, {"n": "3"}]
        )

        assert defs[0][0] == "n"
        assert defs[0][1] != ""

    def test_text_that_infers_as_nothing_becomes_a_varchar_with_headroom(
        self
    ) -> None:
        """Sized from what was observed, plus room, with a floor.

        The headroom is why a slightly longer value in a later delivery does
        not immediately fail the insert.
        """
        defs = IdentityTransform().logical_schema([{"t": "abcdefghijklmno"}])

        assert defs == [("t", "VARCHAR(25)")]

    def test_a_short_column_still_gets_the_minimum_width(self) -> None:
        defs = IdentityTransform().logical_schema([{"t": "ab!"}])

        assert defs == [("t", "VARCHAR(20)")]

    def test_the_widest_value_decides_the_width(self) -> None:
        """Every record, not the first -- the first may be the short one."""
        defs = IdentityTransform().logical_schema(
            [{"t": "ab!"}, {"t": "a much longer value than that one"}]
        )

        assert defs == [("t", "VARCHAR(43)")]

    def test_a_declared_column_absent_from_the_records_is_not_invented(
        self
    ) -> None:
        """The records decide WHICH columns exist; config decides their TYPE.

        A configured column that the file does not carry is not conjured into
        the destination -- on this path the records are already the transform
        stage's output, so a column it did not produce is not one to create.
        """
        transform = IdentityTransform({"never_here": {"type": "date"}})

        defs = transform.logical_schema([{"a": "1"}])

        assert [name for name, _type in defs] == ["a"]


class TestTheBoundary:
    """What this object is allowed to know."""

    def test_it_does_not_reach_back_into_the_loader(self) -> None:
        """Same property the DataFile package holds.

        The transform is built on by DataLoader next; a transform importing
        the procedural loader would put the dependency the wrong way round
        and make the sink object impossible to extract cleanly.
        """
        from pathlib import Path as _Path

        import rey_lib.files.data_transform as module

        source = _Path(module.__file__).read_text(encoding="utf-8")

        assert "file_loader" not in source
        assert "rey_lib.db" not in source

    def test_the_contract_demands_both_answers(self) -> None:
        """A transform that only mapped records could not create a table."""
        assert hasattr(DataTransform, "transform")
        assert hasattr(DataTransform, "logical_schema")
