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
