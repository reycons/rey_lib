"""One column's definition, as PostgreSQL writes it.

A fragment rather than a statement: a column is created with its table. These
prove the shape against a scripted cursor -- no database is opened, and the
catalog rows below are written by the test.
"""

from __future__ import annotations

from typing import Any

from rey_lib.db import postgres_utils


class _Cursor:
    """A cursor answering with one pg_attribute row."""

    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row
        self.statements: list[str] = []
        self.parameters: list[list[Any]] = []

    def execute(self, statement: str, params: list[Any] | None = None) -> None:
        self.statements.append(statement)
        self.parameters.append(list(params or []))

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row

    def close(self) -> None:
        return None


class _Conn:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._cursor = _Cursor(row)

    def cursor(self) -> _Cursor:
        return self._cursor


def _ddl(row: tuple[Any, ...] | None) -> str:
    return postgres_utils.get_column_ddl(_Conn(row), "control", "run_log", "run_id")


def test_it_carries_the_type_the_provider_resolved() -> None:
    """format_type's answer, not a name the caller guessed."""
    assert _ddl(("run_id", "bigint", True, None)) == '"run_id" bigint NOT NULL'


def test_it_preserves_not_null() -> None:
    assert _ddl(("run_id", "bigint", True, None)).endswith("NOT NULL")
    assert _ddl(("run_id", "bigint", False, None)).endswith("NULL")
    assert "NOT NULL" not in _ddl(("run_id", "bigint", False, None))


def test_it_preserves_the_default_expression() -> None:
    """The expression as the catalog rendered it, between type and nullability."""
    assert _ddl(("status", "text", False, "'pending'::text")) == (
        '"status" text DEFAULT \'pending\'::text NULL'
    )


def test_it_is_a_fragment_and_not_a_statement() -> None:
    """A column has no standalone CREATE, and this does not pretend it does."""
    written = _ddl(("run_id", "bigint", True, None))

    assert not written.upper().startswith("CREATE")
    assert not written.upper().startswith("ALTER")
    assert not written.endswith(";")


def test_a_column_that_is_not_there_answers_with_nothing() -> None:
    """A dropped column is an empty definition, not a failure."""
    assert _ddl(None) == ""


def test_it_asks_about_the_column_it_was_given() -> None:
    """Schema, table and column all reach the catalog read."""
    conn = _Conn(("run_id", "bigint", True, None))
    postgres_utils.get_column_ddl(conn, "control", "run_log", "run_id")

    assert conn.cursor().parameters[0] == ["control", "run_log", "run_id"]
    assert "pg_attribute" in conn.cursor().statements[0]
    assert "format_type" in conn.cursor().statements[0]
