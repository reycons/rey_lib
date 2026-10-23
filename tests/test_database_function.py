"""DatabaseFunction owns its DDL (backlog 695).

A function's DDL used to be a branch in each provider's ``get_object_ddl``;
it is ``DatabaseFunction.read_ddl`` now. The server's stored source is a
retrieved fact (``DBAdapter.routine_native_definition``), addressed by Rey's
routine identity -- schema, name, signature -- and never by a provider handle
such as PostgreSQL's oid. The function ends the statement for its provider.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from rey_lib.db import db_adapter as adapter_module
from rey_lib.db.database_objects import DatabaseFunction, DatabaseRoutineIdentity
from rey_lib.db.ddl_export import DatabaseDdlExport
from rey_lib.errors.error_utils import UnsupportedDatabaseCapabilityError

_IDENTITY = DatabaseRoutineIdentity(
    connection="c", catalog="rey", schema="control", name="run_total", signature="integer",
)


class _Connector:
    """Answers one provider's stored routine source, recording what was asked."""

    def __init__(self, provider: str, source: str) -> None:
        self._provider = provider
        self.source = source
        self.asked: list[tuple] = []

    def provider(self, _conn: Any) -> str:
        return self._provider

    def routine_native_definition(
        self, _conn: Any, schema: str, name: str, signature: str, kind: str,
    ) -> str:
        self.asked.append((schema, name, signature, kind))
        if self._provider == "duckdb":
            raise UnsupportedDatabaseCapabilityError("duckdb has no routines")
        return self.source


@pytest.mark.parametrize(("provider", "source", "expected"), [
    ("postgres", "CREATE OR REPLACE FUNCTION control.run_total(integer) ...;",
     "CREATE OR REPLACE FUNCTION control.run_total(integer) ...;"),
    ("mysql", "CREATE FUNCTION `run_total`() RETURNS INT RETURN 1",
     "CREATE FUNCTION `run_total`() RETURNS INT RETURN 1;"),
    ("sqlserver", "CREATE FUNCTION control.run_total() RETURNS INT AS BEGIN RETURN 1 END\n",
     "CREATE FUNCTION control.run_total() RETURNS INT AS BEGIN RETURN 1 END\nGO"),
])
def test_each_provider_s_source_ends_as_its_script_needs(
    provider: str, source: str, expected: str,
) -> None:
    connector = _Connector(provider, source)

    assert DatabaseFunction.read_ddl(object(), _IDENTITY, connector) == expected
    assert connector.asked == [("control", "run_total", "integer", "function")]


def test_a_provider_without_functions_is_refused_by_name() -> None:
    with pytest.raises(UnsupportedDatabaseCapabilityError, match="duckdb"):
        DatabaseFunction.read_ddl(object(), _IDENTITY, _Connector("duckdb", ""))


def test_the_ddl_export_asks_the_function_by_identity(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The export lists and orders; the function writes itself, found by name and signature."""
    asked: list[tuple] = []
    backend = types.SimpleNamespace(
        list_database_objects=lambda _c, database=None: [
            {"database": "rey", "object_type": "functions", "schema": "control",
             "name": "run_total", "signature": "integer", "object_oid": 4242,
             "dependencies": []},
        ],
        get_routine_definition=lambda _c, _catalog, schema, name, signature, kind: asked.append(
            (schema, name, signature, kind)) or "CREATE FUNCTION control.run_total(integer) ...;",
    )
    monkeypatch.setattr(adapter_module, "_backend", lambda _provider: backend)
    conn = types.SimpleNamespace(provider="postgres")

    DatabaseDdlExport.export(conn, connection="rey", output_root=str(tmp_path))

    assert asked == [("control", "run_total", "integer", "function")]
    (written,) = [p for p in tmp_path.rglob("*") if p.is_file() and "run_total" in p.name]
    assert written.read_text(encoding="utf-8") == (
        "CREATE FUNCTION control.run_total(integer) ...;\n"
    )


def test_a_function_reads_itself_by_its_routine_identity() -> None:
    """DatabaseFunction.read: the functions listing, matched on name and signature (1102)."""
    from typing import Any as _Any

    from rey_lib.db.database_objects import DatabaseFunction, DatabaseRoutineIdentity

    class _Listing:
        def provider(self, _conn: _Any) -> str:
            return "postgres"

        def routine_native_definition(self, *_a: _Any) -> str:
            return "CREATE FUNCTION control.f(integer) ...;"

        def list_functions(self, _conn: _Any, _schema: str) -> list[dict]:
            return [{"name": "f", "signature": "integer", "invocation": "",
                     "parameters": [], "returns_rows": False}]

    identity = DatabaseRoutineIdentity(
        connection="c", catalog="", schema="control", name="f", signature="integer",
    )

    function = DatabaseFunction.read(object(), identity, _Listing())

    assert function.identity == identity
    # An empty invocation is the provider's ordinary answer, kept empty.
    assert function.invocation == ""
