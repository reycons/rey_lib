"""Paging a file, without counting it.

Counting rows in a table is cheap; counting rows in a file is a full parse of
that file. ``execute_page`` used to run a ``count(*)`` beside every page, so
opening a large JSONL parsed it twice per page and the Console stopped
responding.

What these state is the contract that replaced it: a file is paged, the total is
**unknown rather than counted**, and ``next_offset`` is what says another page
follows. The important assertion is the negative one -- that nothing on the path
counts -- because a total that quietly came back would restore the hang while
every other test here still passed.

``open_memory_connection`` disables extension autoload and autoinstall, so these
require a DuckDB build with JSON support linked in. If they fail on the reader
rather than on an assertion, that is what to check first: it is a fact about the
build, not about the contract under test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from rey_lib.db.duckdb_utils import execute_page, open_memory_connection
from rey_lib.errors.error_utils import (
    DatabaseError,
    UnsupportedDatabaseCapabilityError,
)

#: Small enough for a suite. The stress case is a file of millions of lines and
#: is not a unit test; what is proved here is the shape, not the speed.
RECORDS = 250


@pytest.fixture()
def jsonl_file(tmp_path: Path) -> Path:
    """One newline-delimited JSON file of ordered, identifiable records."""
    path = tmp_path / "records.jsonl"
    with path.open("w", encoding="utf-8") as out:
        for identity in range(RECORDS):
            out.write(json.dumps({
                "id": identity,
                "ref": f"ref-{identity:05d}",
                "amount": identity * 1.5,
            }) + "\n")
    return path


def _select(path: Path) -> str:
    """The statement the workbench opens a .jsonl on."""
    return (
        f"SELECT * FROM read_json_auto('{path}', format = 'newline_delimited')"
    )


class Recorder:
    """A connection that remembers every statement executed through it.

    Wrapping the handle rather than the module: what matters is what actually
    reached DuckDB, and a test that inspected the source instead would pass on a
    count issued from somewhere else.
    """

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        statements: list[str] | None = None,
    ) -> None:
        self._conn = conn
        # Shared with every cursor taken from this recorder, so a statement
        # issued on execute_page's own handle is recorded here too.
        self.statements: list[str] = [] if statements is None else statements

    def cursor(self) -> "Recorder":
        """Return a recording handle on the same database."""
        return Recorder(self._conn.cursor(), self.statements)

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        self.statements.append(sql)
        return self._conn.execute(sql, *args, **kwargs)

    def close(self) -> None:
        self._conn.close()


def test_a_page_of_a_file_carries_no_total(jsonl_file: Path) -> None:
    """Unknown, and said so -- not zero, and not counted."""
    with open_memory_connection() as conn:
        page = execute_page(conn, _select(jsonl_file), offset=0, limit=50)

    assert page.total_row_count is None
    assert len(page.rows) == 50
    assert page.next_offset == 50


def test_nothing_on_the_path_counts_the_file(jsonl_file: Path) -> None:
    """The negative assertion, and the point of the whole change.

    A count would not fail any other test here. It would simply parse the file
    again on every page, which is what made opening a large one hang.
    """
    with open_memory_connection() as conn:
        recorder = Recorder(conn)
        execute_page(recorder, _select(jsonl_file), offset=0, limit=50)

    counted = [sql for sql in recorder.statements if "count(" in sql.lower()]
    assert counted == [], f"a statement counted the file: {counted}"


def test_the_last_page_says_there_is_no_next(jsonl_file: Path) -> None:
    """next_offset is the continuation signal, so it must stop at the end."""
    with open_memory_connection() as conn:
        page = execute_page(
            conn, _select(jsonl_file), offset=RECORDS - 10, limit=50,
        )

    assert len(page.rows) == 10
    assert page.next_offset is None


def test_paging_to_the_end_yields_every_row_once(jsonl_file: Path) -> None:
    """Followed by next_offset alone, with no total to do arithmetic against."""
    seen: list[int] = []
    with open_memory_connection() as conn:
        offset: int | None = 0
        while offset is not None:
            page = execute_page(
                conn,
                f"{_select(jsonl_file)} ORDER BY id",
                offset=offset,
                limit=40,
            )
            seen.extend(int(row["id"]) for row in page.rows)
            offset = page.next_offset

    assert seen == list(range(RECORDS))


def test_a_statement_that_cannot_be_paged_is_declined(tmp_path: Path) -> None:
    """Declined before execution, which is what makes the eager retry safe."""
    with open_memory_connection() as conn:
        with pytest.raises(UnsupportedDatabaseCapabilityError):
            execute_page(
                conn, "CREATE TABLE zzz_nope (a INTEGER)", offset=0, limit=10,
            )

        # And it did not run: a declination answered by executing the statement
        # eagerly would otherwise create this table twice.
        existing = conn.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE table_name = 'zzz_nope'"
        ).fetchone()
        assert existing is not None and existing[0] == 0


@pytest.mark.parametrize(
    "offset,limit",
    [(-1, 10), (0, 0), (0, 10_000), (0, -5)],
)
def test_bounds_are_refused_rather_than_clamped(
    jsonl_file: Path, offset: int, limit: int,
) -> None:
    """A caller quietly given a different page than it asked for is misled."""
    with open_memory_connection() as conn:
        with pytest.raises(DatabaseError):
            execute_page(conn, _select(jsonl_file), offset=offset, limit=limit)
