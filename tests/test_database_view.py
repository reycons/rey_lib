"""DatabaseView owns its own DDL (backlog 695).

A view's DDL used to be a branch in each provider's ``get_object_ddl``; it is
``DatabaseView.read_ddl`` now, reaching the database through the connector's
primitives. Asserted per provider against a scripted connector, and for DuckDB
end to end against a real in-memory database -- the provider whose old
statement did not parse.
"""

from __future__ import annotations

from typing import Any

import duckdb
import pytest

from rey_lib.db import duckdb_utils, mysql_utils, postgres_utils, sqlserver_utils
from rey_lib.db.database_objects import DatabaseObjectIdentity, DatabaseSchema, DatabaseView
from rey_lib.db.db_adapter import DBAdapter
from rey_lib.errors.error_utils import DatabaseError

_QUOTE = {
    "postgres": postgres_utils.quote_identifier,
    "mysql": mysql_utils.quote_identifier,
    "sqlserver": sqlserver_utils.quote_identifier,
    "duckdb": duckdb_utils.quote_identifier,
}

_IDENTITY = DatabaseObjectIdentity(connection="c", catalog="rey", schema="control", name="run_vw")


class _Connector:
    """The primitives, as one provider, answering every read with ``rows``."""

    def __init__(self, provider: str, rows: list[dict]) -> None:
        self._provider = provider
        self.rows = rows
        self.reads: list[tuple[str, dict]] = []

    def provider(self, _conn: Any) -> str:
        return self._provider

    def quote_identifier(self, _conn: Any, name: str) -> str:
        return _QUOTE.get(self._provider, postgres_utils.quote_identifier)(name)

    def fetch_rows(self, _conn: Any, sql_text: str, params: dict[str, Any]) -> list[dict]:
        self.reads.append((sql_text, dict(params)))
        return self.rows


def test_postgres_writes_create_or_replace_from_its_definition() -> None:
    connector = _Connector("postgres", [{"definition": " SELECT 1 AS run_id"}])

    ddl = DatabaseView.read_ddl(object(), _IDENTITY, connector)

    assert ddl == 'CREATE OR REPLACE VIEW "control"."run_vw" AS\n SELECT 1 AS run_id;'
    ((sql_text, params),) = connector.reads
    assert "pg_get_viewdef" in sql_text
    assert params == {"schema": "control", "view": "run_vw"}


def test_mysql_answers_with_its_own_create_view() -> None:
    connector = _Connector(
        "mysql", [{"View": "run_vw", "Create View": "CREATE VIEW `run_vw` AS select 1 "}],
    )

    ddl = DatabaseView.read_ddl(object(), _IDENTITY, connector)

    assert ddl == "CREATE VIEW `run_vw` AS select 1;"
    assert connector.reads[0][0] == "SHOW CREATE VIEW `control`.`run_vw`"


def test_sql_server_reads_the_definition_by_name_and_ends_the_batch() -> None:
    connector = _Connector(
        "sqlserver", [{"definition": "CREATE VIEW control.run_vw AS SELECT 1\n"}],
    )

    ddl = DatabaseView.read_ddl(object(), _IDENTITY, connector)

    assert ddl == "CREATE VIEW control.run_vw AS SELECT 1\nGO"
    assert connector.reads[0][1] == {"qualified": "[control].[run_vw]"}


def test_sql_server_with_no_definition_is_empty() -> None:
    connector = _Connector("sqlserver", [{"definition": None}])

    assert DatabaseView.read_ddl(object(), _IDENTITY, connector) == ""


def test_the_catalog_a_read_carries_is_not_written_into_the_statement() -> None:
    connector = _Connector("postgres", [{"definition": "SELECT 1"}])

    assert '"rey"' not in DatabaseView.read_ddl(object(), _IDENTITY, connector)


def test_an_unknown_provider_is_refused_by_name() -> None:
    with pytest.raises(DatabaseError, match="oracle"):
        DatabaseView.read_ddl(object(), _IDENTITY, _Connector("oracle", []))


def test_duckdb_answers_from_its_catalog_end_to_end() -> None:
    """The real database and the real adapter: the old SHOW CREATE VIEW did not parse."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE trade (symbol VARCHAR)")
    conn.execute("CREATE VIEW trade_vw AS SELECT symbol FROM trade")
    identity = DatabaseObjectIdentity(connection="c", catalog="", schema="main", name="trade_vw")

    ddl = DatabaseView.read_ddl(conn, identity, DBAdapter())

    assert ddl == "CREATE VIEW trade_vw AS SELECT symbol FROM trade;"


class _SchemaAdapter(_Connector):
    """Enough of a schema read for one view, its DDL answered as postgres."""

    def get_schema_metadata(self, _conn: Any, _schema: str) -> dict:
        return {"tables": [], "views": [{"name": "run_vw", "definition": "SELECT 1"}]}

    def get_columns(self, *_a: Any) -> list[dict]:
        return [{"name": "run_id", "type": "INTEGER", "nullable": False}]

    def list_procedures(self, *_a: Any) -> list:
        return []

    def list_functions(self, *_a: Any) -> list:
        return []


def test_a_schema_read_carries_the_view_s_own_ddl() -> None:
    adapter = _SchemaAdapter("postgres", [{"definition": " SELECT 1 AS run_id"}])

    schema = DatabaseSchema.read(object(), "c", "rey", "control", adapter)

    (view,) = schema.views
    assert view.ddl == 'CREATE OR REPLACE VIEW "control"."run_vw" AS\n SELECT 1 AS run_id;'


def test_the_ddl_export_writes_a_view_s_own_ddl(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The export decides which objects and in what order; a view writes itself.

    On a real DuckDB view. The object listing is scripted: DuckDB's own listing
    asks information_schema.sequences, which this DuckDB does not have -- a
    separate defect, outside view semantics.
    """
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE trade (symbol VARCHAR)")
    conn.execute("CREATE VIEW trade_vw AS SELECT symbol FROM trade")
    monkeypatch.setattr(duckdb_utils, "list_database_objects", lambda _conn, database=None: [
        {"database": "memory", "object_type": "schema", "schema": "main", "name": "main",
         "dependencies": []},
        {"database": "memory", "object_type": "view", "schema": "main", "name": "trade_vw",
         "dependencies": []},
    ])

    DBAdapter().export_database_ddl(conn, output_root=str(tmp_path))

    (written,) = [
        path for path in tmp_path.rglob("*") if path.is_file() and "trade_vw" in path.name
    ]
    assert written.read_text(encoding="utf-8") == (
        "CREATE VIEW trade_vw AS SELECT symbol FROM trade;\n"
    )
