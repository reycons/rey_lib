"""What a call passes, one row per argument.

The index recorded that a call was written and what it targeted, never what
reached it -- so ``bridge_index._seam_observations`` walks the AST itself to
find the binding name passed to ``self._call_rows``. These assert the facts
that walk needs, and the identity that lets an argument find its call.

The subject is extraction. Whether the rows promote is asserted against the
database, beside the writer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.repository_map.js_extractor import extract_js_call_arguments
from rey_lib.repository_map.python_extractor import extract_python_call_arguments
from rey_lib.repository_map.records import (
    ARGUMENT_FORM_KEYWORD,
    ARGUMENT_FORM_POSITIONAL,
    ARGUMENT_FORM_VAR_KEYWORD,
    ARGUMENT_FORM_VAR_POSITIONAL,
    EDGE_KIND_CALL,
    EDGE_KIND_GLOBAL_REFERENCE,
    EDGE_KIND_INTERNAL_CALL,
)


def _python(tmp_path: Path, source: str) -> list:
    """Return the call arguments extracted from one Python source string."""
    path = tmp_path / "subject.py"
    path.write_text(source, encoding="utf-8")
    return extract_python_call_arguments(path, "Python", "subject.py")


def _typescript(tmp_path: Path, source: str) -> list:
    """Return the call arguments extracted from one TypeScript source string."""
    path = tmp_path / "subject.ts"
    path.write_text(source, encoding="utf-8")
    return extract_js_call_arguments(path, "TypeScript", "subject.ts")


class TestTheFourForms:
    """One call writing all four, which no call in the estate does."""

    def test_all_four_forms_share_one_ordinal_sequence(self, tmp_path: Path) -> None:
        """The contract, in a single assertion.

        Ordinal runs across the forms rather than restarting per form. A
        restart would put the positional and the ** at ordinal 0 with the
        same absent keyword, and nothing would tell them apart.
        """
        rows = _python(tmp_path, "def g(a, b, items, options):\n"
                                 "    f(a, *items, named=b, **options)\n")

        assert [(r.ordinal, r.argument_form) for r in rows] == [
            (0, ARGUMENT_FORM_POSITIONAL),
            (1, ARGUMENT_FORM_VAR_POSITIONAL),
            (2, ARGUMENT_FORM_KEYWORD),
            (3, ARGUMENT_FORM_VAR_KEYWORD),
        ]

    def test_only_the_keyword_form_carries_a_name(self, tmp_path: Path) -> None:
        """For **options the name is absent, not empty.

        It is supplied at runtime and none is written, which is why the
        keyword name is data and the ordinal is identity.
        """
        rows = _python(tmp_path, "def g(a, b, items, options):\n"
                                 "    f(a, *items, named=b, **options)\n")

        assert [r.keyword for r in rows] == [None, None, "named", None]

    def test_the_star_is_carried_by_the_form_not_the_expression(
        self, tmp_path: Path
    ) -> None:
        """Repeating it would let one fact disagree with itself."""
        rows = _python(tmp_path, "def g(items, options):\n"
                                 "    f(*items, **options)\n")

        assert [r.expression for r in rows] == ["items", "options"]

    def test_two_double_star_arguments_stay_distinct(self, tmp_path: Path) -> None:
        """Three calls in the estate write this, and only ordinal separates them.

        Both carry no keyword name, so a key including the name and not the
        ordinal would collapse them onto one row.
        """
        rows = _python(tmp_path, "def g(first, second):\n"
                                 "    f(**first, **second)\n")

        assert [(r.ordinal, r.expression) for r in rows] == [
            (0, "first"), (1, "second"),
        ]
        assert {r.argument_form for r in rows} == {ARGUMENT_FORM_VAR_KEYWORD}


class TestTheLiteralAndItsRendering:
    """Two columns because one would answer neither question."""

    def test_a_string_literal_is_stored_unquoted_and_rendered_quoted(
        self, tmp_path: Path
    ) -> None:
        """The payoff turns on this.

        Matching a binding name against the rendered expression fails on
        every row: ``ast.unparse`` gives back "'start_batch'" with its quotes.
        """
        row = _python(tmp_path, "f('start_batch')\n")[0]

        assert row.literal_argument == "start_batch"
        assert row.expression == "'start_batch'"

    def test_a_non_string_literal_carries_no_literal_value(
        self, tmp_path: Path
    ) -> None:
        """Its kind says what it is; the column is for names to match on."""
        row = _python(tmp_path, "f(7)\n")[0]

        assert row.argument_kind == "other_literal"
        assert row.literal_argument is None
        assert row.expression == "7"

    def test_a_literal_holding_a_nul_is_declined_not_falsified(
        self, tmp_path: Path
    ) -> None:
        """A NUL is legal in Python and illegal in a text column.

        csv.py really passes '\\x00' as a delimiter, and staging one failed
        the entire insert -- the scan could not publish at all. Stripping it
        would record a value the source does not contain, so the value is
        declined and the escaped rendering still carries it.
        """
        row = _python(tmp_path, "f('a\\x00b')\n")[0]

        assert row.argument_kind == "string_literal"
        assert row.literal_argument is None
        assert "\x00" not in row.expression
        assert "\\x00" in row.expression


class TestItNamesTheEdgeItBelongsTo:
    """Resolution metadata: how an argument finds exactly one call."""

    def test_a_self_call_is_stamped_internal_call(self, tmp_path: Path) -> None:
        """Since backlog 21 these are internal_call, never call.

        Every self._call_rows site in the estate is one, so a promotion
        resolving against 'call' alone would find no edge for any of them.
        """
        rows = _python(tmp_path, "class C:\n"
                                 "    def m(self):\n"
                                 "        self.run('x')\n")

        assert [r.edge_kind for r in rows] == [EDGE_KIND_INTERNAL_CALL]
        assert rows[0].callee == "self.run"

    def test_chained_calls_are_told_apart_by_callee_not_position(
        self, tmp_path: Path
    ) -> None:
        """Path(p).expanduser() is two calls beginning at one line and column.

        Position alone would attach one call's arguments to the other, which
        is why the callee is part of the key.
        """
        rows = _python(tmp_path, "Path(p).relative_to(root)\n")

        assert {(r.source_line, r.source_column) for r in rows} == {(1, 0)}
        assert {(r.callee, r.expression) for r in rows} == {
            ("Path", "p"), ("Path(p).relative_to", "root"),
        }

    def test_a_call_with_no_arguments_produces_no_rows(
        self, tmp_path: Path
    ) -> None:
        """Absence of arguments is not an argument."""
        assert _python(tmp_path, "f()\n") == []


class TestTypeScript:
    """The same record, from a grammar with two of the four forms."""

    def test_spread_is_var_positional_and_loses_its_dots(
        self, tmp_path: Path
    ) -> None:
        """The form carries them, exactly as Python's star is carried."""
        rows = _typescript(tmp_path, "function g(a, items) { f(a, ...items); }\n")

        assert [(r.ordinal, r.argument_form, r.expression) for r in rows] == [
            (0, ARGUMENT_FORM_POSITIONAL, "a"),
            (1, ARGUMENT_FORM_VAR_POSITIONAL, "items"),
        ]

    def test_a_named_object_argument_is_one_positional_argument(
        self, tmp_path: Path
    ) -> None:
        """JavaScript has no keyword-argument grammar.

        An object literal is how it names things, and it is one argument.
        The absence of keyword rows here is the language, not a gap.
        """
        rows = _typescript(tmp_path, "f({named: 1});\n")

        assert [r.argument_form for r in rows] == [ARGUMENT_FORM_POSITIONAL]
        assert rows[0].keyword is None

    def test_a_this_rooted_call_records_nothing(self, tmp_path: Path) -> None:
        """It records no edge either, so a row here would reference nothing.

        This is the case backlog 21 fixed for Python and did not fix here,
        and it is why arguments are emitted only where the edge is.
        """
        assert _typescript(tmp_path, "class C { m(a) { this.hidden(a); } }\n") == []

    def test_a_global_rooted_call_is_stamped_global_reference(
        self, tmp_path: Path
    ) -> None:
        """Rerouted, not dropped -- and 26 calls in the estate are.

        A promotion resolving only 'call' and 'internal_call' would orphan
        every one of them and refuse an entirely correct scan.
        """
        rows = _typescript(tmp_path, 'window.open("x");\n')

        assert [r.edge_kind for r in rows] == [EDGE_KIND_GLOBAL_REFERENCE]
        assert rows[0].literal_argument == "x"


