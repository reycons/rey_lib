"""Focused tests for DBAdapter's bounded read-query result conversion."""

from __future__ import annotations

from rey_lib.db.db_adapter import DBAdapter


def test_query_rows_returns_named_rows_and_closes_its_cursor() -> None:
    class Cursor:
        description = [("id",), ("name",)]

        def __init__(self) -> None:
            self.executed = ""
            self.fetch_limit = 0
            self.closed = False

        def execute(self, sql_text: str) -> None:
            self.executed = sql_text

        def fetchmany(self, limit: int) -> list[tuple[int, str]]:
            self.fetch_limit = limit
            return [(1, "Alpha")]

        def close(self) -> None:
            self.closed = True

    class Connection:
        def __init__(self) -> None:
            self.cursor_instance = Cursor()

        def cursor(self) -> Cursor:
            return self.cursor_instance

    connection = Connection()
    columns, rows = DBAdapter().query_rows(
        connection, "SELECT id, name FROM records", limit=25
    )

    assert columns == ["id", "name"]
    assert rows == [{"id": 1, "name": "Alpha"}]
    assert connection.cursor_instance.executed == "SELECT id, name FROM records"
    assert connection.cursor_instance.fetch_limit == 25
    assert connection.cursor_instance.closed is True

def test_an_unbounded_read_fetches_everything_and_asks_for_no_limit() -> None:
    """``limit=None`` is a different act from a large number.

    A reader previewing a result names a bound; a load reading its source has
    none to name, and naming a big one instead truncates silently the day the
    result outgrows it. So the unbounded read must reach ``fetchall`` and must
    not pass a number to ``fetchmany`` at all.
    """
    class Cursor:
        description = [("id",)]

        def __init__(self) -> None:
            self.fetch_limits: list[int] = []
            self.fetched_all = False

        def execute(self, sql_text: str) -> None:
            self.executed = sql_text

        def fetchmany(self, limit: int) -> list[tuple[int]]:
            self.fetch_limits.append(limit)
            return [(1,)]

        def fetchall(self) -> list[tuple[int]]:
            self.fetched_all = True
            return [(1,), (2,), (3,)]

        def close(self) -> None:
            pass

    class Connection:
        def __init__(self) -> None:
            self.cursor_instance = Cursor()

        def cursor(self) -> Cursor:
            return self.cursor_instance

    connection = Connection()
    columns, rows = DBAdapter().query_rows(
        connection, "SELECT id FROM records", limit=None
    )

    assert columns == ["id"]
    assert rows == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert connection.cursor_instance.fetched_all is True
    assert connection.cursor_instance.fetch_limits == []


def test_a_statement_with_no_columns_is_not_read_at_all() -> None:
    """A statement exposing no result set has nothing to fetch.

    Asserted for the unbounded case specifically: the bounded path already
    guarded this with its ``if columns`` expression, and an unbounded
    ``fetchall`` on a cursor with no description is what that guard was
    protecting against.
    """
    class Cursor:
        description = None

        def execute(self, sql_text: str) -> None:
            pass

        def fetchall(self) -> list[tuple[int]]:
            raise AssertionError("nothing to fetch from a statement with no result")

        def close(self) -> None:
            pass

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

    columns, rows = DBAdapter().query_rows(
        Connection(), "CREATE TABLE t (a INT)", limit=None
    )

    assert columns == []
    assert rows == []
