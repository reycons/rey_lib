"""Parity tests for the private SQLAlchemy-backed database primitives."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.db import mysql_utils, postgres_utils
from rey_lib.db.routine_call import InvocationShape, RoutineCall
from rey_lib.db._sqlalchemy import ReyConnection, inspect_schema
from rey_lib.db.db_adapter import DBAdapter
from rey_lib.errors.error_utils import DatabaseError


class _Engine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


class _Mappings:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class _Result:
    def __init__(
        self,
        *,
        columns: tuple[str, ...] = (),
        rows: list[tuple[Any, ...]] | None = None,
        mappings: list[dict[str, Any]] | None = None,
        rowcount: int = -1,
    ) -> None:
        self._columns = columns
        self._rows = rows or []
        self._mappings = mappings or []
        self.rowcount = rowcount

    def keys(self) -> tuple[str, ...]:
        return self._columns

    def fetchmany(self, limit: int) -> list[tuple[Any, ...]]:
        return self._rows[:limit]

    def first(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def mappings(self) -> _Mappings:
        return _Mappings(self._mappings)

    def scalar(self) -> Any:
        row = self.first()
        return row[0] if row else None


class _CoreConnection:
    def __init__(self, result: _Result | None = None) -> None:
        self.result = result or _Result()
        self.executed: list[tuple[Any, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.failure: Exception | None = None

    def execute(self, statement: Any, params: Any = None) -> _Result:
        self.executed.append((statement, params))
        if self.failure is not None:
            raise self.failure
        return self.result

    def exec_driver_sql(self, statement: str, params: Any = None) -> _Result:
        self.executed.append((statement, params))
        if self.failure is not None:
            raise self.failure
        return self.result

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def _connection(
    result: _Result | None = None, *, provider: str = "postgres"
) -> tuple[ReyConnection, _CoreConnection, _Engine]:
    core = _CoreConnection(result)
    engine = _Engine()
    return ReyConnection(provider, engine, core), core, engine


def test_rey_connection_preserves_lifecycle_without_exposing_sqlalchemy() -> None:
    conn, core, engine = _connection()

    conn.commit()
    conn.rollback()
    conn.close()
    conn.close()

    assert core.commits == 1
    assert core.rollbacks == 1
    assert core.closed is True
    assert engine.disposed is True
    assert not hasattr(conn, "execute")


def test_postgres_query_rows_preserves_public_result_shape() -> None:
    conn, core, _engine = _connection(
        _Result(columns=("id", "Display Name"), rows=[(1, "Alpha"), (2, "Beta")])
    )

    columns, rows = DBAdapter().query_rows(conn, "SELECT * FROM records", limit=1)

    assert columns == ["id", "Display Name"]
    assert rows == [{"id": 1, "Display Name": "Alpha"}]
    assert str(core.executed[0][0]) == "SELECT * FROM records"


@pytest.mark.parametrize(
    ("mode", "result", "expected"),
    [
        ("no_return", _Result(), None),
        ("scalar_result", _Result(rows=[(17,)]), 17),
        (
            "dataset_result",
            _Result(mappings=[{"Record Id": 17, "Status": "ready"}]),
            [{"Record Id": 17, "Status": "ready"}],
        ),
    ],
)
def test_postgres_named_sql_preserves_modes_and_commits(
    mode: str, result: _Result, expected: Any
) -> None:
    conn, core, _engine = _connection(result)

    actual = postgres_utils.execute_named_sql(
        conn, "SELECT :record_id", {"record_id": 17}, mode
    )

    assert actual == expected
    assert core.commits == 1
    assert core.rollbacks == 0
    assert core.executed[0][1] == {"record_id": 17}


def test_postgres_execution_failure_rolls_back_and_wraps_error() -> None:
    conn, core, _engine = _connection()
    core.failure = RuntimeError("boom")

    with pytest.raises(DatabaseError, match="run_sql failed: boom"):
        postgres_utils.run_sql(conn, "DELETE FROM records")

    assert core.commits == 0
    assert core.rollbacks == 1


def test_postgres_function_and_procedure_use_bound_core_execution() -> None:
    conn, core, _engine = _connection(_Result(rows=[(41,)]))

    scalar = postgres_utils.render_and_execute(
        conn,
        RoutineCall("control.start_run", InvocationShape.SCALAR_FUNCTION,
                    {"payload": {"id": 7}}),
    )
    postgres_utils.render_and_execute(
        conn,
        RoutineCall("control.finish_run", InvocationShape.PROCEDURE,
                    {"run_id": 41}),
    )

    assert scalar == 41
    # Arguments are stated by name: the procedure map binds parameter names, so
    # the rendered call names them and the binding's order is not a contract.
    assert [str(call[0]) for call in core.executed] == [
        "SELECT control.start_run(payload => :payload)",
        "CALL control.finish_run(run_id => :run_id)",
    ]
    assert core.executed[0][1] == {"payload": '{"id": 7}'}
    assert core.commits == 2
    assert core.rollbacks == 0


def test_current_database_queries_are_core_and_return_plain_strings() -> None:
    pg_conn, _pg_core, _pg_engine = _connection(_Result(rows=[("control",)]))
    mysql_conn, _mysql_core, _mysql_engine = _connection(
        _Result(rows=[("warehouse",)]), provider="mysql"
    )

    assert postgres_utils.get_current_database(pg_conn) == "control"
    assert mysql_utils.get_current_database(mysql_conn) == "warehouse"


def test_postgres_get_connection_preserves_config_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    expected = object()

    monkeypatch.setattr(postgres_utils, "_psycopg2", lambda: object())

    def open_connection(*args: Any, **kwargs: Any) -> object:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return expected

    import rey_lib.db._sqlalchemy as sqlalchemy_boundary

    monkeypatch.setattr(sqlalchemy_boundary, "open_connection", open_connection)
    cfg = SimpleNamespace(
        name="control",
        host="db.internal",
        port="5544",
        database="rey_control",
        username="rey",
        password="secret",
    )

    assert postgres_utils.get_connection(cfg) is expected
    assert captured == {
        "args": ("postgres", "postgresql+psycopg2"),
        "kwargs": {
            "host": "db.internal",
            "port": 5544,
            "database": "rey_control",
            "username": "rey",
            "password": "secret",
        },
    }


def test_mysql_query_rows_and_named_fetch_preserve_dict_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _Result(
        columns=("Record Id", "Status"),
        rows=[(8, "ready")],
        mappings=[{"Record Id": 8, "Status": "ready"}],
    )
    conn, core, _engine = _connection(result, provider="mysql")
    monkeypatch.setattr(mysql_utils, "load_sql", lambda _name: "SELECT %s AS value")

    columns, rows = DBAdapter().query_rows(conn, "SELECT * FROM records", limit=10)
    named_rows = mysql_utils.fetch_dicts(conn, "find_record", [8])

    assert columns == ["Record Id", "Status"]
    assert rows == [{"Record Id": 8, "Status": "ready"}]
    assert named_rows == [{"Record Id": 8, "Status": "ready"}]
    assert core.executed[1] == ("SELECT %s AS value", (8,))


def test_mysql_bulk_insert_preserves_normalization_and_caller_transaction() -> None:
    conn, core, _engine = _connection(provider="mysql")

    inserted = mysql_utils.bulk_insert(
        conn,
        "stage",
        "records",
        [{"RecordId": 1, "Description": ""}],
        ["RecordId", "Description"],
    )

    assert inserted == 1
    assert core.commits == 0
    parameter_rows = core.executed[0][1]
    assert parameter_rows == [{"RecordId": 1, "Description": None}]


def test_mysql_get_connection_preserves_driver_and_connect_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    expected = object()

    def open_connection(*args: Any, **kwargs: Any) -> object:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return expected

    import rey_lib.db._sqlalchemy as sqlalchemy_boundary

    monkeypatch.setattr(sqlalchemy_boundary, "open_connection", open_connection)
    cfg = SimpleNamespace(
        host="mysql.internal",
        port="3307",
        database="warehouse",
        user="loader",
        password="secret",
        timeout=12,
        allow_local_infile=True,
    )

    assert mysql_utils.get_connection(cfg) is expected
    assert captured == {
        "args": ("mysql", "mysql+mysqlconnector"),
        "kwargs": {
            "host": "mysql.internal",
            "port": 3307,
            "database": "warehouse",
            "username": "loader",
            "password": "secret",
            "connect_args": {
                "connection_timeout": 12,
                "autocommit": False,
                "allow_local_infile": True,
            },
        },
    }


def test_mysql_connection_retries_with_existing_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    delays: list[float] = []
    expected = object()

    def open_connection(*_args: Any, **_kwargs: Any) -> object:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("not ready")
        return expected

    import rey_lib.db._sqlalchemy as sqlalchemy_boundary

    monkeypatch.setattr(sqlalchemy_boundary, "open_connection", open_connection)
    monkeypatch.setattr(mysql_utils.time, "sleep", delays.append)
    cfg = SimpleNamespace(host="db", database="warehouse", user="loader")

    assert mysql_utils.get_connection(cfg) is expected
    assert attempts == 3
    assert delays == [1.0, 2.0]


def test_mysql_staging_commit_and_failure_rollback() -> None:
    conn, core, _engine = _connection(provider="mysql")

    assert mysql_utils.create_staging_table_if_not_exists(
        conn, "stage", "records", [("RecordId", "INTEGER")]
    ) is True
    assert core.commits == 1
    assert core.rollbacks == 0

    core.failure = RuntimeError("cannot create")
    with pytest.raises(DatabaseError, match="cannot create"):
        mysql_utils.create_staging_table_if_not_exists(
            conn, "stage", "other_records", [("RecordId", "INTEGER")]
        )
    assert core.rollbacks == 1


def test_mysql_query_error_is_normalized() -> None:
    conn, core, _engine = _connection(provider="mysql")
    core.failure = RuntimeError("bad query")

    with pytest.raises(DatabaseError, match="DBAdapter: query failed: bad query"):
        DBAdapter().query_rows(conn, "SELECT broken")


def test_shared_inspector_normalizes_schema_metadata() -> None:
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    core = engine.connect()
    core.exec_driver_sql("CREATE TABLE parent (id INTEGER PRIMARY KEY, code TEXT UNIQUE)")
    core.exec_driver_sql(
        "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL, "
        "FOREIGN KEY(parent_id) REFERENCES parent(id))"
    )
    core.exec_driver_sql("CREATE INDEX ix_child_parent ON child(parent_id)")
    core.exec_driver_sql("CREATE VIEW child_ids AS SELECT id FROM child")
    conn = ReyConnection("postgres", engine, core)
    try:
        metadata = inspect_schema(conn, "main")
    finally:
        conn.close()

    assert "main" in metadata["schemas"]
    child = next(table for table in metadata["tables"] if table["name"] == "child")
    assert [column["name"] for column in child["columns"]] == ["id", "parent_id"]
    assert child["columns"][1]["nullable"] is False
    assert child["primary_key"]["constrained_columns"] == ["id"]
    assert child["foreign_keys"][0]["referred_table"] == "parent"
    assert child["indexes"][0]["name"] == "ix_child_parent"
    parent = next(table for table in metadata["tables"] if table["name"] == "parent")
    assert parent["unique_constraints"][0]["column_names"] == ["code"]
    assert metadata["views"] == [
        {"name": "child_ids", "definition": "CREATE VIEW child_ids AS SELECT id FROM child"}
    ]
    assert all(not hasattr(value, "compile") for value in child["columns"])


def test_mysql_inventory_uses_inspector_and_keeps_routine_trigger_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cursor:
        def __init__(self) -> None:
            self.sql = ""

        def execute(self, sql: str, _params: Any = None) -> None:
            self.sql = sql

        def fetchall(self) -> list[dict[str, Any]]:
            if "information_schema.routines" in self.sql:
                return [
                    {
                        "object_type": "procedure",
                        "schema_name": "warehouse",
                        "object_name": "refresh_records",
                    },
                    {
                        "object_type": "trigger",
                        "schema_name": "warehouse",
                        "object_name": "records_audit",
                    },
                ]
            return []

        def close(self) -> None:
            pass

    class DriverConnection:
        def cursor(self, **_kwargs: Any) -> Cursor:
            return Cursor()

    conn, core, _engine = _connection(provider="mysql")
    core.connection = SimpleNamespace(driver_connection=DriverConnection())
    metadata = {
        "schemas": ["warehouse"],
        "tables": [
            {
                "name": "records",
                "columns": [],
                "primary_key": {"name": "PRIMARY", "constrained_columns": ["id"]},
                "foreign_keys": [{"name": "fk_records_parent"}],
                "indexes": [{"name": "ix_records_status"}],
                "unique_constraints": [{"name": "uq_records_code"}],
            }
        ],
        "views": [{"name": "current_records", "definition": "SELECT 1"}],
    }
    import rey_lib.db._sqlalchemy as sqlalchemy_boundary

    monkeypatch.setattr(sqlalchemy_boundary, "inspect_schema", lambda _conn, _schema: metadata)

    objects = mysql_utils.list_database_objects(conn, "warehouse")

    identities = {
        (item["object_type"], item["schema"], item["name"])
        for item in objects
    }
    assert identities == {
        ("schema", "warehouse", "warehouse"),
        ("table", "warehouse", "records"),
        ("view", "warehouse", "current_records"),
        ("index", "warehouse", "ix_records_status"),
        ("constraint", "warehouse", "PRIMARY"),
        ("constraint", "warehouse", "fk_records_parent"),
        ("constraint", "warehouse", "uq_records_code"),
        ("procedure", "warehouse", "refresh_records"),
        ("trigger", "warehouse", "records_audit"),
    }
    supporting = next(item for item in objects if item["name"] == "ix_records_status")
    assert supporting["dependencies"] == [
        {"object_type": "table", "schema": "warehouse", "name": "records"}
    ]
