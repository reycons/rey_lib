"""DatabaseSequence owns its DDL (backlog 695; 697 folded in).

A sequence's DDL used to be a branch in each provider's ``get_object_ddl``. It
is ``DatabaseSequence.read_ddl`` now, addressed by schema and plain name --
PostgreSQL's listing used to fold the schema into the name. DuckDB supplies its
own definition from ``duckdb_sequences()``; its old reads asked an
``information_schema.sequences`` DuckDB does not have, which broke its whole
export listing (697). PostgreSQL and SQL Server state the parts and the
sequence composes CREATE SEQUENCE.
"""

from __future__ import annotations

import types
from typing import Any

import duckdb
import pytest

from rey_lib.db import duckdb_utils, postgres_utils, sqlserver_utils
from rey_lib.db.database_objects import DatabaseObjectIdentity, DatabaseSequence
from rey_lib.db.db_adapter import DBAdapter
from rey_lib.errors.error_utils import DatabaseError

_IDENTITY = DatabaseObjectIdentity(connection="c", catalog="rey", schema="control", name="run_seq")

_CATALOG = {"start": 1, "increment": 1, "minimum": 1, "maximum": 9999, "cycle": False}

_QUOTE = {
    "postgres": postgres_utils.quote_identifier,
    "sqlserver": sqlserver_utils.quote_identifier,
}


class _Connector:
    """The sequence primitives, as one provider, recording what was asked."""

    def __init__(self, provider: str, catalog: dict) -> None:
        self._provider = provider
        self.catalog = catalog
        self.asked: list[tuple] = []

    def provider(self, _conn: Any) -> str:
        return self._provider

    def quote_identifier(self, _conn: Any, name: str) -> str:
        return _QUOTE[self._provider](name)

    def sequence_catalog(self, _conn: Any, schema: str, name: str) -> dict:
        self.asked.append((schema, name))
        return self.catalog


def test_postgres_composes_create_sequence_if_not_exists() -> None:
    connector = _Connector("postgres", _CATALOG)

    assert DatabaseSequence.read_ddl(object(), _IDENTITY, connector) == (
        'CREATE SEQUENCE IF NOT EXISTS "control"."run_seq" START WITH 1 INCREMENT BY 1 '
        "MINVALUE 1 MAXVALUE 9999 NO CYCLE;"
    )
    assert connector.asked == [("control", "run_seq")]


def test_sql_server_composes_and_ends_the_batch() -> None:
    connector = _Connector("sqlserver", {**_CATALOG, "cycle": True})

    assert DatabaseSequence.read_ddl(object(), _IDENTITY, connector) == (
        "CREATE SEQUENCE [control].[run_seq] START WITH 1 INCREMENT BY 1 "
        "MINVALUE 1 MAXVALUE 9999 CYCLE;\nGO"
    )


def test_a_sequence_that_is_not_there_is_refused_by_name() -> None:
    with pytest.raises(DatabaseError, match="control.run_seq"):
        DatabaseSequence.read_ddl(object(), _IDENTITY, _Connector("postgres", {}))


def test_postgres_reads_every_sequence_the_listing_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """From pg_sequence, as the listing reads pg_class (backlog 911).

    information_schema.sequences shows only what the reading role owns or
    holds privileges on: the listing named sequences the reader then could
    not find.
    """
    asked: list[tuple] = []
    monkeypatch.setattr(postgres_utils, "fetch_rows", lambda _c, sql, params: asked.append(
        (sql, params)) or [{
            "start_value": 1, "increment": 2, "minimum_value": 1,
            "maximum_value": 10, "cycle_option": True,
        }])

    catalog = postgres_utils.sequence_catalog(object(), "control", "run_seq")

    assert catalog == {"start": 1, "increment": 2, "minimum": 1, "maximum": 10, "cycle": True}
    ((sql, params),) = asked
    assert "FROM pg_sequence s" in sql and "information_schema" not in sql
    assert params == {"schema": "control", "name": "run_seq"}


class _Cursor:
    """A psycopg cursor whose listing query is recorded."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql_text: str, *_a: Any) -> None:
        self.statements.append(sql_text)

    def fetchall(self) -> list[tuple]:
        if "WITH base AS" in self.statements[-1]:
            return [("sequences", "control", "run_seq", None, None, None)]
        return []

    def fetchone(self) -> tuple:
        return ("rey",)

    def close(self) -> None:
        pass


def test_the_postgres_listing_names_a_sequence_by_its_plain_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _Cursor()
    monkeypatch.setattr(postgres_utils, "_postgres_dependencies", lambda *_a, **_k: [])

    (listed,) = postgres_utils.list_database_objects(
        types.SimpleNamespace(cursor=lambda: cursor), database="rey",
    )

    assert (listed["schema"], listed["name"]) == ("control", "run_seq")
    (asked,) = [sql for sql in cursor.statements if "WITH base AS" in sql]
    assert "s.sequence_schema || '.'" not in asked
    # From pg_class: information_schema.sequences shows only what the reading
    # role holds privileges on, so the listing depended on who read it (760).
    assert "c.relkind = 'S'" in asked
    assert "FROM information_schema.sequences" not in asked


def test_the_postgres_listing_leaves_an_identity_column_s_sequence_to_its_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An identity sequence is created by its column's GENERATED ... AS IDENTITY (911).

    It is excluded by its identity dependency (deptype 'i'); a serial's
    sequence ('a') stays a sequence of its own, because its column's default
    names it.
    """
    cursor = _Cursor()
    monkeypatch.setattr(postgres_utils, "_postgres_dependencies", lambda *_a, **_k: [])

    postgres_utils.list_database_objects(
        types.SimpleNamespace(cursor=lambda: cursor), database="rey",
    )

    (asked,) = [sql for sql in cursor.statements if "WITH base AS" in sql]
    sequences = asked[asked.index("'sequences'::text"):asked.index("'views'::text")]
    assert "NOT EXISTS" in sequences and "d.deptype = 'i'" in sequences
    assert "deptype = 'a'" not in sequences


def test_duckdb_answers_from_its_own_catalog_end_to_end() -> None:
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SEQUENCE run_seq START 5 INCREMENT 2")
    identity = DatabaseObjectIdentity(connection="c", catalog="", schema="main", name="run_seq")

    ddl = DatabaseSequence.read_ddl(conn, identity, DBAdapter())

    assert ddl.startswith("CREATE SEQUENCE run_seq ")
    assert "INCREMENT BY 2" in ddl and "START 5" in ddl and ddl.endswith(";")


def test_the_duckdb_listing_names_its_sequences_and_each_writes_itself() -> None:
    """697: the listing asked information_schema.sequences, which DuckDB lacks.

    The listing and the sequence's own DDL, not a whole export: DuckDB's
    schema listing repeats ``main`` once per attached catalog, a separate
    defect that still stops a full DuckDB export.
    """
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SEQUENCE run_seq START 5")

    (listed,) = [
        entry for entry in duckdb_utils.list_database_objects(conn)
        if entry["object_type"] == "sequence"
    ]
    identity = DatabaseObjectIdentity(
        connection="c", catalog="", schema=listed["schema"], name=listed["name"],
    )

    assert (listed["schema"], listed["name"]) == ("main", "run_seq")
    assert DatabaseSequence.read_ddl(conn, identity, DBAdapter()).startswith(
        "CREATE SEQUENCE run_seq ",
    )
