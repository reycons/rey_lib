"""Keys and constraints own their standalone DDL (backlog 695).

A constraint's DDL used to be a branch in each provider's ``get_object_ddl``.
It is the owning object's now: DatabasePrimaryKey, DatabaseForeignKey, and
DatabaseConstraint for unique and check -- each addressed by its table and its
own name, each refusing a kind it does not own. PostgreSQL supplies its own
clause; MySQL and SQL Server state the parts and the object composes it.

A table owns its whole creation statement. Where that statement already
creates the table's indexes and constraints, the export does not emit them a
second time -- DatabaseTable states the fact, the export decides.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from rey_lib.db import database_objects
from rey_lib.db import db_adapter as adapter_module
from rey_lib.db import mysql_utils, postgres_utils, sqlserver_utils
from rey_lib.db.database_objects import (
    DatabaseConstraint,
    DatabaseForeignKey,
    DatabaseObjectIdentity,
    DatabasePrimaryKey,
    DatabaseTable,
)
from rey_lib.db.ddl_export import DatabaseDdlExport
from rey_lib.errors.error_utils import DatabaseError

_QUOTE = {
    "postgres": postgres_utils.quote_identifier,
    "mysql": mysql_utils.quote_identifier,
    "sqlserver": sqlserver_utils.quote_identifier,
}

_TABLE = DatabaseObjectIdentity(connection="c", catalog="rey", schema="control", name="run_log")

_PRIMARY = {"kind": "primary_key", "columns": ["run_id"]}
_FOREIGN = {
    "kind": "foreign_key", "columns": ["batch_id"], "referenced_schema": "control",
    "referenced_table": "batch", "referenced_columns": ["batch_id"],
}
_UNIQUE = {"kind": "unique", "columns": ["status", "run_id"]}
_CHECK = {"kind": "check", "columns": [], "check": "[qty]>(0)"}


class _Connector:
    """The constraint primitives, as one provider, recording what was asked."""

    def __init__(self, provider: str, catalog: dict, native: str = "") -> None:
        self._provider = provider
        self.catalog = catalog
        self.native = native
        self.asked: list[tuple] = []

    def provider(self, _conn: Any) -> str:
        return self._provider

    def quote_identifier(self, _conn: Any, name: str) -> str:
        return _QUOTE[self._provider](name)

    def constraint_catalog(
        self, _conn: Any, schema: str, table: str, name: str, **_k: Any,
    ) -> dict:
        self.asked.append(("catalog", schema, table, name))
        return self.catalog

    def constraint_native_definition(self, _conn: Any, schema: str, table: str, name: str) -> str:
        self.asked.append(("native", schema, table, name))
        return self.native


def test_postgres_writes_the_clause_the_server_supplies() -> None:
    connector = _Connector(
        "postgres", _FOREIGN,
        "FOREIGN KEY (batch_id) REFERENCES control.batch(batch_id) ON DELETE CASCADE",
    )

    ddl = DatabaseForeignKey.read_ddl(object(), _TABLE, "run_log_batch_fkey", connector)

    assert ddl == (
        'ALTER TABLE "control"."run_log" ADD CONSTRAINT "run_log_batch_fkey" '
        "FOREIGN KEY (batch_id) REFERENCES control.batch(batch_id) ON DELETE CASCADE;"
    )
    assert ("native", "control", "run_log", "run_log_batch_fkey") in connector.asked


@pytest.mark.parametrize(("owner", "catalog", "expected"), [
    (DatabasePrimaryKey, _PRIMARY,
     "ALTER TABLE `control`.`run_log` ADD CONSTRAINT `k` PRIMARY KEY (`run_id`);"),
    (DatabaseForeignKey, _FOREIGN,
     "ALTER TABLE `control`.`run_log` ADD CONSTRAINT `k` FOREIGN KEY (`batch_id`) "
     "REFERENCES `control`.`batch` (`batch_id`);"),
    (DatabaseConstraint, _UNIQUE,
     "ALTER TABLE `control`.`run_log` ADD CONSTRAINT `k` UNIQUE (`status`, `run_id`);"),
])
def test_mysql_composes_each_kind_from_its_catalog(
    owner: Any, catalog: dict, expected: str,
) -> None:
    assert owner.read_ddl(object(), _TABLE, "k", _Connector("mysql", catalog)) == expected


def test_sql_server_writes_a_check_and_ends_the_batch() -> None:
    ddl = DatabaseConstraint.read_ddl(object(), _TABLE, "ck", _Connector("sqlserver", _CHECK))

    assert ddl == "ALTER TABLE [control].[run_log] ADD CONSTRAINT [ck] CHECK ([qty]>(0));\nGO"


@pytest.mark.parametrize(("owner", "catalog"), [
    (DatabasePrimaryKey, _FOREIGN),
    (DatabaseForeignKey, _UNIQUE),
    (DatabaseConstraint, _PRIMARY),
    (DatabaseConstraint, _FOREIGN),
])
def test_a_kind_the_object_does_not_own_is_refused(owner: Any, catalog: dict) -> None:
    with pytest.raises(DatabaseError, match=owner.__name__):
        owner.read_ddl(object(), _TABLE, "k", _Connector("mysql", catalog))


def test_a_constraint_that_is_not_there_is_refused_by_name() -> None:
    with pytest.raises(DatabaseError, match="control.run_log.gone"):
        DatabaseConstraint.read_ddl(object(), _TABLE, "gone", _Connector("mysql", {}))


@pytest.mark.parametrize(("provider", "constraints", "indexes"), [
    ("postgres", False, False),
    ("mysql", True, True),
    ("sqlserver", False, False),
    ("duckdb", True, False),
])
def test_the_table_states_what_its_creation_creates(
    provider: str, constraints: bool, indexes: bool,
) -> None:
    connector = types.SimpleNamespace(provider=lambda _conn: provider)

    assert DatabaseTable.create_includes_constraints(object(), connector) is constraints
    assert DatabaseTable.create_includes_indexes(object(), connector) is indexes


@pytest.mark.parametrize("fact", ["create_includes_constraints", "create_includes_indexes"])
def test_a_provider_the_fact_is_not_stated_for_is_refused(fact: str) -> None:
    connector = types.SimpleNamespace(provider=lambda _conn: "oracle")

    with pytest.raises(DatabaseError, match="oracle"):
        getattr(DatabaseTable, fact)(object(), connector)


def _export(tmp_path: Any, monkeypatch: pytest.MonkeyPatch, provider: str, **backend: Any) -> None:
    listed = [
        {"database": "rey", "object_type": "constraint", "schema": "control",
         "name": "run_log_batch_fkey", "table": "run_log", "kind": "foreign_key",
         "dependencies": []},
        {"database": "rey", "object_type": "index", "schema": "control",
         "name": "run_log_status_idx", "table": "run_log", "dependencies": []},
    ]
    namespace = types.SimpleNamespace(
        list_database_objects=lambda _c, database=None: listed,
        quote_identifier=_QUOTE[provider],
        **backend,
    )
    monkeypatch.setattr(adapter_module, "_backend", lambda _provider: namespace)
    DatabaseDdlExport.export(
        types.SimpleNamespace(provider=provider), connection="rey", output_root=str(tmp_path),
    )


def test_the_export_asks_the_owning_object_by_kind_and_table(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple] = []
    _export(
        tmp_path, monkeypatch, "sqlserver",
        constraint_catalog=lambda _c, schema, table, name, **_k: asked.append(
            (schema, table, name)) or _FOREIGN,
        index_column_catalog=lambda *_a, **_k: {"unique": False, "primary": False, "columns": ["s"]},
    )

    assert asked == [("control", "run_log", "run_log_batch_fkey")]
    (written,) = [p for p in tmp_path.rglob("*") if p.is_file() and "fkey" in p.name]
    assert written.read_text(encoding="utf-8") == (
        "ALTER TABLE [control].[run_log] ADD CONSTRAINT [run_log_batch_fkey] "
        "FOREIGN KEY ([batch_id]) REFERENCES [control].[batch] ([batch_id]);\nGO\n"
    )


def test_where_the_table_creates_its_children_the_export_does_not_emit_them(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MySQL: SHOW CREATE TABLE already creates every key and index."""
    _export(
        tmp_path, monkeypatch, "mysql",
        constraint_catalog=lambda *_a, **_k: pytest.fail("a child is not emitted separately"),
        index_column_catalog=lambda *_a, **_k: pytest.fail("a child is not emitted separately"),
    )

    assert not [p for p in tmp_path.rglob("*") if p.is_file() and "run_log_" in p.name]


