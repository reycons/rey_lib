"""Governed SQL-file transformation primitives in rey_lib.db.duckdb_utils.

A caller opens a private in-memory connection, registers exactly one source
file as a logical relation, loads a governed .sql file, and reads back the
query's columns and rows. These tests state what each of those steps
guarantees — including the guarantees that are refusals.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import duckdb
import pytest

from rey_lib.db.duckdb_utils import (
    CSV_SOURCE_RELATION,
    TEXT_LINE_COLUMN,
    TEXT_LINE_SOURCE_RELATION,
    fetch_sql_rows,
    load_sql_file,
    open_memory_connection,
    register_csv_source,
    register_text_line_source,
)
from rey_lib.errors.error_utils import ConfigError, DatabaseError


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def test_memory_connection_opens_and_queries() -> None:
    """The connection works and is in memory, not a file on disk."""
    with open_memory_connection() as conn:
        assert conn.execute("SELECT 42").fetchall() == [(42,)]
        databases = conn.execute("PRAGMA database_list").fetchall()
    assert all(not row[2] for row in databases)


def test_memory_connection_closes_on_success() -> None:
    """Leaving the block closes the connection."""
    with open_memory_connection() as conn:
        pass
    with pytest.raises(duckdb.Error):
        conn.execute("SELECT 1")


def test_memory_connection_closes_on_failure() -> None:
    """A raising body still closes the connection."""
    with pytest.raises(RuntimeError), open_memory_connection() as conn:
        raise RuntimeError("caller failed")
    with pytest.raises(duckdb.Error):
        conn.execute("SELECT 1")


def test_memory_connections_are_isolated() -> None:
    """Two connections do not share state, so neither is module-global."""
    with open_memory_connection() as first:
        first.execute("CREATE TABLE only_here (a INTEGER)")
        with open_memory_connection() as second, pytest.raises(duckdb.Error):
            second.execute("SELECT * FROM only_here")


def test_no_extension_install_or_autoload() -> None:
    """Ordinary execution never reaches the network for an extension."""
    with open_memory_connection() as conn:
        settings = conn.execute(
            "SELECT current_setting('autoinstall_known_extensions'), "
            "current_setting('autoload_known_extensions')"
        ).fetchall()
    assert settings == [(False, False)]


# ---------------------------------------------------------------------------
# CSV registration
# ---------------------------------------------------------------------------


def test_csv_first_row_is_the_header(tmp_path: Path) -> None:
    """Row one names the columns; it is not data."""
    source = _write(tmp_path, "a.csv", "Acct,Name\nA1,Alice\nA2,Bob\n")
    with open_memory_connection() as conn:
        register_csv_source(conn, source)
        result = fetch_sql_rows(conn, f"SELECT * FROM {CSV_SOURCE_RELATION}")
    assert result.columns == ["Acct", "Name"]
    assert result.rows == [("A1", "Alice"), ("A2", "Bob")]


def test_csv_header_is_not_independently_discovered(tmp_path: Path) -> None:
    """A preamble above row one is not skipped in search of a better header.

    Header discovery belongs upstream and has exactly one owner. Given a file
    whose real header sits on line 3, the registration still takes line 1 —
    proving DuckDB was told where the header is rather than left to look.
    """
    source = _write(
        tmp_path,
        "preamble.csv",
        "HOLDINGS REPORT\n\nAcct,Name\nA1,Alice\n",
    )
    with open_memory_connection() as conn:
        register_csv_source(conn, source)
        result = fetch_sql_rows(conn, f"SELECT * FROM {CSV_SOURCE_RELATION}")
    # Line 1 named the columns. "Acct" and "Name" are values in the result,
    # never column names, because nothing went looking for a better header.
    assert result.columns[0] == "HOLDINGS REPORT"
    assert "Acct" not in result.columns
    assert ("Acct", "Name") in result.rows


def test_csv_columns_are_all_varchar(tmp_path: Path) -> None:
    """Nothing is type-inferred; transformation SQL casts deliberately."""
    source = _write(
        tmp_path,
        "typed.csv",
        "Amount,Opened,Flag\n10,2026-01-31,true\n",
    )
    with open_memory_connection() as conn:
        register_csv_source(conn, source)
        types = conn.execute(
            f"DESCRIBE {CSV_SOURCE_RELATION}"
        ).fetchall()
        result = fetch_sql_rows(conn, f"SELECT * FROM {CSV_SOURCE_RELATION}")
    assert {row[1] for row in types} == {"VARCHAR"}
    assert result.rows == [("10", "2026-01-31", "true")]


def test_csv_short_rows_are_null_padded(tmp_path: Path) -> None:
    """A row missing trailing fields is queryable, not rejected."""
    source = _write(
        tmp_path,
        "ragged.csv",
        "Acct,Name,Amount\nA1,Alice,10\nA2,Bob\n",
    )
    with open_memory_connection() as conn:
        register_csv_source(conn, source)
        result = fetch_sql_rows(conn, f"SELECT * FROM {CSV_SOURCE_RELATION}")
    assert result.rows == [("A1", "Alice", "10"), ("A2", "Bob", None)]


def test_csv_delimiter_is_honoured(tmp_path: Path) -> None:
    """A caller-supplied separator overrides the default."""
    source = _write(tmp_path, "pipe.csv", "Acct|Name\nA1|Alice\n")
    with open_memory_connection() as conn:
        register_csv_source(conn, source, delimiter="|")
        result = fetch_sql_rows(conn, f"SELECT * FROM {CSV_SOURCE_RELATION}")
    assert result.columns == ["Acct", "Name"]
    assert result.rows == [("A1", "Alice")]


def test_csv_relation_name_is_configurable(tmp_path: Path) -> None:
    """The default relation is ``source``; a caller may name another."""
    source = _write(tmp_path, "a.csv", "Acct\nA1\n")
    assert CSV_SOURCE_RELATION == "source"
    with open_memory_connection() as conn:
        register_csv_source(conn, source, relation_name="staged")
        assert fetch_sql_rows(conn, "SELECT * FROM staged").rows == [("A1",)]


def test_csv_missing_file_fails(tmp_path: Path) -> None:
    """An unreadable source fails at registration, not mid-query."""
    with open_memory_connection() as conn, pytest.raises(DatabaseError):
        register_csv_source(conn, tmp_path / "absent.csv")


def test_csv_source_file_is_unchanged(tmp_path: Path) -> None:
    """Registering and querying never writes to the source."""
    source = _write(
        tmp_path,
        "immutable.csv",
        "Acct,Name\nA1,Alice\nA2,Bob\n",
    )
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    with open_memory_connection() as conn:
        register_csv_source(conn, source)
        fetch_sql_rows(
            conn,
            f"SELECT Acct FROM {CSV_SOURCE_RELATION} WHERE Name <> 'Bob'",
        )
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


# ---------------------------------------------------------------------------
# Fixed-width registration
# ---------------------------------------------------------------------------


def test_fixed_width_registers_one_line_per_row(tmp_path: Path) -> None:
    """Each physical line arrives whole, in a single named column."""
    source = _write(
        tmp_path,
        "fixed.txt",
        "A1   Alice     000100\nA2   Bob       000200\n",
    )
    assert TEXT_LINE_SOURCE_RELATION == "source_lines"
    assert TEXT_LINE_COLUMN == "line"
    with open_memory_connection() as conn:
        register_text_line_source(conn, source)
        result = fetch_sql_rows(
            conn,
            f"SELECT {TEXT_LINE_COLUMN} FROM {TEXT_LINE_SOURCE_RELATION}",
        )
    assert result.columns == ["line"]
    assert result.rows == [
        ("A1   Alice     000100",),
        ("A2   Bob       000200",),
    ]


def test_fixed_width_does_not_interpret_separators_or_quotes(
    tmp_path: Path,
) -> None:
    """Commas and quotes inside a line are content, not structure."""
    source = _write(
        tmp_path,
        "awkward.txt",
        'A1   Bob,Jr    000100\nA2   "Quoted"  000200\n',
    )
    with open_memory_connection() as conn:
        register_text_line_source(conn, source)
        result = fetch_sql_rows(
            conn,
            f"SELECT {TEXT_LINE_COLUMN} FROM {TEXT_LINE_SOURCE_RELATION}",
        )
    assert result.rows == [
        ("A1   Bob,Jr    000100",),
        ('A2   "Quoted"  000200',),
    ]


def test_fixed_width_columns_come_from_sql(tmp_path: Path) -> None:
    """No schema is inferred; the SQL file defines the columns."""
    source = _write(
        tmp_path,
        "fixed.txt",
        "A1   Alice     000100\nA2   Bob       000200\n",
    )
    with open_memory_connection() as conn:
        register_text_line_source(conn, source)
        result = fetch_sql_rows(
            conn,
            "SELECT trim(substr(line, 1, 5)) AS acct, "
            "try_cast(trim(substr(line, 16, 6)) AS INTEGER) AS amount "
            f"FROM {TEXT_LINE_SOURCE_RELATION}",
        )
    assert result.columns == ["acct", "amount"]
    assert result.rows == [("A1", 100), ("A2", 200)]


def test_fixed_width_first_line_is_data(tmp_path: Path) -> None:
    """A text-line relation has no header row of its own."""
    source = _write(tmp_path, "fixed.txt", "AAAA\nBBBB\n")
    with open_memory_connection() as conn:
        register_text_line_source(conn, source)
        result = fetch_sql_rows(
            conn, f"SELECT * FROM {TEXT_LINE_SOURCE_RELATION}"
        )
    assert result.rows == [("AAAA",), ("BBBB",)]


# ---------------------------------------------------------------------------
# SQL file loading
# ---------------------------------------------------------------------------


def test_sql_file_loads_unchanged(tmp_path: Path) -> None:
    """The file's text is returned exactly as written."""
    text = "-- keep this comment\nSELECT *\nFROM source\nWHERE acct <> '';\n"
    path = _write(tmp_path, "transform.sql", text)
    assert load_sql_file(path) == text


