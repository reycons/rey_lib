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
    arguments: str | None = None,
    plain: bool = True,
) -> str:
    """Render one invocation the way ``list_routines`` renders it."""
    return postgres_utils._routine_invocation(
        kind, qualified, returns_set, arguments, plain,
    )


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


class TestArguments:
    """Every argument occupies its position, typed."""

    def test_zero_arguments_render_empty_parentheses(self) -> None:
        assert _render(arguments=None).endswith("f_thing();")

    def test_each_argument_is_placed_and_typed(self) -> None:
        rendered = _render(arguments="NULL::bigint, NULL::text")
        assert rendered == "SELECT control.f_thing(NULL::bigint, NULL::text);"

    def test_a_type_containing_a_comma_stays_one_argument(self) -> None:
        # numeric(10,2) is the case that defeats splitting a signature string
        # on commas. Nothing splits anything here: the types arrived separate.
        rendered = _render(arguments="NULL::numeric(10,2), NULL::text[]")
        assert rendered.count("NULL::") == 2
        assert "NULL::numeric(10,2)" in rendered

    def test_overloads_render_different_statements(self) -> None:
        one = _render(arguments="NULL::bigint")
        two = _render(arguments="NULL::text")
        assert one != two

    def test_an_overload_never_renders_as_a_zero_argument_call(self) -> None:
        # The defect that sank the earlier attempt: a call carrying its
        # arguments only as a comment is a zero-argument call, and resolves to
        # whichever namesake takes no arguments.
        rendered = _render(arguments="NULL::bigint")
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

    def test_non_plain_arguments_render_nothing(self) -> None:
        # OUT, INOUT and VARIADIC are placed by rules this does not implement.
        assert _render(arguments="NULL::text[]", plain=False) == ""

    def test_nothing_is_rendered_for_any_shape(self) -> None:
        for kind in ("function", "procedure"):
            for returns_set in (False, True):
                assert _render(kind=kind, returns_set=returns_set, plain=False) == ""


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

    def test_a_rendered_routine_carries_its_invocation(self) -> None:
        listed = self._listed([
            ("control", "f_thing", "p_id bigint", "control.f_thing",
             False, "NULL::bigint", True),
        ])
        assert listed[0]["invocation"] == "SELECT control.f_thing(NULL::bigint);"

    def test_identity_is_unchanged_by_the_new_key(self) -> None:
        listed = self._listed([
            ("control", "f_thing", "p_id bigint", "control.f_thing",
             False, "NULL::bigint", True),
        ])
        assert listed[0]["schema"] == "control"
        assert listed[0]["name"] == "f_thing"
        assert listed[0]["signature"] == "p_id bigint"

    def test_an_unrendered_routine_carries_no_key_at_all(self) -> None:
        # Not an empty string to be truthiness-tested downstream: absent.
        listed = self._listed([
            ("control", "f_var", "VARIADIC t text[]", "control.f_var",
             False, "NULL::text[]", False),
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
