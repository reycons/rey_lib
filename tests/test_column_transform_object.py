"""The transform object owns the column-transformation behaviour.

It was a set of functions beside a file reader, so only a configured file feed
could reach it and the transform object in the load triple applied nothing.
The behaviour did not change when it moved; its OWNER did.

So most of what is asserted here is about OWNERSHIP rather than about dates and
regexes -- the rey_loader transform suite already proves those, unedited, which
is the evidence that nothing moved except the code.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.data import (
    ColumnTransform,
    DeclaredTransform,
    IdentityTransform,
    OUTPUT_DATATYPES,
    is_exported,
    authorable_starters,
)
from rey_lib.data.errors import TransformError

REY_LIB = Path(__file__).resolve().parents[1] / "rey_lib"


def _declaration(*columns, **rest):
    return {"columns": list(columns), **rest}


class TestItOwnsItsDeclaration:
    """Objectified, not relocated."""

    def test_transform_takes_records_and_nothing_else(self) -> None:
        """A CLASS THAT READ ITS CONFIGURATION PER CALL would be a namespace.

        The old entry point was `transform_row(raw_row, file_type_cfg, row_num,
        ctx)` -- the declaration arrived with every record. This is the whole
        difference the move makes, so it is asserted on the signature.
        """
        taken = list(inspect.signature(ColumnTransform.transform).parameters)

        assert taken == ["self", "records"]

    def test_the_declaration_arrives_once_at_construction(self) -> None:
        held = ColumnTransform(_declaration({"name": "a", "source": "A"}))

        assert held.declaration["columns"][0]["name"] == "a"

    def test_it_answers_for_its_own_output_from_that_declaration(self) -> None:
        """The schema half derives from the SAME declaration the rules do.

        A caller resolving the columns separately and handing them in would be
        a second copy, free to disagree with what is actually applied.
        """
        held = ColumnTransform(_declaration(
            {"name": "a", "source": "A"},
            {"name": "b", "source": "B", "transform": {"type": "numeric"}},
        ))

        assert held.columns == ["a", "b"]
        assert held.column_transforms == {"b": {"type": "numeric"}}

    def test_it_counts_its_own_rows(self) -> None:
        """`row_num` was an argument. It is the object's now."""
        held = ColumnTransform(_declaration(
            {"name": "n", "transform": {"type": "context", "value": "ctx.row_num"}},
        ))

        assert held.transform([{}, {}, {}]) == [{"n": 1}, {"n": 2}, {"n": 3}]

    def test_the_count_is_the_position_in_what_was_read(self) -> None:
        """NOT the position among survivors.

        A filtered row still happened. Counting survivors would renumber every
        row after the first blank line, and `ctx.row_num` is read back by an
        operator looking for a row in a file.
        """
        held = ColumnTransform(_declaration(
            {"name": "n", "transform": {"type": "context", "value": "ctx.row_num"}},
            {"name": "keep", "source": "keep"},
            row_filter={"column": "keep", "type": "not_blank"},
        ))

        kept = held.transform([{"keep": "y"}, {"keep": ""}, {"keep": "y"}])

        assert [one["n"] for one in kept] == [1, 3]


class TestSecretsAreADependencyNotADeclaration:
    """The boundary that makes browser authoring answerable later."""

    def test_they_are_supplied_at_construction(self) -> None:
        held = ColumnTransform(
            _declaration({"name": "a", "source": "A"}), secrets={"K": "v"},
        )

        assert held.secrets == {"K": "v"}

    def test_a_declaration_carrying_its_own_secrets_is_ignored(self) -> None:
        """A DECLARATION IS A DESCRIPTION OF WORK, not a credential.

        The old code read `file_type_cfg["secrets"]` -- the declaration and the
        trusted configuration were the same object, so it could not tell them
        apart. They are separate now, and this is what says the object trusts
        only what it was given.
        """
        held = ColumnTransform(
            _declaration({"name": "a", "source": "A"}, secrets={"K": "smuggled"}),
        )

        assert held.secrets == {}