def test_sql_file_missing_fails(tmp_path: Path) -> None:
    """A missing SQL file fails explicitly."""
    with pytest.raises(ConfigError, match="not found"):
        load_sql_file(tmp_path / "absent.sql")


def test_sql_file_empty_fails(tmp_path: Path) -> None:
    """An empty SQL file fails explicitly rather than running nothing."""
    path = _write(tmp_path, "empty.sql", "")
    with pytest.raises(ConfigError, match="empty"):
        load_sql_file(path)


def test_sql_file_whitespace_only_fails(tmp_path: Path) -> None:
    """Whitespace is not SQL."""
    path = _write(tmp_path, "blank.sql", "\n\n   \n")
    with pytest.raises(ConfigError, match="empty"):
        load_sql_file(path)


def test_sql_file_requires_sql_extension(tmp_path: Path) -> None:
    """Only a .sql file may supply transformation SQL."""
    path = _write(tmp_path, "transform.txt", "SELECT 1;")
    with pytest.raises(ConfigError, match=".sql extension"):
        load_sql_file(path)


def test_sql_file_directory_fails(tmp_path: Path) -> None:
    """A directory named like a SQL file is not a SQL file."""
    directory = tmp_path / "transform.sql"
    directory.mkdir()
    with pytest.raises(ConfigError, match="not found"):
        load_sql_file(directory)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def test_execution_returns_query_column_order_and_aliases(
    tmp_path: Path,
) -> None:
    """The SELECT list defines the result's columns and their order."""
    source = _write(
        tmp_path,
        "a.csv",
        "Acct,Name,Amount\nA1,Alice,10\nA2,Bob,20\n",
    )
    with open_memory_connection() as conn:
        register_csv_source(conn, source)
        result = fetch_sql_rows(
            conn,
            "SELECT Name AS holder, Acct AS account "
            f"FROM {CSV_SOURCE_RELATION} ORDER BY Acct",
        )
    assert result.columns == ["holder", "account"]
    assert result.rows == [("Alice", "A1"), ("Bob", "A2")]


