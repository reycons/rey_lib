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

from rey_lib.data.data_transform import (
    IDENTITY_EXECUTION,
    DataTransform,
    ExecutionForm,
    IdentityTransform,
)


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

        import rey_lib.data.data_transform as module

        source = _Path(module.__file__).read_text(encoding="utf-8")

        assert "file_loader" not in source
        assert "rey_lib.db" not in source

    def test_the_contract_demands_both_answers(self) -> None:
        """A transform that only mapped records could not create a table."""
        assert hasattr(DataTransform, "transform")
        assert hasattr(DataTransform, "logical_schema")


class TestTheOptionalExecutionForm:
    """A transform may offer an equivalent non-row form. Most will not.

    The contract used to be list[dict] -> list[dict] and nothing else, so an
    implementation with an equivalent form that something could execute
    without materialising had no way to say so. The interface forced the
    materialisation.

    What it is NOT: a claim about speed. A transform cannot see the provider,
    the target or the data volume, so it cannot know whether its form is
    worth using. Whatever executes decides that.
    """

    def test_a_transform_without_one_answers_none(self) -> None:
        """And inherits the answer rather than restating it.

        Concrete on the base, so every transform written before this existed
        keeps working and a new one is not made to answer a question it has
        no answer to.
        """
        class _Bare(DataTransform):
            def transform(self, records):
                return records

            def logical_schema(self, records):
                return []

        assert _Bare().execution_form() is None
        assert "execution_form" not in vars(_Bare)

    def test_identity_answers_the_identity_form(self) -> None:
        assert IdentityTransform().execution_form() is IDENTITY_EXECUTION

    def test_the_form_is_identity_even_when_columns_are_declared(self) -> None:
        """THE TRAP, pinned so it cannot be walked into later.

        Declared columns make `ExecutionForm(columns=...)` look like the
        natural description of this transform. It would describe a DIFFERENT
        OPERATION -- see the next test -- so the form is identity either way.
        """
        declared = IdentityTransform(columns=["id", "name"])

        assert declared.execution_form() is IDENTITY_EXECUTION
        assert declared.execution_form() is IdentityTransform().execution_form()

    def test_transform_keeps_a_key_configuration_never_declared(self) -> None:
        """WHY the form is not a projection, asserted rather than asserted-in-prose.

        `transform` returns records untouched. A projection over the declared
        columns would drop `extra`; this does not. Two different operations,
        so one cannot describe the other.

        `logical_schema` does refuse this mismatch -- but in another method,
        after this one, and on a path that never materialises records it may
        not run at all. That is precisely where a form would be used, so the
        refusal cannot be what makes a projection safe.
        """
        records = [{"id": 1, "name": "x", "extra": "y"}]

        produced = IdentityTransform(columns=["id", "name"]).transform(records)

        assert produced == [{"id": 1, "name": "x", "extra": "y"}]
        assert "extra" in produced[0]

    def test_the_form_describes_semantics_and_nothing_else(self) -> None:
        """It carries no fields, and that is the design rather than a stub.

        A column list is earned by the first transform whose ROW behaviour
        projects; a predicate by the first that filters. Describing shapes no
        implementation has would be guessing at semantics nothing can check.
        """
        assert not getattr(ExecutionForm, "__dataclass_fields__", {})
        assert ExecutionForm() == ExecutionForm()

    def test_the_module_depends_on_no_database_or_provider(self) -> None:
        """NEUTRALITY, guarded over dependencies rather than over prose.

        The rule: this module carries no provider-specific implementation, no
        provider identifier, no SQL expression and no SQL execution contract.

        Asserted on IMPORTS, which cannot be satisfied by accident. A guard
        banning the word "provider" or scanning for SQL keywords would fire
        on the docstrings that explain the rule -- which happened once
        already today, when the transfer step's format guard tripped over its
        own comment.
        """
        from pathlib import Path as _Path

        import rey_lib.data.data_transform as module

        source = _Path(module.__file__).read_text(encoding="utf-8")
        imports = [
            line.strip() for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]

        assert imports, "no imports found -- the guard read the wrong thing"
        for line in imports:
            assert "rey_lib.db" not in line, line
            for engine in ("duckdb", "postgres", "mysql", "sqlserver", "sqlalchemy"):
                assert engine not in line.lower(), line
