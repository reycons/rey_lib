"""DatabaseTable and DatabaseType: table and type semantics, out of the connector (backlog 690).

No database is opened. A connector double answers the four primitives
``DatabaseTable`` uses -- ``provider``, ``quote_identifier`` (each provider's
real one), ``execute_statement`` and ``table_exists`` (698) -- and records what it was
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
from rey_lib.db.ddl_export import DatabaseDdlExport
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

    def __init__(
        self, provider: str, *, present: bool = False, fail: Exception | None = None,
        keyed: bool = False, listed: bool = True,
    ):
        self._provider = provider
        self.present = present
        self.fail = fail
        self.keyed = keyed
        self.listed = listed
        self.statements: list[str] = []
        self.asked: list[tuple[str, str, str]] = []
        self.read_from: list[tuple[str, str]] = []

    def provider(self, _conn: Any) -> str:
        return self._provider

    def quote_identifier(self, _conn: Any, name: str) -> str:
        return _QUOTE[self._provider](name)

    def execute_statement(self, _conn: Any, sql_text: str) -> int:
        if self.fail is not None:
            raise self.fail
        self.statements.append(sql_text)
        return 0

    def table_exists(self, _conn: Any, catalog: str, schema: str, table: str) -> bool:
        self.asked.append((catalog, schema, table))
        return self.present

    # The read-back (backlog 727): the schema's structure, then each fact
    # _table asks, every one recorded with the database it was asked of.

    def get_schema_metadata(
        self, _conn: Any, schema: str, *, database_name: str = "",
    ) -> dict[str, Any]:
        self.read_from.append(("schema_metadata", database_name))
        return {"views": [], "tables": [{
            "name": "trade", "foreign_keys": [], "check_constraints": [],
            "primary_key": {"name": "trade_pk", "constrained_columns": ["trade_id"]}
            if self.keyed else {},
            "indexes": [{"name": "trade_ix", "column_names": ["symbol"], "unique": False}]
            if self.keyed else [],
            "unique_constraints": [],
        }] if schema == "staging" and self.listed else []}

    def table_column_catalog(
        self, _conn: Any, _schema: str, _table: str, *, database_name: str = "",
    ) -> list[dict]:
        self.read_from.append(("table_column_catalog", database_name))
        return [{"name": "trade_id", "type": "INTEGER", "default": None, "nullable": True}]

    def constraint_catalog(
        self, _conn: Any, _schema: str, _table: str, _name: str, *, database_name: str = "",
    ) -> dict:
        self.read_from.append(("constraint_catalog", database_name))
        return {"kind": "primary_key", "columns": ["trade_id"], "referenced_schema": "",
                "referenced_table": "", "referenced_columns": [], "check": None}

    def index_column_catalog(
        self, _conn: Any, _schema: str, _table: str, _index: str, *, database_name: str = "",
    ) -> dict:
        self.read_from.append(("index_column_catalog", database_name))
        return {"unique": False, "primary": False, "columns": ["symbol"]}


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

    @pytest.mark.parametrize(("provider", "expected"), [
        ("postgres", "BOOLEAN"), ("mysql", "BOOLEAN"), ("sqlserver", "BIT"),
        ("duckdb", "BOOLEAN"),
    ])
    def test_boolean_is_each_providers_own_spelling(self, provider: str, expected: str) -> None:
        """Backlog 386: one entry per provider in the one translation."""
        assert DatabaseType(provider).column_type(LogicalType("boolean")) == expected

    def test_a_kind_rey_does_not_have_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="geometry"):
            LogicalType("geometry")


class TestExistsIsTheTablesQuestion:

    def test_it_asks_the_connector_s_existence_fact(self) -> None:
        """What "exists" means is the table's; how the catalog is asked is the
        connector's (backlog 698)."""
        connector = _Connector("postgres", present=True)

        assert DatabaseTable.exists(_Conn(), _identity(), connector) is True
        assert connector.asked == [("", "staging", "trade")]

    def test_an_absent_table_is_False(self) -> None:
        assert DatabaseTable.exists(_Conn(), _identity(), _Connector("mysql")) is False

    def test_sql_server_asks_the_named_database_s_own_catalog(self) -> None:
        connector = _Connector("sqlserver")

        DatabaseTable.exists(_Conn(), _identity(catalog="NaviStage"), connector)

        assert connector.asked == [("NaviStage", "staging", "trade")]

    def test_a_failed_read_is_reported_not_answered_False(self) -> None:
        """A lost connection must not read as "the table is not there"."""
        class _Failing(_Connector):
            def table_exists(self, *_a: Any) -> bool:
                raise DatabaseError("connection closed")

        with pytest.raises(DatabaseError, match="connection closed"):
            DatabaseTable.exists(_Conn(), _identity(), _Failing("postgres"))