def test_execution_filters_report_furniture(tmp_path: Path) -> None:
    """A WHERE clause removes banner rows and keeps real records."""
    source = _write(
        tmp_path,
        "furniture.csv",
        "Acct,Name\nHOLDINGS,\nA1,Alice\nTOTAL,\nA2,Bob\n",
    )
    with open_memory_connection() as conn:
        register_csv_source(conn, source)
        result = fetch_sql_rows(
            conn,
            f"SELECT Acct, Name FROM {CSV_SOURCE_RELATION} "
            "WHERE Name IS NOT NULL AND Name <> ''",
        )
    assert result.rows == [("A1", "Alice"), ("A2", "Bob")]


def test_execution_returns_no_rows_with_columns(tmp_path: Path) -> None:
    """An empty result still reports the columns the query declared."""
    source = _write(tmp_path, "a.csv", "Acct,Name\nA1,Alice\n")
    with open_memory_connection() as conn:
        register_csv_source(conn, source)
        result = fetch_sql_rows(
            conn,
            f"SELECT Acct FROM {CSV_SOURCE_RELATION} WHERE Acct = 'missing'",
        )
    assert result.columns == ["Acct"]
    assert result.rows == []


def test_execution_invalid_sql_fails(tmp_path: Path) -> None:
    """DuckDB's complaint surfaces through the database error boundary."""
    source = _write(tmp_path, "a.csv", "Acct\nA1\n")
    with open_memory_connection() as conn:
        register_csv_source(conn, source)
        with pytest.raises(DatabaseError, match="SQL execution failed"):
            fetch_sql_rows(conn, "SELECT nonexistent FROM source")


