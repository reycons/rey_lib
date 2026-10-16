"""ResolvedQuery: what SQL reads one subject (backlog 733).

Asserted: each constructor's exact statement, filters and attributes; the
literal rendering of a declared value; that a database identity is read as its
one table; the refusals; and that this owner never imports the file manifest.

The templates are neutral text with the real placeholders: substitution is what
is under test, and it does not read the statement around them.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.db import resolved_query as module
from rey_lib.db.database_objects import DatabaseObjectIdentity
from rey_lib.db.resolved_query import ResolvedQuery
from rey_lib.errors.error_utils import ConfigError

_RUN_TEMPLATE = "run view [:run_id] [:run_log_id]"
_FILE_TEMPLATE = "file view [:feed] [:file_manifest_id]"


@pytest.fixture()
def bindings(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """The rey_apps map, answering run_view and file_view; each lookup recorded."""
    asked: list[tuple[str, str]] = []

    def resolve(_cfg: Any, map_name: str, binding_name: str) -> dict[str, Any]:
        asked.append((map_name, binding_name))
        return {"sql": {"run_view": _RUN_TEMPLATE, "file_view": _FILE_TEMPLATE}[binding_name]}

    monkeypatch.setattr(module, "get_procedure_map", lambda _ctx, map_name: {"map": map_name})
    monkeypatch.setattr(module, "resolve_sql_binding", resolve)
    return asked


class TestConfiguredStatements:

    def test_a_run_reads_through_run_view_filtered_to_it(self, bindings) -> None:
        resolved = ResolvedQuery.for_run(SimpleNamespace(), "41")

        assert bindings == [("rey_apps", "run_view")]
        assert resolved.statement == "run view [41] [NULL]"
        assert (resolved.display_name, resolved.connection_map) == ("Run 41", "rey_apps")

    def test_a_run_not_named_by_a_whole_number_is_refused(self, bindings) -> None:
        with pytest.raises(ConfigError, match="named by its identity"):
            ResolvedQuery.for_run(SimpleNamespace(), "x")

    def test_a_feed_reads_through_file_view_its_name_quoted(self, bindings) -> None:
        resolved = ResolvedQuery.for_feed(SimpleNamespace(), " o'brien ")

        assert bindings == [("rey_apps", "file_view")]
        assert resolved.statement == "file view ['o''brien'] [NULL]"
        assert resolved.display_name == "o'brien"

    def test_a_blank_feed_is_refused(self, bindings) -> None:
        with pytest.raises(ConfigError, match="feed is named"):
            ResolvedQuery.for_feed(SimpleNamespace(), "  ")


class TestADeclaredStatement:

    @pytest.mark.parametrize(("value", "literal"), [
        (7, "7"), (2.5, "2.5"), (None, "NULL"), (True, "TRUE"), (False, "FALSE"),
        ("it's", "'it''s'"), ("1; anything", "'1; anything'"),
    ])
    def test_the_value_is_rendered_then_written_in(self, value: Any, literal: str) -> None:
        resolved = ResolvedQuery.for_declared("the rows for [:current_value]", value)

        assert resolved.statement == f"the rows for [{literal}]"
        assert resolved.connection_map == "rey_apps"

    def test_a_declaration_with_no_statement_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="declared none"):
            ResolvedQuery.for_declared("  ", 1)


class TestADatabaseObject:

    def test_it_is_returned_as_it_travelled(self) -> None:
        resolved = ResolvedQuery.for_database_object(
            " orders ", "the orders statement", "the orders definition", " w ", " s ",
        )

        assert (resolved.name, resolved.schema_name, resolved.connection_map) == (
            "orders", "s", "w",
        )
        assert (resolved.statement, resolved.ddl) == (
            "the orders statement", "the orders definition",
        )

    def test_a_definition_alone_still_opens(self) -> None:
        assert ResolvedQuery.for_database_object("f", "", "the f definition", "w").ddl

    @pytest.mark.parametrize(("name", "statement", "ddl"), [("", "x", ""), ("t", "", "")])
    def test_nothing_to_open_is_refused(self, name: str, statement: str, ddl: str) -> None:
        with pytest.raises(ConfigError):
            ResolvedQuery.for_database_object(name, statement, ddl, "w")


class TestADatabaseIdentity:

    def test_it_reads_the_one_table_the_identity_names(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        identity = DatabaseObjectIdentity("warehouse", "", "trading", "trades")
        opened: list[str] = []
        read: list[Any] = []

        def connect(_ctx: Any, name: str) -> Any:
            opened.append(name)
            return SimpleNamespace(handle=lambda: "handle")

        def read_table(conn: Any, asked: DatabaseObjectIdentity) -> Any:
            read.append((conn, asked))
            return SimpleNamespace(initial_select="the trades statement",
                                   ddl="the trades definition")

        monkeypatch.setattr(module, "shared_connection", connect)
        monkeypatch.setattr(module.DatabaseTable, "read", staticmethod(read_table))

        resolved = ResolvedQuery.for_database_identity(SimpleNamespace(), identity)

        assert opened == ["warehouse"]
        assert read == [("handle", identity)]
        assert (resolved.statement, resolved.ddl) == (
            "the trades statement", "the trades definition",
        )
        assert (resolved.name, resolved.schema_name, resolved.connection_map) == (
            "trades", "trading", "warehouse",
        )


def test_it_never_imports_the_file_manifest() -> None:
    """Where a mutation went is the manifest's question, not this owner's."""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = [
        node.module for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    ] + [
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names
    ]

    assert not [name for name in imported if name.startswith("rey_lib.files")]


def _installation(*connections: tuple[str, str]) -> SimpleNamespace:
    """An installation whose configured connections are these (name, provider) pairs."""
    return SimpleNamespace(connections=[
        SimpleNamespace(name=name, provider=provider) for name, provider in connections
    ])


class TestHeldForASurface:
    """Held on a page runtime and shown by a Data Workbench (backlogs 981, 982)."""

    def test_the_projection_is_what_it_was_resolved_to(self) -> None:
        resolved = ResolvedQuery.for_database_object(
            "orders", "the orders statement", "the orders definition", "w", "s",
        )

        assert resolved.projection(_installation(("w", "postgres"))) == {
            "display_name": resolved.display_name,
            "statement": "the orders statement",
            "ddl": "the orders definition",
            "readings": ["query", "ddl"],
            "name": "orders",
            "schema_name": "s",
            "connections": ["w"],
        }

    def test_only_the_readings_present_are_offered(self) -> None:
        resolved = ResolvedQuery.for_database_object("f", "", "the f definition", "w")

        assert resolved.projection(_installation(("w", "postgres")))["readings"] == ["ddl"]

    def test_its_connection_leads_the_configured_database_connections(self) -> None:
        installation = _installation(
            ("a", "postgres"), ("files", "duckdb"), ("w", "postgres"), ("api", "http"),
        )
        resolved = ResolvedQuery.for_declared("the rows for [:current_value]", 1)
        resolved = ResolvedQuery(
            resolved.display_name, resolved.statement, connection_map="w",
        )

        assert resolved.projection(installation)["connections"] == ["w", "files", "a"]

    def test_a_connection_that_is_not_configured_is_refused(self) -> None:
        resolved = ResolvedQuery.for_declared("the rows for [:current_value]", 1)

        with pytest.raises(ConfigError, match="nothing to run on"):
            resolved.projection(_installation(("w", "postgres")))

    def test_the_projection_reads_nothing(self, bindings) -> None:
        resolved = ResolvedQuery.for_run(SimpleNamespace(), "41")
        bindings.clear()

        resolved.projection(_installation(("rey_apps", "postgres")))

        assert bindings == []

    def test_one_act_is_taken_under_its_own_lock(self) -> None:
        one = ResolvedQuery("x", "select 1", "w")
        other = ResolvedQuery("x", "select 1", "w")

        with one.act():
            assert one.act() is one.act()
        assert one.act() is not other.act()
