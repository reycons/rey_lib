"""DuckDB's inventory lists one catalog, once (backlog 700).

Against a real in-memory DuckDB, through the provider and the real export. A
DuckDB connection always sees several catalogs -- the database itself,
``system``, ``temp`` and anything attached -- and the inventory answered for
all of them, so ``main`` was listed once per catalog and the export refused
the duplicates.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import duckdb
import pytest

from rey_lib.db import duckdb_utils
from rey_lib.db.db_adapter import DBAdapter
from rey_lib.db.ddl_export import DatabaseDdlExport


@pytest.fixture()
def duck() -> duckdb.DuckDBPyConnection:
    """One database with a schema, table, view and sequence; another attached."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA s")
    conn.execute("CREATE TABLE s.t (a INTEGER)")
    conn.execute("CREATE VIEW s.v AS SELECT a FROM s.t")
    conn.execute("CREATE SEQUENCE s.q")
    conn.execute("ATTACH ':memory:' AS other")
    conn.execute("CREATE SCHEMA other.s")
    conn.execute("CREATE TABLE other.s.t (b INTEGER)")
    conn.execute("CREATE TABLE other.main.elsewhere (c INTEGER)")
    conn.execute("CREATE SEQUENCE other.s.q")
    return conn


def _listed(objects: list[dict]) -> list[tuple[str, str, str]]:
    return [(one["object_type"], one["schema"], one["name"]) for one in objects]


def test_each_object_of_the_current_catalog_is_listed_once(duck) -> None:
    listed = _listed(duckdb_utils.list_database_objects(duck))

    assert sorted(listed) == [
        ("schema", "main", "main"),
        ("schema", "s", "s"),
        ("sequence", "s", "q"),
        ("table", "s", "t"),
        ("view", "s", "v"),
    ]
    assert not [key for key, count in Counter(listed).items() if count > 1]


def test_an_attached_database_does_not_answer_for_the_current_one(duck) -> None:
    objects = duckdb_utils.list_database_objects(duck)

    assert "elsewhere" not in {one["name"] for one in objects}
    assert {one["database"] for one in objects} == {"memory"}


def test_the_database_asked_for_is_the_one_listed(duck) -> None:
    listed = _listed(duckdb_utils.list_database_objects(duck, "other"))

    assert sorted(listed) == [
        ("schema", "main", "main"),
        ("schema", "s", "s"),
        ("sequence", "s", "q"),
        ("table", "main", "elsewhere"),
        ("table", "s", "t"),
    ]


def test_the_export_validates_with_no_duplicates(duck) -> None:
    """The original symptom: duplicate object definitions schemas:main.main."""
    found = DatabaseDdlExport.validate(duck, connection="rey", adapter=DBAdapter())

    assert found["errors"] == [] and found["duplicates"] == []


def test_a_full_export_succeeds(duck, tmp_path: Path) -> None:
    result = DatabaseDdlExport.export(duck, connection="rey", output_root=str(tmp_path), adapter=DBAdapter())

    assert sorted(result["objects"]) == [
        "schemas:main.main", "schemas:s.s", "sequences:s.q", "tables:s.t", "views:s.v",
    ]
    written = Path(result["build_manifest"]["paths"]["build_database_sql"])
    assert written.is_file() and "CREATE TABLE" in written.read_text(encoding="utf-8")