def test_execution_unknown_relation_fails() -> None:
    """SQL cannot query a source that was never registered."""
    with open_memory_connection() as conn, pytest.raises(
        DatabaseError, match="SQL execution failed"
    ):
        fetch_sql_rows(conn, f"SELECT * FROM {CSV_SOURCE_RELATION}")


def test_loaded_sql_file_executes_against_source(tmp_path: Path) -> None:
    """The governed file's SQL runs unchanged against the relation."""
    source = _write(
        tmp_path,
        "a.csv",
        "Acct,Name,Amount\nA1,Alice,10\nA2,Bob,20\n",
    )
    sql_file = _write(
        tmp_path,
        "transform.sql",
        "SELECT Acct AS account, try_cast(Amount AS INTEGER) AS amount\n"
        f"FROM {CSV_SOURCE_RELATION}\n"
        "WHERE Name <> 'Bob'\n",
    )
    with open_memory_connection() as conn:
        register_csv_source(conn, source)
        result = fetch_sql_rows(conn, load_sql_file(sql_file))
    assert result.columns == ["account", "amount"]
    assert result.rows == [("A1", 10)]


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


def test_csv_module_owns_no_duckdb_or_sql_execution() -> None:
    """Format mechanics stay in csv.py; query execution stays in the db layer."""
    csv_source = (
        Path(__file__).resolve().parents[1]
        / "rey_lib"
        / "files"
        / "csv.py"
    ).read_text(encoding="utf-8")
    assert "duckdb" not in csv_source.lower()
    assert "read_csv_auto" not in csv_source


# ---------------------------------------------------------------------------
# Connections opened through the shared dispatcher
# ---------------------------------------------------------------------------


def test_a_memory_connection_opens_from_its_configuration(tmp_path: Path) -> None:
    """A caller selects a connection by name; the provider reads the config.

    Every backend is reached through the one adapter, so DuckDB has to open
    from a connection config like the others rather than from module state a
    caller had to initialise first.
    """
    from types import SimpleNamespace

    from rey_lib.db.duckdb_utils import MEMORY_DATABASE, get_connection

    conn = get_connection(SimpleNamespace(provider="duckdb", database=MEMORY_DATABASE))
    try:
        assert conn.execute("SELECT 42").fetchall() == [(42,)]
        # In memory: nothing was written anywhere.
        assert all(not row[2] for row in conn.execute("PRAGMA database_list").fetchall())
    finally:
        conn.close()


def test_a_file_connection_opens_the_configured_path(tmp_path: Path) -> None:
    """A path in the configuration is the database, created on first use."""
    from types import SimpleNamespace

    from rey_lib.db.duckdb_utils import get_connection

    target = tmp_path / "nested" / "warehouse.duckdb"
    conn = get_connection(SimpleNamespace(provider="duckdb", path=str(target)))
    try:
        conn.execute("CREATE TABLE kept (a INTEGER)")
    finally:
        conn.close()
    assert target.exists()


def test_the_shared_adapter_dispatches_to_duckdb() -> None:
    """The caller names a connection; nothing above the adapter names a driver."""
    from types import SimpleNamespace

    from rey_lib.db.db_adapter import DBAdapter
    from rey_lib.db.duckdb_utils import MEMORY_DATABASE

    adapter = DBAdapter()
    conn = adapter.get_connection(
        SimpleNamespace(provider="duckdb", database=MEMORY_DATABASE)
    )
    try:
        columns, rows = adapter.query_rows(conn, "SELECT 1 AS answer")
        assert columns == ["answer"]
        assert rows == [{"answer": 1}]
    finally:
        conn.close()


def test_module_state_still_serves_a_caller_that_initialised_it(
    tmp_path: Path,
) -> None:
    """init_db callers are unaffected: with no config, module state is used."""
    from rey_lib.db.duckdb_utils import get_connection, init_db

    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    init_db(tmp_path / "app.duckdb", sql_dir)
    conn = get_connection()
    try:
        assert conn.execute("SELECT 1").fetchall() == [(1,)]
    finally:
        conn.close()
    assert (tmp_path / "app.duckdb").exists()


# ---------------------------------------------------------------------------
# The default query for a file
# ---------------------------------------------------------------------------


