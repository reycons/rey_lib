"""What the tree data object is, and what it is allowed to read.

Moved from the Console with the object itself (backlog 1054). The tree is loaded
from one place. These prove that the object is the shared database view
specialized rather than a parallel abstraction, that it asks for its own
connection, that it reads the canonical view and nothing beneath it, and that the
identities the producing SQL composed arrive unread.
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.db.database_objects import DatabaseObjectIdentity, DatabaseView
from rey_lib.tree.persisted import (
    TREE_CONNECTION,
    TREE_SCHEMA,
    TREE_VIEW_NAME,
    TreeDataObject,
)

#: Every relation the tree is *not* allowed to read: the tables the canonical
#: view is built from, and the producers that feed it.
FORBIDDEN = (
    "tree_group", "tree_root", "tree_node",
    "persisted_tvw", "run_history_tvw", "file_manifest_tvw",
)


def tree_object() -> TreeDataObject:
    """The object as the schema reader builds it."""
    return TreeDataObject(
        identity=DatabaseObjectIdentity(
            connection=TREE_CONNECTION,
            catalog="rey_apps",
            schema=TREE_SCHEMA,
            name=TREE_VIEW_NAME,
        ),
    )


class Spy:
    """Stands in for the connection and the row query, and records what was asked.

    It answers with rows rather than reaching a database, because what is under
    test is which datasource was named and what came back unaltered -- not
    whether Postgres works.
    """

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.asked_for: list[str] = []
        self.statements: list[str] = []
        self.params: list[dict[str, Any]] = []
        self.answer = rows or []

    def shared_connection(self, _ctx: Any, name: str) -> "Spy":
        self.asked_for.append(name)
        return self

    def handle(self) -> str:
        return "handle"

    def __call__(self, statement: str, params: Any = None) -> "Spy":  # DatabaseQuery(...)
        self.statements.append(statement)
        self.params.append(dict(params or {}))
        return self

    def rows(self, handle: Any) -> tuple[dict[str, Any], ...]:
        assert handle == "handle"
        return tuple(dict(row) for row in self.answer)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> Spy:
    watcher = Spy()
    monkeypatch.setattr(
        "rey_lib.tree.persisted.shared_connection", watcher.shared_connection,
    )
    monkeypatch.setattr("rey_lib.tree.persisted.DatabaseQuery", watcher)
    return watcher


def test_it_is_the_shared_database_view_specialized() -> None:
    """One object, and it is a DatabaseView -- not a parallel abstraction."""
    assert issubclass(TreeDataObject, DatabaseView)

    obj = tree_object()
    assert isinstance(obj, DatabaseView)
    assert obj.identity.schema == "tree"
    assert obj.identity.name == "tree_vw"

    # The view's own facts come from the shared model, not restated here.
    for field in ("identity", "columns", "ddl", "initial_select"):
        assert hasattr(obj, field)


def test_it_reads_through_its_own_connection(spy: Spy) -> None:
    """The tree connection, never control's -- they are separate identities."""
    tree_object().rows(ctx=object())

    assert spy.asked_for == ["tree"]
    assert "control" not in spy.asked_for


def test_it_reads_the_canonical_view_and_nothing_beneath_it(spy: Spy) -> None:
    """One datasource. The tables and the producers are never named."""
    tree_object().rows(ctx=object())

    statement = spy.statements[0]
    assert "tree.tree_vw" in statement
    # Qualified, not bare: "tree_group" also matches the ordering column
    # tree_group_id, and a substring test over SQL cannot tell a table from a
    # column that shares its prefix.
    for relation in FORBIDDEN:
        assert f"tree.{relation}" not in statement, f"the loader named {relation}"


def test_identities_arrive_unread(monkeypatch: pytest.MonkeyPatch) -> None:
    """node_key is the producing SQL's. Nothing splits or rebuilds one."""
    emitted = {
        "row_type": "node",
        "node_key": "5:6:4325:4353",
        "parent_node_key": "5:6:6:4325",
        "label": "Pipelines",
    }
    watcher = Spy(rows=[emitted])
    monkeypatch.setattr(
        "rey_lib.tree.persisted.shared_connection", watcher.shared_connection,
    )
    monkeypatch.setattr("rey_lib.tree.persisted.DatabaseQuery", watcher)

    (row,) = tree_object().rows(ctx=object())

    assert row["node_key"] == "5:6:4325:4353"
    assert row["parent_node_key"] == "5:6:6:4325"
    assert row.to_dict() == emitted


def test_a_scoped_read_binds_its_group(spy: Spy) -> None:
    """A tab's roots are asked for by group, and the id is bound."""
    tree_object().rows(ctx=object(), row_type="root", tree_group_id=1)

    statement, params = spy.statements[0], spy.params[0]
    assert "tree_group_id = :tree_group_id" in statement
    assert params["tree_group_id"] == 1


def _order_by(statement: str) -> list[str]:
    """The ORDER BY terms of one statement, one per entry."""
    clause = statement.split("ORDER BY", 1)[1].split("LIMIT", 1)[0]
    return [" ".join(term.split()) for term in clause.split(",")]


