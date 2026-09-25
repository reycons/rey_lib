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
from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.data import ColumnTransform, DeclaredTransform, IdentityTransform
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