def test_each_format_gets_the_reader_duckdb_needs_for_it() -> None:
    """One helper knows which reader a file's extension calls for."""
    from rey_lib.db.duckdb_utils import default_file_select_sql

    assert default_file_select_sql("/data/holdings.csv") == (
        "SELECT * FROM read_csv('/data/holdings.csv', header = true, "
        "all_varchar = true, null_padding = true)"
    )
    assert default_file_select_sql("/data/records.json") == (
        "SELECT * FROM read_json_auto('/data/records.json')"
    )
    assert default_file_select_sql("/data/shapes.parquet") == (
        "SELECT * FROM read_parquet('/data/shapes.parquet')"
    )


def test_newline_delimited_json_is_read_as_such() -> None:
    """A record-per-line file is not one JSON document, and says so."""
    from rey_lib.db.duckdb_utils import default_file_select_sql

    for name in ("run.jsonl", "run.ndjson"):
        sql = default_file_select_sql(f"/data/{name}")
        assert "read_json_auto(" in sql
        assert "format = 'newline_delimited'" in sql


def test_the_extension_is_matched_whatever_its_case() -> None:
    """A file named in capitals is the same format as one in lower case."""
    from rey_lib.db.duckdb_utils import default_file_select_sql

    assert "read_parquet(" in default_file_select_sql("/data/SHAPES.PARQUET")
    assert "read_csv(" in default_file_select_sql("/data/Holdings.Csv")


def test_a_path_with_spaces_survives_intact() -> None:
    """A path is a value, not something to be reformatted."""
    from rey_lib.db.duckdb_utils import default_file_select_sql

    sql = default_file_select_sql("/Users/joe/Rey Apps/data/May 26 holdings.csv")
    assert "'/Users/joe/Rey Apps/data/May 26 holdings.csv'" in sql


def test_a_quote_in_the_path_cannot_end_the_literal() -> None:
    """A single quote is doubled, so a path can never become SQL."""
    from rey_lib.db.duckdb_utils import default_file_select_sql

    sql = default_file_select_sql("/data/O'Brien/report'.csv")
    assert "'/data/O''Brien/report''.csv'" in sql
    # The statement still has exactly one opening and one closing quote pair
    # around the literal: nothing escaped out of it.
    assert sql.count("'") % 2 == 0


def test_an_unreadable_format_fails_clearly() -> None:
    """A legacy workbook is refused with what would work instead."""
    from rey_lib.db.duckdb_utils import default_file_select_sql

    with pytest.raises(ConfigError, match=r"\.xls is not a supported file format"):
        default_file_select_sql("/data/legacy.xls")
    # Spreadsheets are out of scope for now: a workbook is converted to CSV
    # upstream, and DuckDB's excel extension is not loaded by these connections.
    with pytest.raises(ConfigError, match=r"\.xlsx is not a supported file format"):
        default_file_select_sql("/data/book.xlsx")
    with pytest.raises(ConfigError, match="not a supported file format"):
        default_file_select_sql("/data/notes")


def test_no_path_is_refused() -> None:
    """An empty path produces no statement rather than a broken one."""
    from rey_lib.db.duckdb_utils import default_file_select_sql

    for empty in ("", "   ", None):
        with pytest.raises(ConfigError, match="file path is required"):
            default_file_select_sql(empty)  # type: ignore[arg-type]


def test_the_default_query_actually_runs(tmp_path: Path) -> None:
    """Proof against DuckDB, not just against the string it produced."""
    from rey_lib.db.duckdb_utils import default_file_select_sql

    csv = _write(tmp_path, "holdings.csv", "Acct,Name\nA1,Alice\nA2,Bob\n")
    jsonl = _write(tmp_path, "run.jsonl", '{"a": 1}\n{"a": 2}\n')

    with open_memory_connection() as conn:
        assert fetch_sql_rows(conn, default_file_select_sql(csv)).rows == [
            ("A1", "Alice"), ("A2", "Bob"),
        ]
        assert fetch_sql_rows(conn, default_file_select_sql(jsonl)).rows == [
            (1,), (2,),
        ]


def test_a_quoted_path_still_runs(tmp_path: Path) -> None:
    """The escaping holds against a real file, not only in the string."""
    from rey_lib.db.duckdb_utils import default_file_select_sql

    awkward = _write(tmp_path, "O'Brien holdings.csv", "Acct\nA1\n")
    with open_memory_connection() as conn:
        assert fetch_sql_rows(conn, default_file_select_sql(awkward)).rows == [("A1",)]