class TestTheExistencePrimitive:
    """How each provider's catalog is asked (backlog 698)."""

    @pytest.mark.parametrize("module", [postgres_utils, mysql_utils, duckdb_utils])
    def test_it_asks_information_schema_with_named_parameters(
        self, module: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        asked: list[tuple[str, dict]] = []
        monkeypatch.setattr(module, "fetch_rows", lambda _c, sql, params: asked.append(
            (sql, dict(params))) or [{"present": 1}])

        assert module.table_exists(object(), "ignored", "staging", "trade") is True
        ((sql_text, params),) = asked
        assert "information_schema.tables" in sql_text
        assert params == {"schema": "staging", "table": "trade"}

    def test_sql_server_asks_the_named_database_s_own_catalog(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        asked: list[str] = []
        monkeypatch.setattr(sqlserver_utils, "fetch_rows",
                            lambda _c, sql, _params: asked.append(sql) or [])

        assert sqlserver_utils.table_exists(object(), "NaviStage", "staging", "trade") is False
        assert "FROM [NaviStage].information_schema.tables" in asked[0]


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

        created = DatabaseTable.create(_Conn(), _identity(), _COLUMNS, connector)

        assert created.identity == _identity()  # the table read back (backlog 727)
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

        found = DatabaseTable.create(_Conn(), _identity(), _COLUMNS, connector)

        assert found.identity == _identity()  # read back as it is (backlog 727)
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


class TestCreateReadsTheTableBack:
    """create returns the table read back, from the database it addressed (backlog 727)."""

    def test_the_connected_database_is_read_when_the_identity_names_none(self) -> None:
        connector = _Connector("postgres", keyed=True)

        table = DatabaseTable.create(_Conn(), _identity(), _COLUMNS, connector)

        assert [column.name for column in table.columns] == ["trade_id"]
        assert table.primary_key is not None and table.primary_key.name == "trade_pk"
        assert [index.name for index in table.indexes] == ["trade_ix"]
        assert {database for _read, database in connector.read_from} == {""}

    def test_sql_server_reads_every_fact_from_the_named_database(self) -> None:
        """If create can address the table, read addresses the same one."""
        connector = _Connector("sqlserver", present=True, keyed=True)

        table = DatabaseTable.create(
            _Conn(), _identity(catalog="NaviStage"), _COLUMNS, connector,
        )

        assert table.identity == _identity(catalog="NaviStage")
        assert {read for read, _database in connector.read_from} == {
            "schema_metadata", "table_column_catalog", "constraint_catalog",
            "index_column_catalog",
        }
        assert {database for _read, database in connector.read_from} == {"NaviStage"}

    def test_a_table_the_read_does_not_find_is_refused_by_name(self) -> None:
        connector = _Connector("postgres", listed=False)

        with pytest.raises(DatabaseError, match="table not found: staging.trade"):
            DatabaseTable.create(_Conn(), _identity(), _COLUMNS, connector)


class TestSqlServerReadsTheNamedDatabase:
    """Each read _table makes qualifies SQL Server's catalog views (backlog 727)."""

    def _capture(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        asked: list[str] = []
        monkeypatch.setattr(sqlserver_utils, "fetch_rows", lambda _c, sql_text, _p: asked.append(
            sql_text) or [])
        return asked

    @pytest.mark.parametrize(("read", "arguments"), [
        ("schema_metadata", ("staging",)),
        ("table_column_catalog", ("staging", "trade")),
        ("constraint_catalog", ("staging", "trade", "trade_pk")),
        ("index_column_catalog", ("staging", "trade", "trade_ix")),
    ])
    def test_a_named_database_qualifies_every_catalog_view(
        self, monkeypatch: pytest.MonkeyPatch, read: str, arguments: tuple,
    ) -> None:
        asked = self._capture(monkeypatch)

        getattr(sqlserver_utils, read)(object(), *arguments, database_name="NaviStage")

        assert asked
        for sql_text in asked:
            assert "[NaviStage].sys." in sql_text
            assert " sys." not in sql_text

    def test_no_name_reads_the_connected_database(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        asked = self._capture(monkeypatch)

        sqlserver_utils.table_column_catalog(object(), "staging", "trade")

        assert "FROM sys.columns" in asked[0] and "]." not in asked[0].split("WHERE")[0]

    def test_the_adapter_carries_the_name_to_the_provider(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        asked = self._capture(monkeypatch)
        conn = type("_Handle", (), {"provider": "sqlserver"})()

        DBAdapter().get_schema_metadata(conn, "staging", database_name="NaviStage")

        assert all("[NaviStage].sys." in sql_text for sql_text in asked)


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

    def table_column_catalog(
        self, _conn: Any, _schema: str, _table: str, **_k: Any,
    ) -> list[dict]:
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
            return [{"name": "a", "type": "integer", "default_value": None, "nullable": False,
                     "identity_kind": "", "identity_options_default": None}]

        monkeypatch.setattr(postgres_utils, "fetch_rows", _rows)

        assert postgres_utils.table_column_catalog(object(), "s", "t") == [
            {"name": "a", "type": "integer", "default": None, "nullable": False,
             "identity": None, "identity_options_default": None},
        ]
        assert "pg_attribute" in asked[0] and "format_type" in asked[0]

    def test_postgres_states_an_identity_column_and_whether_its_options_are_default(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An identity column has no default: attidentity says it is one (backlog 911)."""
        asked: list[str] = []

        def _rows(_conn: Any, sql_text: str, params: dict) -> list[dict]:
            asked.append(sql_text)
            return [
                {"name": "id", "type": "bigint", "default_value": None, "nullable": False,
                 "identity_kind": "a", "identity_options_default": True},
                {"name": "n", "type": "integer", "default_value": None, "nullable": False,
                 "identity_kind": "d", "identity_options_default": False},
            ]

        monkeypatch.setattr(postgres_utils, "fetch_rows", _rows)

        stated = postgres_utils.table_column_catalog(object(), "s", "t")

        assert [(c["identity"], c["identity_options_default"]) for c in stated] == [
            ("always", True), ("by_default", False),
        ]
        assert "a.attidentity" in asked[0] and "pg_sequence" in asked[0]
        assert "deptype = 'i'" in asked[0]

    def test_sql_server_states_each_type_as_it_declares_it(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        asked: list[str] = []
        monkeypatch.setattr(sqlserver_utils, "fetch_rows", lambda _c, sql_text, _p: asked.append(
            sql_text) or [
            {"name": "symbol", "type_name": "nvarchar", "max_length": 24, "precision": 0,
             "scale": 0, "is_nullable": True, "default_value": None},
        ])

        assert sqlserver_utils.table_column_catalog(object(), "s", "t") == [
            {"name": "symbol", "type": "nvarchar(12)", "default": None, "nullable": True},
        ]
        # A view's columns are answered as a table's are (backlog 726).
        assert "sys.objects" in asked[0] and "o.type IN ('U', 'V')" in asked[0]

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

        DatabaseDdlExport.export(conn, output_root=str(tmp_path))

        (written,) = [p for p in tmp_path.rglob("*") if p.is_file() and "trade" in p.name]
        assert written.read_text(encoding="utf-8") == "CREATE TABLE trade(symbol VARCHAR);\n"


class _Writer:
    """The write primitives, recording exactly what DatabaseTable passed."""

    def __init__(self, truncation: bool = False) -> None:
        self.asked: list[tuple] = []
        self._truncation = truncation

    def bulk_insert(self, conn: Any, schema: str, table: str, rows: list, columns: list) -> int:
        self.asked.append(("bulk_insert", conn, schema, table, rows, columns))
        return len(rows)

    def insert_from_path(
        self, conn: Any, schema: str, table: str, source_path: Any, columns: list,
    ) -> int:
        self.asked.append(("insert_from_path", conn, schema, table, source_path, columns))
        return 7

    def is_truncation_error(self, exc: Exception) -> bool:
        self.asked.append(("is_truncation_error", exc))
        return self._truncation


class TestInsertingRowsIsTheTablesBehaviour:
    """bulk_insert, insert_from_path and is_truncation_error (backlog 751).

    Each passes the primitive exactly what writes have always sent -- a
    catalog folded into the schema argument -- and none commits.
    """

    _IN_CATALOG = DatabaseObjectIdentity(connection="c", catalog="db", schema="s", name="t")

    def test_bulk_insert_passes_the_rows_through_and_does_not_commit(self) -> None:
        conn, writer = _Conn(), _Writer()
        rows = [{"a": 1}, {"a": 2}]

        inserted = DatabaseTable.bulk_insert(conn, self._IN_CATALOG, rows, ["a"], writer)

        assert inserted == 2
        assert writer.asked == [("bulk_insert", conn, "db.s", "t", rows, ["a"])]
        assert conn.commits == 0

    def test_insert_from_path_passes_the_path_through_and_does_not_commit(self) -> None:
        conn, writer = _Conn(), _Writer()

        inserted = DatabaseTable.insert_from_path(
            conn, _identity(""), "/in/file.csv", ["a", "b"], writer,
        )

        assert inserted == 7
        assert writer.asked == [
            ("insert_from_path", conn, "staging", "trade", "/in/file.csv", ["a", "b"]),
        ]
        assert conn.commits == 0

    @pytest.mark.parametrize("answer", [True, False])
    def test_can_insert_from_path_is_the_providers_capability_answer(
        self, answer: bool,
    ) -> None:
        asked: list[tuple] = []

        class _Capability:
            def supports_provider_capability(self, conn: Any, name: str) -> bool:
                asked.append((conn, name))
                return answer

        conn = _Conn()

        assert DatabaseTable.can_insert_from_path(conn, _Capability()) is answer
        assert asked == [(conn, "insert_from_path")]

    def test_a_real_provider_answers_the_capability(self) -> None:
        assert DatabaseTable.can_insert_from_path(duckdb.connect(":memory:")) is True

    @pytest.mark.parametrize("answer", [True, False])
    def test_is_truncation_error_is_the_providers_answer(self, answer: bool) -> None:
        exc = DatabaseError("String or binary data would be truncated")

        assert DatabaseTable.is_truncation_error(exc, _Writer(truncation=answer)) is answer

    def test_rows_land_in_a_real_table(self) -> None:
        conn = duckdb.connect(":memory:")
        conn.execute("CREATE SCHEMA s")
        conn.execute("CREATE TABLE s.t (a INTEGER, b VARCHAR)")
        identity = DatabaseObjectIdentity(connection="d", catalog="", schema="s", name="t")

        inserted = DatabaseTable.bulk_insert(
            conn, identity, [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}], ["a", "b"], DBAdapter(),
        )

        assert inserted == 2
        assert conn.execute("SELECT a, b FROM s.t ORDER BY a").fetchall() == [(1, "x"), (2, "y")]


class TestChildrenReadThemselvesFromTheirTable:
    """A key or index is read by its table's identity and its own name (backlog 1102)."""

    def test_a_primary_key_is_built_by_its_tables_read_and_carries_the_table(self) -> None:
        from rey_lib.db.database_objects import DatabasePrimaryKey

        key = DatabasePrimaryKey.read(
            _Conn(), _identity(), "trade_pk", _Connector("postgres", keyed=True),
        )

        assert (key.name, key.columns, key.table) == ("trade_pk", ("trade_id",), _identity())

    def test_an_index_is_built_by_its_tables_read_and_carries_the_table(self) -> None:
        from rey_lib.db.database_objects import DatabaseIndex

        index = DatabaseIndex.read(
            _Conn(), _identity(), "trade_ix", _Connector("postgres", keyed=True),
        )

        assert (index.name, index.columns, index.table) == ("trade_ix", ("symbol",), _identity())

    def test_a_child_the_table_does_not_hold_is_refused_by_name(self) -> None:
        from rey_lib.db.database_objects import DatabaseForeignKey

        with pytest.raises(
            DatabaseError, match="DatabaseForeignKey not found: staging.trade.trade_fk",
        ):
            DatabaseForeignKey.read(
                _Conn(), _identity(), "trade_fk", _Connector("postgres", keyed=True),
            )
