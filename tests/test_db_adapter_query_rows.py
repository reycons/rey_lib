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