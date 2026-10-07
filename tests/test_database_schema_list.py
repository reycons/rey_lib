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

from rey_lib.db import db_adapter, sqlserver_utils
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


def test_a_provider_that_cannot_list_schemas_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused, not answered with none."""
    monkeypatch.setitem(db_adapter._PROVIDER_METADATA_CAPABILITIES, "sqlserver", frozenset())

    with pytest.raises(UnsupportedDatabaseCapabilityError, match="provider 'sqlserver'"):
        DatabaseSchema.list(SimpleNamespace(provider="sqlserver"), "c", DBAdapter())


def test_sql_server_lists_its_schemas_from_its_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 720 refusal is gone: SQL Server answers from its object inventory (723)."""
    monkeypatch.setattr(sqlserver_utils, "get_current_database", lambda _conn: "NaviStage")
    monkeypatch.setattr(sqlserver_utils, "list_database_objects", lambda _conn, database=None: [
        {"object_type": "schema", "schema": "staging", "name": "staging"},
        {"object_type": "schema", "schema": "dbo", "name": "dbo"},
        {"object_type": "table", "schema": "dbo", "name": "trade"},
    ])

    listed = DatabaseSchema.list(SimpleNamespace(provider="sqlserver"), "c", DBAdapter())

    assert [(s.catalog, s.schema) for s in listed] == [
        ("NaviStage", "dbo"), ("NaviStage", "staging"),
    ]


def test_duckdb_lists_its_schemas_end_to_end() -> None:
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA staging")
    try:
        names = [s.schema for s in DatabaseSchema.list(conn, "c", DBAdapter())]
    finally:
        conn.close()

    assert "staging" in names and "main" in names
