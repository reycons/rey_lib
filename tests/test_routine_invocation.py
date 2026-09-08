"""A routine carries the invocation its provider rendered.

The renderer is PostgreSQL's own. These tests hold it to the cases that decide
whether a rendered call reaches the routine it names -- and hold the contract
around it to the promise that an unrendered one is an ordinary answer, not a
failure to paper over.
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.db import postgres_utils
from rey_lib.db.database_objects import DatabaseFunction, DatabaseProcedure
from rey_lib.db.database_objects import DatabaseRoutineIdentity


def _render(
    kind: str = "function",
    qualified: str = "control.f_thing",
    returns_set: bool = False,
    positional: str | None = None,
    named: str | None = None,
    all_named: bool | None = None,
    has_variadic: bool | None = None,
) -> str:
    """Render one invocation the way ``list_routines`` renders it."""
    return postgres_utils._routine_invocation(
        kind=kind,
        qualified=qualified,
        returns_set=returns_set,
        positional=positional,
        named=named,
        all_named=all_named,
        has_variadic=has_variadic,
    )


def _args(positional: str, named: str) -> dict[str, Any]:
    """One argument list in both forms, as the query supplies them."""
    return {"positional": positional, "named": named, "all_named": True}


class TestShape:
    """A procedure is called; a function is selected, from or as."""

    def test_scalar_function_is_selected(self) -> None:
        assert _render() == "SELECT control.f_thing();"

    def test_set_returning_function_is_selected_from(self) -> None:
        assert _render(returns_set=True) == "SELECT * FROM control.f_thing();"

    def test_procedure_is_called(self) -> None:
        rendered = _render(kind="procedure", qualified="control.p_do")
        assert rendered == "CALL control.p_do();"

    def test_a_procedure_is_never_selected(self) -> None:
        # The shapes are not interchangeable: SELECTing a procedure is an
        # error, so the kind must reach the template rather than a default.
        assert "SELECT" not in _render(kind="procedure")


class TestNotation:
    """Named where the arguments are named, positional where they are not."""

    def test_named_arguments_are_written_by_name(self) -> None:
        rendered = _render(**_args("NULL::bigint", "p_id => NULL::bigint"))
        assert rendered == "SELECT control.f_thing(p_id => NULL::bigint);"

    def test_unnamed_arguments_fall_back_to_position(self) -> None:
        rendered = _render(
            positional="NULL::bigint", named=" => NULL::bigint", all_named=False,
        )
        assert rendered == "SELECT control.f_thing(NULL::bigint);"

    def test_a_routine_with_no_arguments_needs_neither(self) -> None:
        # all_named is null for a routine carrying no arguments, which is not
        # an unnamed one: both forms are the same empty list.
        assert _render(all_named=None) == "SELECT control.f_thing();"

    def test_the_procedure_shape_this_estate_actually_has(self) -> None:
        # control.p_data_profile_ins: named inputs, then INOUT outputs that
        # carry DEFAULT NULL. One INOUT makes the whole routine mode-carrying,
        # which is the shape that rendered nothing and showed the refusal.
        rendered = _render(
            kind="procedure",
            qualified="control.p_data_profile_ins",
            **_args(
                "NULL::character varying, NULL::jsonb, NULL::bigint",
                "p_data_profile_key => NULL::character varying, "
                "p_clear_profile => NULL::jsonb, "
                "o_data_profile_id => NULL::bigint",
            ),
        )

        assert rendered == (
            "CALL control.p_data_profile_ins("
            "p_data_profile_key => NULL::character varying, "
            "p_clear_profile => NULL::jsonb, "
            "o_data_profile_id => NULL::bigint);"
        )


class TestArguments:
    """Every argument is carried, and typed."""

    def test_zero_arguments_render_empty_parentheses(self) -> None:
        assert _render().endswith("f_thing();")

    def test_a_type_containing_a_comma_stays_one_argument(self) -> None:
        # numeric(10,2) is the case that defeats splitting a signature string
        # on commas. Nothing splits anything here: the types arrived separate.
        rendered = _render(**_args(
            "NULL::numeric(10,2), NULL::text[]",
            "p_amount => NULL::numeric(10,2), p_tags => NULL::text[]",
        ))
        assert rendered.count("NULL::") == 2
        assert "NULL::numeric(10,2)" in rendered

    def test_overloads_render_different_statements(self) -> None:
        one = _render(**_args("NULL::bigint", "p_id => NULL::bigint"))
        two = _render(**_args("NULL::text", "p_id => NULL::text"))
        assert one != two

    def test_every_argument_is_cast(self) -> None:
        # The cast is what names one overload rather than whichever shares the
        # name, and it is on both forms for that reason.
        for rendered in (
            _render(**_args("NULL::bigint", "p_id => NULL::bigint")),
            _render(positional="NULL::bigint", named="", all_named=False),
        ):
            assert "NULL::bigint" in rendered

    def test_an_overload_never_renders_as_a_zero_argument_call(self) -> None:
        # The defect that sank the earlier attempt: a call carrying its
        # arguments only as a comment is a zero-argument call, and resolves to
        # whichever namesake takes no arguments.
        rendered = _render(**_args("NULL::bigint", "p_id => NULL::bigint"))
        assert "()" not in rendered


class TestQuoting:
    """PostgreSQL quotes its own names."""

    def test_the_rendered_name_is_used_exactly_as_given(self) -> None:
        # format('%I.%I') already decided when quoting was needed; nothing
        # here re-decides it, so a quoted name survives unchanged.
        rendered = _render(qualified='"Control"."F Thing"')
        assert '"Control"."F Thing"' in rendered

    def test_nothing_requotes_a_plain_name(self) -> None:
        assert '"' not in _render(qualified="control.f_thing")


class TestUnrendered:
    """Absent is an answer, and it is the safe one."""

    def test_a_variadic_argument_renders_nothing(self) -> None:
        # Named notation does not accept one, and its keyword form is a second
        # convention rather than a variation of this one.
        rendered = _render(
            positional="NULL::text[]", named="p_tags => NULL::text[]",
            all_named=True, has_variadic=True,
        )
        assert rendered == ""

    def test_nothing_is_rendered_for_any_shape(self) -> None:
        for kind in ("function", "procedure"):
            for returns_set in (False, True):
                assert _render(
                    kind=kind, returns_set=returns_set, has_variadic=True,
                ) == ""


class TestListing:
    """``list_routines`` carries what it rendered, and omits what it did not."""

    class _Cursor:
        def __init__(self, rows: list[tuple[Any, ...]]) -> None:
            self._rows = rows
            self.closed = False

        def execute(self, sql: str, params: list[Any]) -> None:
            self.sql = sql

        def fetchall(self) -> list[tuple[Any, ...]]:
            return self._rows

        def close(self) -> None:
            self.closed = True

    class _Conn:
        def __init__(self, rows: list[tuple[Any, ...]]) -> None:
            self.cursor_object = TestListing._Cursor(rows)

        def cursor(self) -> Any:
            return self.cursor_object

    def _listed(self, rows: list[tuple[Any, ...]]) -> list[dict[str, str]]:
        return postgres_utils.list_routines(
            self._Conn(rows), "catalog", "control", "function",
        )

    #: One catalog row, in the order the query selects.
    _ROW = (
        "control", "f_thing", "p_id bigint", "control.f_thing", False,
        "NULL::bigint", "p_id => NULL::bigint", True, False,
    )

    def test_a_rendered_routine_carries_its_invocation(self) -> None:
        listed = self._listed([self._ROW])
        assert listed[0]["invocation"] == "SELECT control.f_thing(p_id => NULL::bigint);"

    def test_identity_is_unchanged_by_the_new_key(self) -> None:
        listed = self._listed([self._ROW])
        assert listed[0]["schema"] == "control"
        assert listed[0]["name"] == "f_thing"
        assert listed[0]["signature"] == "p_id bigint"

    def test_an_unrendered_routine_carries_no_key_at_all(self) -> None:
        # Not an empty string to be truthiness-tested downstream: absent.
        listed = self._listed([
            ("control", "f_var", "VARIADIC t text[]", "control.f_var", False,
             "NULL::text[]", "p_tags => NULL::text[]", True, True),
        ])
        assert "invocation" not in listed[0]

    def test_the_cursor_is_closed(self) -> None:
        conn = self._Conn([])
        postgres_utils.list_routines(conn, "catalog", None, "function")
        assert conn.cursor_object.closed


class TestObjects:
    """Both routine objects carry it, and default to carrying none."""

    def _identity(self) -> DatabaseRoutineIdentity:
        return DatabaseRoutineIdentity(
            connection="local", catalog="rey", schema="control", name="f_thing",
            signature="",
        )

    @pytest.mark.parametrize("build", [DatabaseFunction, DatabaseProcedure])
    def test_invocation_defaults_to_empty(self, build: Any) -> None:
        assert build(identity=self._identity()).invocation == ""

    @pytest.mark.parametrize("build", [DatabaseFunction, DatabaseProcedure])
    def test_invocation_survives_to_dict(self, build: Any) -> None:
        routine = build(identity=self._identity(), invocation="SELECT 1;")
        assert routine.to_dict()["invocation"] == "SELECT 1;"

    @pytest.mark.parametrize("build", [DatabaseFunction, DatabaseProcedure])
    def test_the_object_does_not_compose_one(self, build: Any) -> None:
        # The object holds the provider's answer. With none, it has none --
        # it does not derive a call from the identity it happens to carry.
        assert build(identity=self._identity()).to_dict()["invocation"] == ""
