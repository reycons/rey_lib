"""DatabaseSchema owns CREATE SCHEMA; there is no generic DDL owner (backlog 695).

CREATE SCHEMA was the last branch of each provider's ``get_object_ddl``. It is
``DatabaseSchema.read_ddl`` now, and ``get_object_ddl`` -- the provider's and
DBAdapter's -- is gone: every database object writes its own DDL, and the DDL
export refuses an object type no object owns rather than handing it to a
generic provider dispatch.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from rey_lib.db import db_adapter as adapter_module
from rey_lib.db import duckdb_utils, mysql_utils, postgres_utils, sqlserver_utils
from rey_lib.db.database_objects import DatabaseSchema, DatabaseTable
from rey_lib.db.db_adapter import DBAdapter
from rey_lib.errors.error_utils import DatabaseError

_QUOTE = {
    "postgres": postgres_utils.quote_identifier,
    "mysql": mysql_utils.quote_identifier,
    "sqlserver": sqlserver_utils.quote_identifier,
    "duckdb": duckdb_utils.quote_identifier,
}


def _connector(provider: str) -> Any:
    return types.SimpleNamespace(
        provider=lambda _conn: provider,
        quote_identifier=lambda _conn, name: _QUOTE[provider](name),
    )


@pytest.mark.parametrize(("provider", "expected"), [
    ("postgres", 'CREATE SCHEMA IF NOT EXISTS "staging";'),
    ("mysql", "CREATE SCHEMA IF NOT EXISTS `staging`;"),
    ("duckdb", 'CREATE SCHEMA IF NOT EXISTS "staging";'),
    ("sqlserver", "IF SCHEMA_ID(N'staging') IS NULL EXEC('CREATE SCHEMA [staging]');"),
])
def test_each_provider_s_create_schema(provider: str, expected: str) -> None:
    assert DatabaseSchema.read_ddl(object(), "staging", _connector(provider)) == expected


def test_sql_server_escapes_any_schema_name() -> None:
    """The name is a literal, an identifier, and inside EXEC's string: each escaped."""
    ddl = DatabaseSchema.read_ddl(object(), "o'brien]x", _connector("sqlserver"))

    assert ddl == (
        "IF SCHEMA_ID(N'o''brien]x') IS NULL EXEC('CREATE SCHEMA [o''brien]]x]');"
    )


def test_an_unknown_provider_is_refused_by_name() -> None:
    with pytest.raises(DatabaseError, match="oracle"):
        DatabaseSchema.read_ddl(object(), "staging", _connector("oracle"))


@pytest.mark.parametrize("module", [postgres_utils, mysql_utils, sqlserver_utils, duckdb_utils])
def test_no_provider_dispatches_object_ddl(module: Any) -> None:
    assert not hasattr(module, "get_object_ddl")


def test_the_adapter_has_no_generic_object_ddl() -> None:
    assert not hasattr(DBAdapter, "get_object_ddl")


def _export(tmp_path: Any, monkeypatch: pytest.MonkeyPatch, listed: list[dict]) -> None:
    backend = types.SimpleNamespace(
        list_database_objects=lambda _c, database=None: listed,
        quote_identifier=postgres_utils.quote_identifier,
    )
    monkeypatch.setattr(adapter_module, "_backend", lambda _provider: backend)
    DBAdapter().export_database_ddl(
        types.SimpleNamespace(provider="postgres"), output_root=str(tmp_path),
    )


def test_the_export_asks_the_schema_for_its_ddl(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _export(tmp_path, monkeypatch, [
        {"database": "rey", "object_type": "schemas", "schema": "staging", "name": "staging",
         "dependencies": []},
    ])

    (written,) = [p for p in tmp_path.rglob("*") if p.is_file() and "staging" in p.name]
    assert written.read_text(encoding="utf-8") == 'CREATE SCHEMA IF NOT EXISTS "staging";\n'


def test_an_unknown_listed_type_is_refused_not_made_a_table(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not handed to a provider, and not normalised into a table: it has no owner."""
    monkeypatch.setattr(
        DatabaseTable, "read_ddl",
        classmethod(lambda *_a: pytest.fail("an unknown type was routed to DatabaseTable")),
    )

    with pytest.raises(DatabaseError, match="No database object owns the DDL of a 'rule'"):
        _export(tmp_path, monkeypatch, [
            {"database": "rey", "object_type": "rule", "schema": "staging", "name": "r",
             "dependencies": []},
        ])


def test_a_known_alias_still_normalises_to_its_canonical_type() -> None:
    assert adapter_module._normalise_object_type("Index") == "indexes"
    assert adapter_module._normalise_object_type("rule") == "rule"