def test_a_root_read_is_ordered_by_the_roots_declared_sort_order(spy: Spy) -> None:
    """Backlog 569: a root's row declares its position, and the read honours it."""
    tree_object().rows(ctx=object(), row_type="root", tree_group_id=1)

    terms = _order_by(spy.statements[0])
    assert terms[:3] == [
        "tree_group_id", "sort_order NULLS LAST", "tree_root_id NULLS FIRST",
    ]


def test_a_child_read_keeps_the_projections_own_order(spy: Spy) -> None:
    """Only roots changed: every other level is ordered exactly as before."""
    tree_object().rows(ctx=object(), row_type="node", parent_node_key="1:1:9")

    assert _order_by(spy.statements[0]) == [
        "tree_group_id",
        "tree_root_id NULLS FIRST",
        "sort_rank NULLS LAST",
        "sort_ts DESC NULLS LAST",
        "sort_number NULLS LAST",
        "sort_text NULLS LAST",
        "node_key NULLS FIRST",
    ]


def test_a_child_read_binds_the_parent_key(spy: Spy) -> None:
    """Children are asked for by the edge the view declares, bound not spliced."""
    tree_object().rows(ctx=object(), row_type="node", parent_node_key="1:1:9")

    statement, params = spy.statements[0], spy.params[0]
    assert "parent_node_key = :parent_node_key" in statement
    assert params["parent_node_key"] == "1:1:9"
    # The key is carried whole: it is bound, never spliced into the statement.
    assert "1:1:9" not in statement


def test_predicates_compose(spy: Spy) -> None:
    """Every predicate is optional, and together they narrow one read."""
    tree_object().rows(
        ctx=object(), row_type="node", tree_group_id=2, parent_node_key="2:3",
    )

    statement, params = spy.statements[0], spy.params[0]
    assert statement.count("AND") >= 2
    assert set(params) == {"row_type", "tree_group_id", "parent_node_key"}


def test_a_scoped_read_binds_its_root(spy: Spy) -> None:
    """A root is a predicate like a group: optional, and bound."""
    tree_object().rows(ctx=object(), row_type="node", tree_root_id=35)

    statement, params = spy.statements[0], spy.params[0]
    assert "tree_root_id = :tree_root_id" in statement
    assert params["tree_root_id"] == 35
    assert "35" not in statement


def test_a_child_read_carries_the_parents_own_scope(spy: Spy) -> None:
    """Expanding a node binds the group and root the record already holds.

    Without them the union evaluates every tree to answer one node.
    """
    parent = {"node_key": "19:35:ungrouped:1", "tree_group_id": 19, "tree_root_id": 35}

    tree_object().children(object(), parent)

    statement, params = spy.statements[0], spy.params[0]
    assert params == {
        "row_type": "node",
        "tree_group_id": 19,
        "tree_root_id": 35,
        "parent_node_key": "19:35:ungrouped:1",
    }
    assert "tree.tree_vw" in statement


def test_a_record_without_scope_is_read_by_its_key_alone(spy: Spy) -> None:
    """Compatibility fallback: a record holding no scope adds no predicate."""
    tree_object().children(object(), {"node_key": "1:1:9"})

    assert spy.params[0] == {"row_type": "node", "parent_node_key": "1:1:9"}


def test_row_types_are_preserved_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    """group, root and node arrive as the view spelled them."""
    emitted = [{"row_type": kind} for kind in ("group", "root", "node")]
    watcher = Spy(rows=emitted)
    monkeypatch.setattr(
        "rey_lib.tree.persisted.shared_connection", watcher.shared_connection,
    )
    monkeypatch.setattr("rey_lib.tree.persisted.DatabaseQuery", watcher)

    rows = tree_object().rows(ctx=object())

    assert [row.row_type for row in rows] == ["group", "root", "node"]


def test_one_kind_and_a_bound_are_asked_for_rather_than_filtered(spy: Spy) -> None:
    """The database restricts and limits. Python does not read then discard."""
    tree_object().rows(ctx=object(), row_type="node", limit=50)

    statement, params = spy.statements[0], spy.params[0]
    assert "row_type = :row_type" in statement
    assert "LIMIT :row_limit" in statement
    assert params == {"row_type": "node", "row_limit": 50}
    # Bound, never interpolated.
    assert "'node'" not in statement
    assert "50" not in statement


def test_repeated_rows_for_one_node_are_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """A node carrying two actions is two rows, because the view says so."""
    emitted = [
        {"row_type": "node", "node_key": "1:1:9", "action_object_id": "open_query"},
        {"row_type": "node", "node_key": "1:1:9", "action_object_id": "open_rollback"},
    ]
    watcher = Spy(rows=emitted)
    monkeypatch.setattr(
        "rey_lib.tree.persisted.shared_connection", watcher.shared_connection,
    )
    monkeypatch.setattr("rey_lib.tree.persisted.DatabaseQuery", watcher)

    rows = tree_object().rows(ctx=object())

    assert len(rows) == 2
    assert {row["action_object_id"] for row in rows} == {"open_query", "open_rollback"}


def test_the_persisted_tree_does_not_reach_dbadapter() -> None:
    """Its fixed statements run through DatabaseQuery, never DBAdapter (backlog 730)."""
    from pathlib import Path

    from rey_lib.tree import persisted

    source = Path(persisted.__file__).read_text(encoding="utf-8")
    assert "DBAdapter" not in source
    assert "execute_sql" not in source
    assert "from rey_lib.db.database_query import DatabaseQuery" in source
