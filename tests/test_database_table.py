"""DatabaseTable and DatabaseType: table and type semantics, out of the connector (backlog 690).

No database is opened. A connector double answers the four primitives
``DatabaseTable`` uses -- ``provider``, ``quote_identifier`` (each provider's
real one), ``execute_statement`` and ``fetch_rows`` -- and records what it was
asked, so what is asserted is what the TABLE decided for each provider.
"""

from __future__ import annotations

from typing import Any

import duckdb
import pytest

from rey_lib.data.logical_type import LogicalType
from rey_lib.db import duckdb_utils, mysql_utils, postgres_utils, sqlserver_utils
from rey_lib.db.database_objects import DatabaseObjectIdentity, DatabaseTable
from rey_lib.db.database_type import DatabaseType
from rey_lib.db.db_adapter import DBAdapter
from rey_lib.errors.error_utils import ConfigError, DatabaseError

_QUOTE = {
    "postgres": postgres_utils.quote_identifier,
    "mysql": mysql_utils.quote_identifier,
    "sqlserver": sqlserver_utils.quote_identifier,
    "duckdb": duckdb_utils.quote_identifier,
}

_COLUMNS = [
    ("trade_id", LogicalType("integer")),
    ("symbol", LogicalType("text", length=12)),
    ("price", LogicalType("decimal", precision=12, scale=2)),
    ("note", LogicalType("text")),
    ("traded", LogicalType("datetime")),
]


class _Conn:
    """The handle a load passes down, watching for a transaction it ends."""

    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


class _Connector:
    """The four primitives, as one provider, recording what was asked."""

    def __init__(self, provider: str, *, present: bool = False, fail: Exception | None = None):
        self._provider = provider
        self.present = present
        self.fail = fail
        self.statements: list[str] = []
        self.reads: list[tuple[str, dict]] = []

    def provider(self, _conn: Any) -> str:
        return self._provider

    def quote_identifier(self, _conn: Any, name: str) -> str:
        return _QUOTE[self._provider](name)

    def execute_statement(self, _conn: Any, sql_text: str) -> int:
        if self.fail is not None:
            raise self.fail
        self.statements.append(sql_text)
        return 0

    def fetch_rows(self, _conn: Any, sql_text: str, params: dict[str, Any]) -> list[dict]:
        self.reads.append((sql_text, dict(params)))
        return [{"present": 1}] if self.present else []


def _identity(catalog: str = "") -> DatabaseObjectIdentity:
    return DatabaseObjectIdentity(connection="c", catalog=catalog, schema="staging", name="trade")


class TestDatabaseTypeIsTheOneTranslation:

    @pytest.mark.parametrize(("provider", "expected"), [
        ("postgres", ["INTEGER", "VARCHAR(12)", "DECIMAL(12, 2)", "TEXT", "TIMESTAMP"]),
        ("mysql", ["INT", "VARCHAR(12)", "DECIMAL(12, 2)", "TEXT", "DATETIME"]),
        ("sqlserver", ["INT", "NVARCHAR(12)", "DECIMAL(12, 2)", "NVARCHAR(MAX)", "DATETIME2"]),
        ("duckdb", ["INTEGER", "VARCHAR", "DECIMAL(12, 2)", "VARCHAR", "TIMESTAMP"]),
    ])
    def test_each_provider_spells_each_kind_its_own_way(
        self, provider: str, expected: list[str],
    ) -> None:
        types = DatabaseType(provider)

        assert [types.column_type(logical) for _name, logical in _COLUMNS] == expected

    def test_date_and_time_are_the_same_everywhere(self) -> None:
        for provider in _QUOTE:
            types = DatabaseType(provider)
            assert (types.column_type(LogicalType("date")),
                    types.column_type(LogicalType("time"))) == ("DATE", "TIME")

    def test_an_unknown_provider_is_refused_by_name(self) -> None:
        with pytest.raises(ConfigError, match="oracle"):
            DatabaseType("oracle").column_type(LogicalType("integer"))

    def test_a_kind_rey_does_not_have_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="boolean"):
            LogicalType("boolean")


class TestExistsIsTheTablesQuestion:

    def test_it_asks_the_catalog_with_named_parameters(self) -> None:
        connector = _Connector("postgres", present=True)

        assert DatabaseTable.exists(_Conn(), _identity(), connector) is True
        ((sql_text, params),) = connector.reads
        assert "information_schema.tables" in sql_text
        assert ":schema" in sql_text and ":table" in sql_text
        assert params == {"schema": "staging", "table": "trade"}

    def test_an_absent_table_is_False(self) -> None:
        assert DatabaseTable.exists(_Conn(), _identity(), _Connector("mysql")) is False

    def test_sql_server_asks_the_named_database_s_own_catalog(self) -> None:
        connector = _Connector("sqlserver")

        DatabaseTable.exists(_Conn(), _identity(catalog="NaviStage"), connector)

        assert "FROM [NaviStage].information_schema.tables" in connector.reads[0][0]

    def test_a_failed_read_is_reported_not_answered_False(self) -> None:
        """A lost connection must not read as "the table is not there"."""
        class _Failing(_Connector):
            def fetch_rows(self, *_a: Any) -> list[dict]:
                raise DatabaseError("connection closed")

        with pytest.raises(DatabaseError, match="connection closed"):
            DatabaseTable.exists(_Conn(), _identity(), _Failing("postgres"))