class TestItKnowsNothingAboutFiles:

    def test_it_names_a_source_by_key(self) -> None:
        held = ColumnTransform(_declaration({"name": "out", "source": "In Field"}))

        assert held.transform([{"In Field": "v"}]) == [{"out": "v"}]

    def test_the_module_imports_nothing_from_the_file_family(self) -> None:
        """The layering, guarded over IMPORTS rather than prose.

        `rey_lib/data/` is the bottom of that stack. A transform reaching into
        one implementation family would be exactly the inversion the data layer
        exists to prevent.
        """
        source = (REY_LIB / "data" / "column_transform.py").read_text(encoding="utf-8")

        assert "rey_lib.files" not in source
        assert "rey_lib.db" not in source

    def test_no_transformation_type_is_implemented_at_the_file_boundary(self) -> None:
        """THE ARCHITECTURAL TEST.

        Stated as an invariant rather than a word search: the file module may
        still RECOGNISE a format -- that is the parsing it keeps -- but no
        transformation type may be implemented there.
        """
        source = (REY_LIB / "files" / "transformer.py").read_text(encoding="utf-8")

        for gone in (
            "def _transform_date", "def _transform_datetime", "def _transform_time",
            "def _transform_numeric", "def _transform_regex_extract",
            "def _transform_prefix_map", "def _transform_regex_date",
            "def _transform_encrypt", "def _transform_strip_parens",
            "def _compute_hash", "def _apply_transform_v2",
        ):
            assert gone not in source, gone

    def test_the_old_per_record_entry_point_is_gone(self) -> None:
        """Owner -> repoint -> zero callers -> delete."""
        import rey_lib.files as files
        import rey_lib.files.transformer as transformer

        assert not hasattr(transformer, "transform_row")
        assert not hasattr(files, "transform_row")


class TestItIsNotAnIdentityTransform:

    def test_both_answer_for_their_output_through_one_base(self) -> None:
        """The half they share, and the reason they can share it.

        A declaration says what the output columns are whether the transform
        then passes records through or rewrites them.
        """
        assert issubclass(ColumnTransform, DeclaredTransform)
        assert issubclass(IdentityTransform, DeclaredTransform)
        assert not issubclass(ColumnTransform, IdentityTransform)

    def test_it_claims_no_equivalent_non_row_form(self) -> None:
        """THE DANGEROUS ONE.

        `IdentityTransform` answers IDENTITY_EXECUTION, and a loader may use
        that form to let a provider read the source itself -- skipping
        `transform()` entirely. A transform that APPLIES rules and inherited
        that answer would have its rules silently not applied.
        """
        held = ColumnTransform(_declaration({"name": "a", "source": "A"}))

        assert held.execution_form() is None
        assert IdentityTransform().execution_form() is not None

    def test_identity_still_applies_nothing(self) -> None:
        """Unchanged, and still correct: its stage already ran."""
        records = [{"a": 1, "extra": 2}]

        assert IdentityTransform().transform(records) == records


class TestWhatItRaises:

    def test_an_unknown_rule_names_the_column(self) -> None:
        held = ColumnTransform(_declaration(
            {"name": "amount", "source": "A", "transform": {"type": "nonsense"}},
        ))

        with pytest.raises(TransformError) as raised:
            held.transform([{"A": "1"}])

        assert raised.value.column == "amount"
        assert "amount" in str(raised.value)


class TestTheRulesThemselvesStillWork:
    """A thin pass. The rey_loader suite is the real proof, unedited."""

    def test_a_date_rule(self) -> None:
        held = ColumnTransform(_declaration(
            {"name": "d", "source": "D",
             "transform": {"type": "date", "format": "MM/dd/yyyy"}},
        ))

        assert held.transform([{"D": "03/04/2026"}]) == [{"d": "2026-03-04"}]

    def test_a_constant_resolves_a_context_token(self) -> None:
        held = ColumnTransform(
            _declaration({"name": "who",
                          "transform": {"type": "constant", "value": "{app_name}"}}),
            context=SimpleNamespace(app_name="rey_loader"),
        )

        assert held.transform([{}]) == [{"who": "rey_loader"}]

    def test_a_hash_is_taken_across_finished_columns(self) -> None:
        """Deferred to a second pass, which is the declaration's meaning.

        A hash declared anywhere but last still hashes finished values.
        """
        held = ColumnTransform(_declaration(
            {"name": "key",
             "transform": {"type": "hash", "columns": ["a", "b"]}},
            {"name": "a", "source": "A"},
            {"name": "b", "source": "B"},
        ))

        produced = held.transform([{"A": "1", "B": "2"}])[0]

        assert produced["a"] == "1" and produced["b"] == "2"
        assert produced["key"] and produced["key"] != "key"


