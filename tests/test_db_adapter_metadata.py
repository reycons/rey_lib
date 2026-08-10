"""Provider-neutral DBAdapter metadata and capability contract tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import duckdb
import pytest
from sqlalchemy import create_engine

from rey_lib.db import mysql_utils, postgres_utils
from rey_lib.db._sqlalchemy import ReyConnection
from rey_lib.db.db_adapter import DBAdapter
from rey_lib.errors.error_utils import (
    ConfigError,
    UnsupportedDatabaseCapabilityError,
)


_ALL_CAPABILITIES = {
    "catalogs",
    "schemas",
    "tables",
    "views",
    "columns",
    "primary_keys",
    "foreign_keys",
    "indexes",
    "unique_constraints",
}


def _sqlalchemy_connection(provider: str) -> ReyConnection:
    engine = create_engine("sqlite://")
    core = engine.connect()
    core.exec_driver_sql(
        "CREATE TABLE parent ("
        "id INTEGER, code TEXT, "
        "CONSTRAINT pk_parent PRIMARY KEY(id), "
        "CONSTRAINT uq_parent_code UNIQUE(code))"
    )
    core.exec_driver_sql(
        "CREATE TABLE child ("
        "id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL DEFAULT 7, "
        "CONSTRAINT fk_child_parent FOREIGN KEY(parent_id) REFERENCES parent(id))"
    )
    core.exec_driver_sql("CREATE TABLE no_primary_key (value TEXT)")
    core.exec_driver_sql("CREATE INDEX ix_child_parent ON child(parent_id)")
    core.exec_driver_sql("CREATE VIEW child_ids AS SELECT id FROM child")
    return ReyConnection(provider, engine, core)


def _assert_rey_primitives(value: Any) -> None:
    if isinstance(value, dict):
        assert all(isinstance(key, str) for key in value)
        for nested in value.values():
            _assert_rey_primitives(nested)
        return
    if isinstance(value, list):
        for nested in value:
            _assert_rey_primitives(nested)
        return
    assert value is None or isinstance(value, (str, int, float, bool))


@pytest.mark.parametrize(
    ("provider", "backend"),
    [("postgres", postgres_utils), ("mysql", mysql_utils)],
)
def test_sqlalchemy_providers_return_the_same_normalized_contract(
    provider: str,
    backend: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _sqlalchemy_connection(provider)
    monkeypatch.setattr(backend, "get_current_database", lambda _conn: "subject")
    adapter = DBAdapter()
    try:
        assert all(adapter.supports(conn, capability) for capability in _ALL_CAPABILITIES)
        assert adapter.list_catalogs(conn) == [{"name": "subject"}]
        assert {item["name"] for item in adapter.list_schemas(conn)} >= {"main"}
        assert {item["name"] for item in adapter.list_tables(conn, "main")} == {
            "child",
            "no_primary_key",
            "parent",
        }
        assert adapter.list_views(conn, "main") == [
            {"catalog": "subject", "schema": "main", "name": "child_ids"}
        ]

        columns = adapter.get_columns(conn, "main", "child")
        assert columns == [
            {
                "catalog": "subject",
                "schema": "main",
                "table": "child",
                "name": "id",
                "ordinal_position": 1,
                "type": "INTEGER",
                "nullable": True,
                "default": None,
            },
            {
                "catalog": "subject",
                "schema": "main",
                "table": "child",
                "name": "parent_id",
                "ordinal_position": 2,
                "type": "INTEGER",
                "nullable": False,
                "default": "7",
            },
        ]
        assert adapter.get_primary_key(conn, "main", "parent") == {
            "catalog": "subject",
            "schema": "main",
            "table": "parent",
            "name": "pk_parent",
            "columns": ["id"],
        }
        assert adapter.get_primary_key(conn, "main", "no_primary_key") == {
            "catalog": "subject",
            "schema": "main",
            "table": "no_primary_key",
            "name": None,
            "columns": [],
        }
        foreign_keys = adapter.get_foreign_keys(conn, "main", "child")
        assert foreign_keys[0]["name"] == "fk_child_parent"
        assert foreign_keys[0]["columns"] == ["parent_id"]
        assert foreign_keys[0]["referenced_table"] == "parent"
        assert foreign_keys[0]["referenced_columns"] == ["id"]
        assert adapter.get_indexes(conn, "main", "child") == [
            {
                "catalog": "subject",
                "schema": "main",
                "table": "child",
                "name": "ix_child_parent",
                "columns": ["parent_id"],
                "unique": False,
            }
        ]
        assert adapter.get_unique_constraints(conn, "main", "parent") == [
            {
                "catalog": "subject",
                "schema": "main",
                "table": "parent",
                "name": "uq_parent_code",
                "columns": ["code"],
            }
        ]

        for value in (
            columns,
            adapter.get_primary_key(conn, "main", "parent"),
            foreign_keys,
            adapter.get_indexes(conn, "main", "child"),
            adapter.get_unique_constraints(conn, "main", "parent"),
        ):
            _assert_rey_primitives(value)
    finally:
        conn.close()


def test_supported_empty_is_not_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite://")
    conn = ReyConnection("postgres", engine, engine.connect())
    monkeypatch.setattr(postgres_utils, "get_current_database", lambda _conn: "empty")
    try:
        adapter = DBAdapter()
        assert adapter.supports(conn, "tables") is True
        assert adapter.list_tables(conn, "main") == []
        assert adapter.list_views(conn, "main") == []
    finally:
        conn.close()


def test_unknown_capability_fails_as_configuration_error() -> None:
    with pytest.raises(ConfigError, match="unknown metadata capability"):
        DBAdapter().supports(SimpleNamespace(provider="postgres"), "tablez")


def test_duckdb_reports_only_existing_metadata_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = duckdb.connect(":memory:")
    from rey_lib.db import duckdb_utils

    monkeypatch.setattr(
        duckdb_utils,
        "list_database_objects",
        lambda _conn, catalog: [
            {
                "database": catalog,
                "object_type": "schema",
                "schema": "subject",
                "name": "subject",
                "dependencies": [],
            },
            {
                "database": catalog,
                "object_type": "table",
                "schema": "subject",
                "name": "records",
                "dependencies": [],
            },
            {
                "database": catalog,
                "object_type": "view",
                "schema": "subject",
                "name": "current_records",
                "dependencies": [],
            },
        ],
    )
    adapter = DBAdapter()
    try:
        for capability in ("catalogs", "schemas", "tables", "views"):
            assert adapter.supports(conn, capability) is True
        for capability in (
            "columns",
            "primary_keys",
            "foreign_keys",
            "indexes",
            "unique_constraints",
        ):
            assert adapter.supports(conn, capability) is False

        catalog = adapter.list_catalogs(conn)[0]["name"]
        assert {item["name"] for item in adapter.list_schemas(conn)} >= {"subject"}
        assert adapter.list_tables(conn, "subject") == [
            {"catalog": catalog, "schema": "subject", "name": "records"}
        ]
        assert adapter.list_views(conn, "subject") == [
            {"catalog": catalog, "schema": "subject", "name": "current_records"}
        ]
        with pytest.raises(
            UnsupportedDatabaseCapabilityError,
            match="provider 'duckdb'.*capability 'columns'",
        ):
            adapter.get_columns(conn, "subject", "records")
    finally:
        conn.close()


def test_sqlserver_stays_outside_the_metadata_increment() -> None:
    conn = SimpleNamespace(provider="sqlserver")
    adapter = DBAdapter()
    assert adapter.supports(conn, "schemas") is False
    with pytest.raises(UnsupportedDatabaseCapabilityError, match="provider 'sqlserver'"):
        adapter.list_schemas(conn)
