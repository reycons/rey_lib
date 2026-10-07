"""DatabaseSchema.list: schema enumeration is the schema's (backlog 720).

One unread ``DatabaseSchema`` per schema a connection holds, carrying its
connection, catalog and name and nothing it contains. A provider that cannot
enumerate schemas is refused by name -- never answered as a database with none.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import duckdb
import pytest

from rey_lib.db.database_objects import DatabaseSchema
from rey_lib.db.db_adapter import DBAdapter
from rey_lib.errors.error_utils import UnsupportedDatabaseCapabilityError


class _Adapter:
    """Answers the schema listing it is given."""

    def __init__(self, listed: list[dict[str, Any]]) -> None:
        self.listed = listed

    def list_schemas(self, _conn: Any) -> list[dict[str, Any]]:
        return list(self.listed)


def test_each_listed_schema_is_an_unread_schema_object() -> None:
    adapter = _Adapter([
        {"catalog": "rey", "name": "control"}, {"catalog": "rey", "name": "tree"},
    ])

    listed = DatabaseSchema.list(object(), "rey_apps", adapter)

    assert listed == (
        DatabaseSchema(connection="rey_apps", catalog="rey", schema="control"),
        DatabaseSchema(connection="rey_apps", catalog="rey", schema="tree"),
    )
    # Unread: what a schema holds is DatabaseSchema.read's answer.
    assert all(not (s.tables or s.views or s.procedures or s.functions) for s in listed)


def test_an_unnamed_entry_is_not_a_schema() -> None:
    adapter = _Adapter([{"catalog": "rey", "name": ""}, {"catalog": "rey", "name": "control"}])

    assert [s.schema for s in DatabaseSchema.list(object(), "c", adapter)] == ["control"]


def test_a_provider_that_cannot_list_schemas_is_refused_by_name() -> None:
    """SQL Server declares no schema listing: refused, not answered with none."""
    with pytest.raises(UnsupportedDatabaseCapabilityError, match="provider 'sqlserver'"):
        DatabaseSchema.list(SimpleNamespace(provider="sqlserver"), "c", DBAdapter())


def test_duckdb_lists_its_schemas_end_to_end() -> None:
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA staging")
    try:
        names = [s.schema for s in DatabaseSchema.list(conn, "c", DBAdapter())]
    finally:
        conn.close()

    assert "staging" in names and "main" in names