class TestTheStartersItPublishesForAuthoring:
    """Starting declarations a SURFACE may offer, beside the dispatch.

    They sit here because the dispatch owns the types. A surface holding its own
    list would be a second answer about what a transform is, free to drift from
    the one that executes -- and free to re-offer what this deliberately
    withholds.
    """

    def test_every_starter_names_a_type_the_dispatch_applies(self) -> None:
        """A starter for a type nothing implements is a control that does
        nothing, which is the whole reason the list lives beside the dispatch.
        """
        for name, declaration in authorable_starters().items():
            held = ColumnTransform(
                _declaration({"name": "out", "source": "IN",
                              "transform": declaration}),
            )
            try:
                held.transform([{"IN": "01/02/2024"}])
            except TransformError as exc:
                assert "Unknown transform type" not in str(exc), name

    def test_every_starter_declares_its_own_name_as_its_type(self) -> None:
        for name, declaration in authorable_starters().items():
            assert declaration["type"] == name

    def test_encrypt_is_not_offered_for_authoring(self) -> None:
        """Not a claim that it is invalid -- it stays legal in a declaration.

        It resolves `key_env` against a secrets map supplied at construction, so
        a declaration naming one is only as good as the store the thing that
        built it can reach. What is missing is the rule for a declaration
        arriving from a BROWSER: which namespace it may name, and how those
        names are offered without their values. Until that exists, offering the
        starter would advertise a capability that is not whole.
        """
        assert "encrypt" not in authorable_starters()

    def test_encrypt_still_applies_when_a_declaration_names_it(self) -> None:
        """The withholding is about AUTHORING, not about execution."""
        held = ColumnTransform(
            _declaration({"name": "secret", "source": "IN",
                          "transform": {"type": "encrypt", "key_env": "NOPE"}}),
        )

        # It reaches the encrypt rule and fails on the missing KEY, which is the
        # proof it was dispatched rather than rejected as unknown.
        with pytest.raises(TransformError, match="NOPE"):
            held.transform([{"IN": "value"}])

    def test_each_caller_is_handed_its_own_copy(self) -> None:
        """A starter a caller edited would be the starter every later caller
        got, and the containers are what an author is most likely to fill.
        """
        first, second = authorable_starters(), authorable_starters()

        first["prefix_map"]["prefixes"]["BUY"] = "Buy"
        first["hash"]["columns"].append("a")

        assert second["prefix_map"]["prefixes"] == {}
        assert second["hash"]["columns"] == []


class TestExportSelectsTheOutputWithoutChangingExecution:
    """``export`` is a projection, not an execution switch.

    Every declared entry is computed; only the exported ones come out. That
    ordering is the whole of what the property means -- dropping a column where
    it was computed would make an intermediate impossible to declare at all.
    """

    @staticmethod
    def _with_an_intermediate():
        return _declaration(
            {"source": "A", "name": "a"},
            {"source": "B", "name": "working", "export": False},
            {"name": "digest",
             "transform": {"type": "hash", "columns": ["a", "working"]}},
        )

    def test_an_unexported_column_is_not_in_the_produced_record(self) -> None:
        held = ColumnTransform(self._with_an_intermediate())

        assert list(held.transform([{"A": "1", "B": "2"}])[0]) == ["a", "digest"]

    def test_nor_in_the_columns_it_says_it_produces(self) -> None:
        held = ColumnTransform(self._with_an_intermediate())

        assert held.columns_for_names(["A", "B"]) == ["a", "digest"]

    def test_nor_in_the_schema(self) -> None:
        held = ColumnTransform(self._with_an_intermediate())
        produced = held.transform([{"A": "1", "B": "2"}])

        assert [name for name, _type in held.logical_schema(produced)] == [
            "a", "digest",
        ]

    def test_but_it_was_computed_and_a_later_entry_read_it(self) -> None:
        """THE CASE THAT DECIDES THE SEMANTICS.

        A hash over an unexported column must differ when that column's value
        differs. If it did not, the column was never computed and ``export``
        would have been an execution switch.
        """
        held = ColumnTransform(self._with_an_intermediate())

        one = held.transform([{"A": "1", "B": "2"}])[0]["digest"]
        other = ColumnTransform(self._with_an_intermediate()).transform(
            [{"A": "1", "B": "DIFFERENT"}],
        )[0]["digest"]

        assert one != other

    def test_absent_means_exported(self) -> None:
        """Every declaration written before the property existed keeps its
        meaning, which is what makes this additive.
        """
        held = ColumnTransform(_declaration({"source": "A", "name": "a"}))

        assert is_exported({"name": "a"})
        assert held.columns_for_names(["A"]) == ["a"]

    def test_exporting_nothing_declares_no_columns_rather_than_no_transform(
        self,
    ) -> None:
        """All entries unexported is a declaration that produces nothing, and
        the record it returns is empty rather than the source's own.
        """
        held = ColumnTransform(
            _declaration({"source": "A", "name": "a", "export": False}),
        )

        assert held.transform([{"A": "1"}]) == [{}]


