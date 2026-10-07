"""DatabaseUserDefinedType owns user-defined type DDL (backlog 695).

A PostgreSQL enum or domain and a SQL Server alias type are variants of one
schema-scoped database object, identified by schema and name, with a kind.
Its DDL used to be provider branches in ``get_object_ddl``; the type composes
it now from what its catalog states. It is not DatabaseType, which spells a
Rey logical type at a destination and has no identity. SQL Server table and
CLR types are not listed: this object cannot represent them correctly.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from rey_lib.db import db_adapter as adapter_module
from rey_lib.db import postgres_utils, sqlserver_utils
from rey_lib.db.database_objects import DatabaseObjectIdentity, DatabaseUserDefinedType
from rey_lib.db.db_adapter import DBAdapter
from rey_lib.errors.error_utils import DatabaseError

_IDENTITY = DatabaseObjectIdentity(connection="c", catalog="rey", schema="control", name="mood")

_QUOTE = {
    "postgres": postgres_utils.quote_identifier,
    "sqlserver": sqlserver_utils.quote_identifier,
}


def _catalog(kind: str, **facts: Any) -> dict:
    return {"kind": kind, "base_type": "", "default": None, "not_null": False, "labels": [],
            **facts}


class _Connector:
    """The type primitive, as one provider, recording what was asked."""

    def __init__(self, provider: str, catalog: dict) -> None:
        self._provider = provider
        self.catalog = catalog
        self.asked: list[tuple] = []

    def provider(self, _conn: Any) -> str:
        return self._provider

    def quote_identifier(self, _conn: Any, name: str) -> str:
        return _QUOTE[self._provider](name)

    def user_type_catalog(self, _conn: Any, schema: str, name: str) -> dict:
        self.asked.append((schema, name))
        return self.catalog


def test_an_enum_lists_its_labels_in_order_escaped() -> None:
    connector = _Connector("postgres", _catalog("enum", labels=["ok", "can't"]))

    assert DatabaseUserDefinedType.read_ddl(object(), _IDENTITY, connector) == (
        """CREATE TYPE "control"."mood" AS ENUM ('ok', 'can''t');"""
    )
    assert connector.asked == [("control", "mood")]


def test_a_domain_carries_its_base_default_and_nullability() -> None:
    connector = _Connector("postgres", _catalog(
        "domain", base_type="character varying(20)", default="'x'::character varying",
        not_null=True,
    ))

    assert DatabaseUserDefinedType.read_ddl(object(), _IDENTITY, connector) == (
        'CREATE DOMAIN "control"."mood" AS character varying(20) '
        "DEFAULT 'x'::character varying NOT NULL;"
    )


def test_a_sql_server_alias_names_its_base_and_ends_the_batch() -> None:
    connector = _Connector("sqlserver", _catalog("alias", base_type="nvarchar(20)"))

    assert DatabaseUserDefinedType.read_ddl(object(), _IDENTITY, connector) == (
        "CREATE TYPE [control].[mood] FROM nvarchar(20);\nGO"
    )


@pytest.mark.parametrize("kind", ["table", "clr"])
def test_a_kind_it_does_not_represent_is_refused(kind: str) -> None:
    with pytest.raises(DatabaseError, match=kind):
        DatabaseUserDefinedType.read_ddl(
            object(), _IDENTITY, _Connector("sqlserver", _catalog(kind)),
        )


def test_a_type_that_is_not_there_is_refused_by_name() -> None:
    with pytest.raises(DatabaseError, match="control.mood"):
        DatabaseUserDefinedType.read_ddl(object(), _IDENTITY, _Connector("postgres", {}))


def test_postgres_reads_an_enum_s_labels_in_sort_order(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter([
        [{"typtype": "e", "base_type": "-", "not_null": False, "default_value": None}],
        [{"enumlabel": "happy"}, {"enumlabel": "sad"}],
    ])
    asked: list[str] = []
    monkeypatch.setattr(postgres_utils, "fetch_rows", lambda _c, sql, _p: asked.append(
        sql) or next(answers))

    catalog = postgres_utils.user_type_catalog(object(), "control", "mood")

    assert (catalog["kind"], catalog["labels"]) == ("enum", ["happy", "sad"])
    assert "ORDER BY e.enumsortorder" in asked[1]


def test_sql_server_spells_an_alias_s_base_as_it_declares_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sqlserver_utils, "fetch_rows", lambda *_a: [{
        "base_name": "nvarchar", "max_length": 40, "precision": 0, "scale": 0,
        "is_table_type": False, "is_assembly_type": False,
    }])

    catalog = sqlserver_utils.user_type_catalog(object(), "control", "mood")

    assert (catalog["kind"], catalog["base_type"]) == ("alias", "nvarchar(20)")


def test_the_ddl_export_asks_the_type_by_its_identity(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple] = []
    backend = types.SimpleNamespace(
        list_database_objects=lambda _c, database=None: [
            {"database": "rey", "object_type": "types", "schema": "control", "name": "mood",
             "object_oid": 77, "dependencies": []},
        ],
        quote_identifier=postgres_utils.quote_identifier,
        user_type_catalog=lambda _c, schema, name: asked.append(
            (schema, name)) or _catalog("enum", labels=["ok"]),
    )
    monkeypatch.setattr(adapter_module, "_backend", lambda _provider: backend)

    DBAdapter().export_database_ddl(
        types.SimpleNamespace(provider="postgres"), output_root=str(tmp_path),
    )

    assert asked == [("control", "mood")]
    (written,) = [p for p in tmp_path.rglob("*") if p.is_file() and "mood" in p.name]
    assert written.read_text(encoding="utf-8") == (
        'CREATE TYPE "control"."mood" AS ENUM (\'ok\');\n'
    )