def test_duckdb_emits_its_indexes_but_not_its_constraints(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its CREATE TABLE creates its constraints, not its indexes."""
    asked: list[str] = []
    monkeypatch.setattr(
        DatabaseTable, "create_includes_constraints", classmethod(lambda _c, *_a: True),
    )
    monkeypatch.setattr(
        DatabaseTable, "create_includes_indexes", classmethod(lambda _c, *_a: False),
    )
    # Replaced through the one read_ddl contract, so the export asking the
    # listed index for its own DDL reaches it (backlog 723).
    monkeypatch.setattr(
        "rey_lib.db.database_objects.DatabaseIndex.read_ddl",
        database_objects._ReadDdl(
            lambda _c, _conn, _table, name, _adapter=None, **_k: asked.append(name)
            or "CREATE INDEX x;",
        ),
    )
    _export(
        tmp_path, monkeypatch, "postgres",
        constraint_catalog=lambda *_a, **_k: pytest.fail("a constraint is not emitted separately"),
    )

    assert asked == ["run_log_status_idx"]


def test_postgres_states_a_constraint_s_kind_in_rey_s_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[dict] = []
    monkeypatch.setattr(postgres_utils, "fetch_rows", lambda _c, _sql, params: asked.append(
        params) or [
            {"contype": "f", "column_name": "batch_id", "referenced_schema": "control",
             "referenced_table": "batch", "referenced_column": "batch_id", "check_clause": None},
        ])

    catalog = postgres_utils.constraint_catalog(object(), "control", "run_log", "fk")

    assert catalog["kind"] == "foreign_key"
    assert (catalog["columns"], catalog["referenced_columns"]) == (["batch_id"], ["batch_id"])
    assert asked == [{"schema": "control", "table": "run_log", "name": "fk"}]