class TestADeclaredOutputDatatype:
    """What the output is INTENDED to be, which beats what it looks like."""

    def test_a_stated_type_wins_over_the_derived_one(self) -> None:
        held = ColumnTransform(
            _declaration({"source": "A", "name": "a", "datatype": "text"}),
        )
        produced = held.transform([{"A": "123"}])

        # Derived, this is an INTEGER -- the values say so. Declared, it is text.
        assert held.logical_schema(produced) == [("a", "VARCHAR(20)")]

    def test_absent_leaves_the_derivation_exactly_as_it_was(self) -> None:
        held = ColumnTransform(_declaration({"source": "A", "name": "a"}))
        produced = held.transform([{"A": "123"}])

        assert held.logical_schema(produced) == [("a", "INTEGER")]

    def test_every_published_datatype_renders(self) -> None:
        """A vocabulary member with no rendering is a control that produces a
        schema nobody can apply.
        """
        for datatype in OUTPUT_DATATYPES:
            held = ColumnTransform(
                _declaration({"source": "A", "name": "a", "datatype": datatype}),
            )
            rendered = held.logical_schema(held.transform([{"A": "123"}]))

            assert rendered and rendered[0][1], datatype

    def test_boolean_is_not_published(self) -> None:
        """It is the one member that would mean inventing a physical type in
        shared code. Deferred to the row that would give a provider somewhere
        to answer.
        """
        assert "boolean" not in OUTPUT_DATATYPES


class TestTheStartersOfferBothFormats:
    """`format` reads the value; `output_format` writes it.

    Only the first was offered, so the one that decides what a reader SEES --
    in a preview and in the loaded rows alike -- could be reached only by
    knowing the key existed.
    """

    #: Each with a value that rule can actually read -- a time rule is given a
    #: time, not a whole timestamp.
    TIME_SHAPED = (
        ("date", "2026-09-26T11:04:00"),
        ("datetime", "2026-09-26T11:04:00"),
        ("time", "11:04:00"),
    )

    def test_each_time_shaped_starter_offers_the_output_format(self) -> None:
        for name, _value in self.TIME_SHAPED:
            assert "output_format" in authorable_starters()[name], name

    def test_seeding_it_changes_nothing_for_an_author_who_leaves_it(self) -> None:
        """Each is seeded with the ANSI spelling that rule already falls back
        to, so adding the key moved no output.
        """
        for name, value in self.TIME_SHAPED:
            seeded = ColumnTransform(_declaration(
                {"source": "V", "name": "x", "transform": authorable_starters()[name]},
            )).transform([{"V": value}])[0]["x"]

            bare = dict(authorable_starters()[name])
            del bare["output_format"]
            without = ColumnTransform(_declaration(
                {"source": "V", "name": "x", "transform": bare},
            )).transform([{"V": value}])[0]["x"]

            assert seeded == without, name

    def test_an_authors_own_output_format_is_what_is_written(self) -> None:
        """The point of offering it: the value a reader sees is theirs to set,
        and it is the transform that decides -- not a second formatter reading
        the same declaration somewhere else.
        """
        declared = dict(authorable_starters()["date"])
        declared["format"] = "yyyy-MM-dd"
        declared["output_format"] = "MM/dd/yyyy"

        produced = ColumnTransform(_declaration(
            {"source": "V", "name": "x", "transform": declared},
        )).transform([{"V": "2026-09-26T11:04:00"}])

        assert produced == [{"x": "09/26/2026"}]