class TestCreate:

    @pytest.mark.parametrize(("provider", "table", "first_column"), [
        ("postgres", '"staging"."trade"', '"trade_id" INTEGER NULL'),
        ("mysql", "`staging`.`trade`", "`trade_id` INT NULL"),
        ("sqlserver", "[staging].[trade]", "[trade_id] INT NULL"),
        ("duckdb", '"staging"."trade"', '"trade_id" INTEGER'),
    ])
    def test_it_writes_the_provider_s_create_with_translated_types(
        self, provider: str, table: str, first_column: str,
    ) -> None:
        connector = _Connector(provider)

        assert DatabaseTable.create(_Conn(), _identity(), _COLUMNS, connector) is True

        (ddl,) = connector.statements
        assert ddl.startswith(f"CREATE TABLE {table} (\n\t{first_column},")
        assert "IF NOT EXISTS" not in ddl  # existence is the table's own question

    @pytest.mark.parametrize(("provider", "commits"), [
        ("postgres", 0), ("duckdb", 0), ("sqlserver", 1), ("mysql", 1),
    ])
    def test_it_commits_only_where_table_ddl_is_not_transactional(
        self, provider: str, commits: int,
    ) -> None:
        conn = _Conn()

        DatabaseTable.create(conn, _identity(), _COLUMNS, _Connector(provider))

        assert conn.commits == commits

    def test_sql_server_names_the_database_when_the_identity_carries_one(self) -> None:
        connector = _Connector("sqlserver")

        DatabaseTable.create(_Conn(), _identity(catalog="NaviStage"), _COLUMNS, connector)

        assert connector.statements[0].startswith("CREATE TABLE [NaviStage].[staging].[trade] (")

    def test_an_existing_table_is_left_untouched(self) -> None:
        connector = _Connector("postgres", present=True)

        assert DatabaseTable.create(_Conn(), _identity(), _COLUMNS, connector) is False
        assert connector.statements == []

    def test_no_columns_is_refused_rather_than_creating_an_empty_table(self) -> None:
        connector = _Connector("postgres")

        with pytest.raises(DatabaseError, match="no columns"):
            DatabaseTable.create(_Conn(), _identity(), [], connector)
        assert connector.statements == []

    def test_an_awkward_column_name_is_quoted_not_refused(self) -> None:
        connector = _Connector("postgres")

        DatabaseTable.create(
            _Conn(), _identity(), [('trade "id"', LogicalType("integer"))], connector,
        )

        assert '"trade ""id""" INTEGER NULL' in connector.statements[0]

    def test_a_failed_create_names_the_table(self) -> None:
        connector = _Connector("postgres", fail=DatabaseError("permission denied"))

        with pytest.raises(DatabaseError, match="staging.trade: permission denied"):
            DatabaseTable.create(_Conn(), _identity(), _COLUMNS, connector)


class TestWidenColumn:

    @pytest.mark.parametrize(("provider", "statement"), [
        ("postgres", 'ALTER TABLE "staging"."trade" ALTER COLUMN "symbol" TYPE VARCHAR(40)'),
        ("mysql", "ALTER TABLE `staging`.`trade` MODIFY COLUMN `symbol` VARCHAR(40) NULL"),
        ("sqlserver", "ALTER TABLE [staging].[trade] ALTER COLUMN [symbol] NVARCHAR(40) NULL"),
        ("duckdb", 'ALTER TABLE "staging"."trade" ALTER COLUMN "symbol" TYPE VARCHAR'),
    ])
    def test_each_provider_alters_through_the_same_translation(
        self, provider: str, statement: str,
    ) -> None:
        connector = _Connector(provider)

        DatabaseTable(identity=_identity()).widen_column(
            _Conn(), "symbol", LogicalType("text", length=40), connector,
        )

        assert connector.statements == [statement]


class _CatalogConnector(_Connector):
    """The connector, also answering what a catalog says about the table."""

    def __init__(self, provider: str, *, columns: list[dict] | None = None, native: str = ""):
        super().__init__(provider)
        self.columns = columns or []
        self.native = native

    def table_column_catalog(self, _conn: Any, _schema: str, _table: str) -> list[dict]:
        return self.columns

    def table_native_definition(self, _conn: Any, _schema: str, _table: str) -> str:
        return self.native


_CATALOG_COLUMNS = [
    {"name": "trade_id", "type": "integer", "default": None, "nullable": False},
    {"name": "symbol", "type": "character varying(12)", "default": "'X'::text", "nullable": True},
]


