"""The database source's two reads, moved with it into rey_lib.tree (backlog 1057).

From the Console's test_database_schema_reader.py and test_connection_snapshot.py:

- load_database_schema: reading is rey_lib's (``DatabaseSchema.read``); asserted
  here is the shared connection, a missing name as ConfigError, a failed read or
  connection as DatabaseError, and the one view the tree is loaded from arriving
  as ``TreeDataObject``.
- connection_snapshot: it asks ``DatabaseSchema.list`` and composes catalogs and
  schemas from what it answers. It reaches no provider adapter, carries no
  provider capability, and lists no relations. These exist because the suite
  once stayed green while the tree could not open.

The views' statements here are markers: they are carried, never read.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from rey_lib.db.database_objects import DatabaseObjectIdentity, DatabaseSchema, DatabaseView
from rey_lib.errors.error_utils import (
    ConfigError,
    DatabaseError,
    UnsupportedDatabaseCapabilityError,
)
from rey_lib.tree import TREE_SCHEMA, TREE_VIEW_NAME, TreeDataObject
from rey_lib.tree import database
from rey_lib.tree.database import connection_snapshot, load_database_schema


class _Connection:
    def handle(self) -> str:
        return "handle"


def _view(schema: str, name: str) -> DatabaseView:
    return DatabaseView(
        identity=DatabaseObjectIdentity(connection="rey_apps", catalog="rey", schema=schema, name=name),
        ddl="ddl-marker", initial_select=f"select-marker {schema}.{name}",
    )


# -- load_database_schema ----------------------------------------------------


@pytest.fixture
def read(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """The shared connection, and the rey_lib read it calls."""
    calls: list[tuple] = []
    monkeypatch.setattr(
        "rey_lib.tree.database.shared_connection", lambda _ctx, _name: _Connection(),
    )

    def _read(conn: Any, connection: str, catalog: str, schema: str) -> DatabaseSchema:
        calls.append((conn, connection, catalog, schema))
        views = (_view(schema, TREE_VIEW_NAME), _view(schema, "other_vw"))
        return DatabaseSchema(connection=connection, catalog=catalog, schema=schema, views=views)

    monkeypatch.setattr(DatabaseSchema, "read", staticmethod(_read))
    return calls


def test_it_reads_through_rey_lib_on_the_shared_connection(read: list[tuple]) -> None:
    schema = load_database_schema(object(), "rey_apps", "rey", "control")

    assert read == [("handle", "rey_apps", "rey", "control")]
    assert isinstance(schema, DatabaseSchema)


def test_the_tree_view_arrives_as_the_tree_data_object(read: list[tuple]) -> None:
    schema = load_database_schema(object(), "rey_apps", "rey", TREE_SCHEMA)

    tree_view, other = schema.views
    assert type(tree_view) is TreeDataObject
    assert tree_view.identity.name == TREE_VIEW_NAME
    assert tree_view.initial_select == f"select-marker {TREE_SCHEMA}.{TREE_VIEW_NAME}"
    assert type(other) is DatabaseView


def test_a_view_of_that_name_in_another_schema_is_left_a_view(read: list[tuple]) -> None:
    schema = load_database_schema(object(), "rey_apps", "rey", "control")

    assert all(type(view) is DatabaseView for view in schema.views)


def test_a_schema_read_needs_a_named_connection(read: list[tuple]) -> None:
    with pytest.raises(ConfigError, match="named connection"):
        load_database_schema(object(), "", "rey", "control")
    assert read == []


def test_a_failed_read_is_reported_as_a_database_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "rey_lib.tree.database.shared_connection", lambda _ctx, _name: _Connection(),
    )

    def _fail(*_args: Any) -> DatabaseSchema:
        raise DatabaseError("The schema 'control' could not be read: boom")

    monkeypatch.setattr(DatabaseSchema, "read", staticmethod(_fail))

    with pytest.raises(DatabaseError, match="could not be read: boom"):
        load_database_schema(object(), "rey_apps", "rey", "control")


def test_a_connection_that_cannot_open_is_a_database_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse(_ctx: Any, _name: str) -> Any:
        raise RuntimeError("no such connection")

    monkeypatch.setattr("rey_lib.tree.database.shared_connection", _refuse)

    with pytest.raises(DatabaseError, match="no such connection"):
        load_database_schema(object(), "rey_apps", "rey", "control")


# -- connection_snapshot -----------------------------------------------------


class _Shared:
    def handle(self) -> str:
        return "handle"


@pytest.fixture
def listed(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """The shared connection, and the schemas DatabaseSchema.list answers with."""
    calls: list[tuple] = []
    monkeypatch.setattr(database, "shared_connection", lambda _ctx, _name: _Shared())

    def _list(conn: Any, connection: str) -> tuple[DatabaseSchema, ...]:
        calls.append((conn, connection))
        return (
            DatabaseSchema(connection=connection, catalog="rey", schema="control"),
            DatabaseSchema(connection=connection, catalog="rey", schema="tree"),
        )

    monkeypatch.setattr(DatabaseSchema, "list", staticmethod(_list))
    return calls


def test_it_answers_with_the_catalogs_and_schemas_listed(listed: list[tuple]) -> None:
    answered = connection_snapshot(object(), "rey_apps")

    assert answered == {
        "catalogs": [{"name": "rey"}],
        "schemas": [{"catalog": "rey", "name": "control"}, {"catalog": "rey", "name": "tree"}],
    }
    assert listed == [("handle", "rey_apps")]


def test_it_carries_no_capabilities_and_no_relations(listed: list[tuple]) -> None:
    """The kinds are the canonical object types; contents are each schema's read."""
    answered = connection_snapshot(object(), "rey_apps")

    for absent in ("capabilities", "tables", "views", "procedures", "functions"):
        assert absent not in answered


def test_a_provider_that_cannot_list_schemas_is_said_not_shown_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capability failure is not a database with no schemas."""
    monkeypatch.setattr(database, "shared_connection", lambda _ctx, _name: _Shared())

    def _refuse(*_a: Any) -> tuple:
        raise UnsupportedDatabaseCapabilityError(
            "DBAdapter: provider 'sqlserver' does not support metadata capability 'schemas'."
        )

    monkeypatch.setattr(DatabaseSchema, "list", staticmethod(_refuse))

    with pytest.raises(DatabaseError, match="provider 'sqlserver'"):
        connection_snapshot(object(), "rey_apps")


def test_a_snapshot_needs_a_named_connection(listed: list[tuple]) -> None:
    with pytest.raises(ConfigError, match="named connection"):
        connection_snapshot(object(), "")
    assert listed == []


def test_the_snapshot_composes_canonical_objects_only() -> None:
    """Canonical objects -> provider adapter: never straight to the adapter.

    Read over the function's own code (its docstring set aside): it now shares a
    module with the feeder, which the feeder's own test checks separately.
    """
    code = inspect.getsource(connection_snapshot).split('"""', 2)[2]

    assert "DBAdapter" not in code
    assert "db_adapter" not in code
    assert "supports" not in code
    assert "DatabaseSchema.list" in code