class TestARuleIsGivenTextWhateverTheSourceHeld:
    """Every rule guards its input with `isinstance(value, str) else ""`.

    That held while the only source was a FILE, where each field is text
    already. A DATABASE source hands back what its driver holds, and the guard
    turned those into an empty string -- so a rule failed, or nulled the column,
    over a value that was perfectly good.
    """

    @staticmethod
    def _declared(transform):
        return _declaration({"source": "V", "name": "out", "transform": transform})

    def test_a_date_rule_reads_a_driver_datetime(self) -> None:
        """The case that reached a reader: `date` on a TIMESTAMP column came
        back "Empty date value and allow_blank is False".
        """
        held = ColumnTransform(self._declared(
            {"type": "date", "format": "yyyy-MM-dd", "output_format": "MM/dd/yyyy"},
        ))

        produced = held.transform([{"V": datetime(2025, 1, 1, 9, 30)}])

        assert produced == [{"out": "01/01/2025"}]

    def test_it_no_longer_nulls_the_column_where_blanks_are_allowed(self) -> None:
        """WORSE THAN THE ERROR. With `allow_blank`, the empty string was
        accepted and the column became null -- a real value replaced by nothing,
        with nothing raised and nothing logged above debug.
        """
        held = ColumnTransform(self._declared(
            {"type": "date", "format": "yyyy-MM-dd", "allow_blank": True},
        ))

        assert held.transform([{"V": date(2025, 1, 1)}]) == [{"out": "2025-01-01"}]

    def test_a_numeric_rule_reads_a_driver_decimal(self) -> None:
        held = ColumnTransform(self._declared({"type": "numeric"}))

        assert held.transform([{"V": Decimal("1234.50")}]) == [{"out": 1234.5}]

    def test_a_blank_value_is_still_blank(self) -> None:
        """None is absence and stays absence: it is not rendered as "None" and
        handed to a rule as though somebody had typed the word.
        """
        held = ColumnTransform(self._declared(
            {"type": "date", "format": "yyyy-MM-dd", "allow_blank": True},
        ))

        assert held.transform([{"V": None}]) == [{"out": None}]

    def test_a_pass_through_column_keeps_the_value_it_was_given(self) -> None:
        """UNTOUCHED, and deliberately: a column nobody declared a rule for is
        carried to a destination that has a schema for it, so what a load writes
        for one does not change.
        """
        when = datetime(2025, 1, 1, 9, 30)
        held = ColumnTransform(_declaration({"source": "V", "name": "out"}))

        assert held.transform([{"V": when}]) == [{"out": when}]


class TestATemporalValueIsNotRoundTrippedThroughText:
    """A driver's value is already what the rule was going to parse FOR.

    Rendering it to text so a text parser can rebuild the same object can only
    lose: `str()` on a timezone-aware value with microseconds gives
    `2026-09-05 12:23:15.001322-04:00`, which no format in the fallback list
    matches. A value needing no parsing at all failed to parse.
    """

    AWARE = datetime(
        2026, 9, 5, 12, 23, 15, 1322, tzinfo=timezone(timedelta(hours=-4)),
    )

    @staticmethod
    def _run(transform, value):
        return ColumnTransform(_declaration(
            {"source": "V", "name": "o", "transform": transform},
        )).transform([{"V": value}])[0]["o"]

    def test_the_value_a_reader_actually_hit(self) -> None:
        assert self._run(
            {"type": "date", "format": "MM/dd/yyyy", "output_format": "MM/dd/yyyy"},
            self.AWARE,
        ) == "09/05/2026"

    def test_a_date_rule_takes_the_date_and_a_time_rule_the_time(self) -> None:
        """The rule's own instruction, not a loss: a column declared a date is
        a date.
        """
        assert self._run({"type": "date", "format": "yyyy-MM-dd"}, self.AWARE) \
            == "2026-09-05"
        assert self._run({"type": "time", "format": "HH:mm:ss"}, self.AWARE) \
            == "12:23:15"

    def test_a_datetime_rule_widens_a_bare_date(self) -> None:
        assert self._run(
            {"type": "datetime", "format": "yyyy-MM-dd HH:mm:ss"}, date(2025, 1, 1),
        ) == "2025-01-01 00:00:00"

    def test_text_is_still_parsed_exactly_as_before(self) -> None:
        """The path every configured file feed takes is untouched."""
        assert self._run(
            {"type": "date", "format": "yyyy-MM-dd", "output_format": "MM/dd/yyyy"},
            "2025-01-01",
        ) == "01/01/2025"

    def test_the_rules_that_re_read_the_raw_record_cope_too(self) -> None:
        """`regex_extract`, `regex_date` and `prefix_map` do not use the value
        they are handed -- they go back to the record for a field their own
        declaration names -- so the dispatcher's rendering never reached them
        and `.strip()` was called on a datetime.
        """
        assert self._run(
            {"type": "regex_extract", "source": "V", "pattern": r"(\d{4})"},
            self.AWARE,
        ) == "2026"
        assert self._run(
            {"type": "regex_date", "source": "V",
             "pattern": r"(\d{4}-\d{2}-\d{2})", "format": "yyyy-MM-dd"},
            self.AWARE,
        ) == "2026-09-05"
        assert self._run(
            {"type": "prefix_map", "source": "V",
             "prefixes": {"2026": "this year"}, "default": "other"},
            self.AWARE,
        ) == "this year"
