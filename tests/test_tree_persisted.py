"""What the tree data object is, and how it reads (backlog 1054, 1207).

The object is the shared database view specialized rather than a parallel
abstraction; it asks for its own connection; it reads through one packaged,
parameterized statement over tree.f_tree_rows and names no relation itself; and
the identities the producing SQL composed arrive unread.
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

#: Every relation the read must not name itself: the tables beneath the Tree,
#: the providers (the database dispatches to them), and the union.
FORBIDDEN = (
    "tree_group", "tree_root", "tree_node", "tree_vw",
    "persisted_tvw", "run_history_tvw", "file_manifest_tvw",
)

#: Every value the statement binds, each present on every read.
BOUND = {"expansion_tvw", "row_type", "tree_group_id", "tree_root_id",
         "parent_node_key", "row_limit"}


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


def test_it_reads_through_the_dispatch_function_and_names_no_relation(spy: Spy) -> None:
    """One packaged statement over tree.f_tree_rows; no table, provider or union named."""
    tree_object().rows(ctx=object())

    statement = spy.statements[0]
    assert "tree.f_tree_rows(" in statement
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
    assert ":tree_group_id" in statement
    assert params["tree_group_id"] == 1


def _order_by(statement: str) -> list[str]:
    """The ORDER BY terms of one statement, one per entry."""
    clause = statement.split("ORDER BY", 1)[1].split("LIMIT", 1)[0]
    return [" ".join(term.split()) for term in clause.split(",")]


def test_a_root_read_is_ordered_by_the_roots_declared_sort_order(spy: Spy) -> None:
    """Backlog 569: a root's row declares its position, and the read honours it."""
    tree_object().rows(ctx=object(), row_type="root", tree_group_id=1)

    terms = _order_by(spy.statements[0])
    assert terms[0] == "tree_group_id"
    # The root's declared position, applied for a root read only.
    assert terms[1] == ("CASE WHEN CAST(:row_type AS character varying) = 'root' "
                        "THEN sort_order END NULLS LAST")
    assert terms[2] == "tree_root_id NULLS FIRST"
    assert spy.params[0]["row_type"] == "root"


def test_a_child_read_keeps_the_projections_own_order(spy: Spy) -> None:
    """Only roots changed: every other level is ordered exactly as before."""
    tree_object().rows(ctx=object(), row_type="node", parent_node_key="1:1:9")

    assert _order_by(spy.statements[0]) == [
        "tree_group_id",
        "CASE WHEN CAST(:row_type AS character varying) = 'root' THEN sort_order END NULLS LAST",
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
    assert ":parent_node_key" in statement
    assert params["parent_node_key"] == "1:1:9"
    # The key is carried whole: it is bound, never spliced into the statement.
    assert "1:1:9" not in statement


def test_every_value_is_bound_and_an_omitted_filter_is_null(spy: Spy) -> None:
    """Every filter is optional: an omitted one is bound as NULL and applies none."""
    tree_object().rows(
        ctx=object(), row_type="node", tree_group_id=2, parent_node_key="2:3",
    )

    params = spy.params[0]
    assert set(params) == BOUND
    assert params == {"expansion_tvw": None, "row_type": "node", "tree_group_id": 2,
                      "tree_root_id": None, "parent_node_key": "2:3", "row_limit": None}


def test_a_scoped_read_binds_its_root(spy: Spy) -> None:
    """A root is a predicate like a group: optional, and bound."""
    tree_object().rows(ctx=object(), row_type="node", tree_root_id=35)

    statement, params = spy.statements[0], spy.params[0]
    assert ":tree_root_id" in statement
    assert params["tree_root_id"] == 35
    assert "35" not in statement


def test_a_child_read_carries_the_parents_own_scope(spy: Spy) -> None:
    """Expanding a node binds the group and root the record already holds.

    The record's own expansion is what the database dispatches.
    """
    parent = {"node_key": "19:35:ungrouped:1", "tree_group_id": 19, "tree_root_id": 35,
              "expansion_tvw": "tree.development_backlog_tvw"}

    tree_object().children(object(), parent)

    assert spy.params[0] == {
        "expansion_tvw": "tree.development_backlog_tvw",
        "row_type": "node",
        "tree_group_id": 19,
        "tree_root_id": 35,
        "parent_node_key": "19:35:ungrouped:1",
        "row_limit": None,
    }


def test_a_record_declaring_no_expansion_is_read_from_the_persisted_provider(spy: Spy) -> None:
    """A NULL expansion is bound as NULL: the database answers it from persisted_tvw."""
    tree_object().children(object(), {"node_key": "1:1:9", "expansion_tvw": "  "})

    assert spy.params[0]["expansion_tvw"] is None


def test_a_record_without_scope_is_read_by_its_key_alone(spy: Spy) -> None:
    """A record holding no scope binds none: its key alone narrows the read."""
    tree_object().children(object(), {"node_key": "1:1:9"})

    assert spy.params[0] == {"expansion_tvw": None, "row_type": "node", "tree_group_id": None,
                             "tree_root_id": None, "parent_node_key": "1:1:9",
                             "row_limit": None}


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
    assert ":row_type" in statement
    assert "LIMIT CAST(:row_limit AS bigint)" in statement
    assert params["row_type"] == "node" and params["row_limit"] == 50
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