class TestTheExtractorsAgreeWithTheirOwnEdges:
    """The one failure the design cannot rule out by construction.

    An argument resolves to its edge by position, callee and kind. Those come
    from the same classifier the edge branch uses, so they cannot drift --
    unless someone changes one caller and not the other, which is what this
    notices.
    """

    @pytest.mark.parametrize("source, expected", [
        ("f(1)\n", EDGE_KIND_CALL),
        ("class C:\n    def m(self):\n        self.f(1)\n", EDGE_KIND_INTERNAL_CALL),
    ])
    def test_the_kind_stamped_is_the_kind_the_edge_carries(
        self, tmp_path: Path, source: str, expected: str
    ) -> None:
        """Asserted against the reference extractor, not against a constant."""
        from rey_lib.repository_map.python_extractor import extract_python_references

        path = tmp_path / "subject.py"
        path.write_text(source, encoding="utf-8")
        arguments = extract_python_call_arguments(path, "Python", "subject.py")
        edges = [
            edge for edge in extract_python_references(path, "Python", "subject.py")
            if edge.source_line == arguments[0].source_line
            and edge.source_column == arguments[0].source_column
            and edge.to == arguments[0].callee
        ]

        assert [edge.edge_kind for edge in edges] == [expected]
        assert arguments[0].edge_kind == expected