class TestReadDdl:
    """Backlog 695: a table reads its own DDL; how the catalog is asked is the connector's."""

    def test_postgres_composes_from_the_catalog_s_own_spelling(self) -> None:
        connector = _CatalogConnector("postgres", columns=_CATALOG_COLUMNS)

        assert DatabaseTable.read_ddl(_Conn(), _identity("rey"), connector) == (
            'CREATE TABLE "staging"."trade" (\n'
            '    "trade_id" integer NOT NULL,\n'
            '    "symbol" character varying(12) DEFAULT \'X\'::text NULL\n'
            ");"
        )

    def test_sql_server_composes_and_ends_the_batch(self) -> None:
        columns = [{"name": "trade_id", "type": "int", "default": None, "nullable": False}]
        connector = _CatalogConnector("sqlserver", columns=columns)

        assert DatabaseTable.read_ddl(_Conn(), _identity(), connector) == (
            "CREATE TABLE [staging].[trade] (\n    [trade_id] int NOT NULL\n);\nGO"
        )

    @pytest.mark.parametrize(("provider", "native", "expected"), [
        ("mysql", "CREATE TABLE `trade` (\n  `trade_id` int\n) ENGINE=InnoDB",
         "CREATE TABLE `trade` (\n  `trade_id` int\n) ENGINE=InnoDB;"),
        ("duckdb", "CREATE TABLE trade(trade_id INTEGER);",
         "CREATE TABLE trade(trade_id INTEGER);"),
    ])
    def test_a_server_s_own_definition_is_the_ddl_untouched(
        self, provider: str, native: str, expected: str,
    ) -> None:
        connector = _CatalogConnector(provider, native=native)

        assert DatabaseTable.read_ddl(_Conn(), _identity(), connector) == expected

    @pytest.mark.parametrize("provider", ["postgres", "mysql"])
    def test_a_missing_table_is_refused_by_name(self, provider: str) -> None:
        with pytest.raises(DatabaseError, match="table not found: staging.trade"):
            DatabaseTable.read_ddl(_Conn(), _identity(), _CatalogConnector(provider))


class TestTheCatalogPrimitives:
    """What the providers retrieve: facts and native definitions, never DDL."""

    def test_postgres_states_columns_from_pg_attribute(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        asked: list[str] = []

        def _rows(_conn: Any, sql_text: str, params: dict) -> list[dict]:
            asked.append(sql_text)
            return [{"name": "a", "type": "integer", "default_value": None, "nullable": False}]

        monkeypatch.setattr(postgres_utils, "fetch_rows", _rows)

        assert postgres_utils.table_column_catalog(object(), "s", "t") == [
            {"name": "a", "type": "integer", "default": None, "nullable": False},
        ]
        assert "pg_attribute" in asked[0] and "format_type" in asked[0]

    def test_sql_server_states_each_type_as_it_declares_it(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sqlserver_utils, "fetch_rows", lambda *_a: [
            {"name": "symbol", "type_name": "nvarchar", "max_length": 24, "precision": 0,
             "scale": 0, "is_nullable": True, "default_value": None},
        ])

        assert sqlserver_utils.table_column_catalog(object(), "s", "t") == [
            {"name": "symbol", "type": "nvarchar(12)", "default": None, "nullable": True},
        ]

    def test_mysql_retrieves_show_create_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        asked: list[str] = []
        native = "CREATE TABLE `t` (`a` int) ENGINE=InnoDB"
        monkeypatch.setattr(mysql_utils, "fetch_rows", lambda _c, sql_text, _p: asked.append(
            sql_text) or [{"Table": "t", "Create Table": native}])

        assert mysql_utils.table_native_definition(object(), "s", "t") == (
            "CREATE TABLE `t` (`a` int) ENGINE=InnoDB"
        )
        assert asked == ["SHOW CREATE TABLE `s`.`t`"]

    def test_duckdb_retrieves_its_catalog_statement(self) -> None:
        conn = duckdb.connect(":memory:")
        conn.execute("CREATE TABLE trade (symbol VARCHAR NOT NULL)")

        assert duckdb_utils.table_native_definition(conn, "main", "trade") == (
            "CREATE TABLE trade(symbol VARCHAR NOT NULL);"
        )

    def test_the_ddl_export_writes_a_table_s_own_ddl(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End to end on DuckDB: the listing is scripted (backlog 697), the DDL is the table's."""
        conn = duckdb.connect(":memory:")
        conn.execute("CREATE TABLE trade (symbol VARCHAR)")
        monkeypatch.setattr(duckdb_utils, "list_database_objects", lambda _c, database=None: [
            {"database": "memory", "object_type": "table", "schema": "main", "name": "trade",
             "dependencies": []},
        ])

        DBAdapter().export_database_ddl(conn, output_root=str(tmp_path))

        (written,) = [p for p in tmp_path.rglob("*") if p.is_file() and "trade" in p.name]
        assert written.read_text(encoding="utf-8") == "CREATE TABLE trade(symbol VARCHAR);\n"
