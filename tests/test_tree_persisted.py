"""What the tree data object is, and how it reads (backlog 1054, 1207, #1360).

The object is the shared database view specialized rather than a parallel
abstraction; it reads through the runtime's one Control, as the governed binding
get_tree_rows over tree.f_tree_rows_ordered, and holds no statement and names no
relation itself; and the identities the producing SQL composed arrive unread.
The Tree's presentation order is the routine's (tree.f_tree_rows_ordered,
#1360): Python neither states nor re-applies it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.config import bootstrap
from rey_lib.db.database_objects import DatabaseObjectIdentity, DatabaseView
from rey_lib.errors.error_utils import ConfigError
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

#: Every value the governed read binds, each present on every read.
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
    """Stands in for the runtime's shared Control, and records what was asked.

    It answers with rows rather than reaching a database, because what is under
    test is what the object asked the governed read for and what came back
    unaltered -- not whether Postgres works.
    """

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.contexts: list[Any] = []
        self.params: list[dict[str, Any]] = []
        self.answer = rows or []

    def open_shared_control(self, ctx: Any) -> SimpleNamespace:
        self.contexts.append(ctx)
        return SimpleNamespace(shared_control=self)

    def tree_rows(self, **params: Any) -> list[dict[str, Any]]:  # Control.tree_rows
        self.params.append(dict(params))
        return [dict(row) for row in self.answer]


def _watch(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]] | None = None) -> Spy:
    watcher = Spy(rows)
    monkeypatch.setattr(bootstrap, "open_shared_control", watcher.open_shared_control)
    return watcher


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> Spy:
    return _watch(monkeypatch)


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


def test_it_reads_through_the_runtimes_shared_control(spy: Spy) -> None:
    """The runtime's one Control, as the governed get_tree_rows read (#1360)."""
    ctx = object()
    tree_object().rows(ctx=ctx)

    assert spy.contexts == [ctx]
    assert len(spy.params) == 1


def test_a_runtime_with_no_control_database_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """No control database, no Tree read -- refused, never answered empty."""
    monkeypatch.setattr(bootstrap, "open_shared_control",
                        lambda ctx: SimpleNamespace(shared_control=None))
    with pytest.raises(ConfigError, match="no control database"):
        tree_object().rows(ctx=object())


def test_it_holds_no_statement_and_names_no_relation() -> None:
    """The read is the routine's: no SQL, no table, provider or union named here (#1360)."""
    from pathlib import Path

    from rey_lib.tree import persisted

    source = Path(persisted.__file__).read_text(encoding="utf-8")
    for keyword in ("SELECT", "ORDER BY", "LIMIT"):
        assert keyword not in source, f"persisted.py holds SQL ({keyword})"
    # Qualified, not bare: "tree_group" also matches the column tree_group_id.
    # tree.tree_vw is excepted: it is this object's own identity, which its
    # docstrings name; it is never read from here.
    for relation in (one for one in FORBIDDEN if one != "tree_vw"):
        assert f"tree.{relation}" not in source, f"the loader named {relation}"


def test_identities_arrive_unread(monkeypatch: pytest.MonkeyPatch) -> None:
    """node_key is the producing SQL's. Nothing splits or rebuilds one."""
    emitted = {
        "row_type": "node",
        "node_key": "5:6:4325:4353",
        "parent_node_key": "5:6:6:4325",
        "label": "Pipelines",
    }
    _watch(monkeypatch, [emitted])

    (row,) = tree_object().rows(ctx=object())

    assert row["node_key"] == "5:6:4325:4353"
    assert row["parent_node_key"] == "5:6:6:4325"
    assert row.to_dict() == emitted


def test_a_scoped_read_binds_its_group(spy: Spy) -> None:
    """A tab's roots are asked for by group, and the id is bound."""
    tree_object().rows(ctx=object(), row_type="root", tree_group_id=1)

    assert spy.params[0]["tree_group_id"] == 1
    assert spy.params[0]["row_type"] == "root"


def test_rows_keep_the_routines_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """The presentation order is tree.f_tree_rows_ordered's; nothing here re-sorts it."""
    emitted = [{"row_type": "root", "node_key": key, "sort_order": order}
               for key, order in (("1:9", 3), ("1:2", 1), ("1:5", None))]
    _watch(monkeypatch, emitted)

    rows = tree_object().rows(ctx=object(), row_type="root", tree_group_id=1)

    assert [row["node_key"] for row in rows] == ["1:9", "1:2", "1:5"]


def test_a_child_read_binds_the_parent_key(spy: Spy) -> None:
    """Children are asked for by the edge the view declares, bound as a value."""
    tree_object().rows(ctx=object(), row_type="node", parent_node_key="1:1:9")

    assert spy.params[0]["parent_node_key"] == "1:1:9"


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

    assert spy.params[0]["tree_root_id"] == 35


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
    _watch(monkeypatch, [{"row_type": kind} for kind in ("group", "root", "node")])

    rows = tree_object().rows(ctx=object())

    assert [row.row_type for row in rows] == ["group", "root", "node"]


def test_one_kind_and_a_bound_are_asked_for_rather_than_filtered(spy: Spy) -> None:
    """The database restricts and limits. Python does not read then discard."""
    tree_object().rows(ctx=object(), row_type="node", limit=50)

    assert spy.params[0]["row_type"] == "node" and spy.params[0]["row_limit"] == 50


def test_repeated_rows_for_one_node_are_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """A node carrying two actions is two rows, because the view says so."""
    _watch(monkeypatch, [
        {"row_type": "node", "node_key": "1:1:9", "action_object_id": "open_query"},
        {"row_type": "node", "node_key": "1:1:9", "action_object_id": "open_rollback"},
    ])

    rows = tree_object().rows(ctx=object())

    assert len(rows) == 2
    assert {row["action_object_id"] for row in rows} == {"open_query", "open_rollback"}


def test_the_persisted_tree_does_not_reach_dbadapter() -> None:
    """No DBAdapter (backlog 730), and no statement runner of its own either (#1360)."""
    from pathlib import Path

    from rey_lib.tree import persisted

    source = Path(persisted.__file__).read_text(encoding="utf-8")
    assert "DBAdapter" not in source
    assert "execute_sql" not in source
    assert "DatabaseQuery" not in source
    assert "read_text_file" not in source


def test_the_read_is_the_governed_binding() -> None:
    """Control.tree_rows calls get_tree_rows with every value, required (#1360)."""
    from unittest.mock import patch

    from rey_lib.control.control import Control

    rows = [{"row_type": "root", "node_key": "1:2"}]
    with patch.object(Control, "_call_rows", return_value=rows) as called:
        answered = Control.__new__(Control).tree_rows(
            expansion_tvw=None, row_type="root", tree_group_id=1, tree_root_id=None,
            parent_node_key=None, row_limit=None)

    called.assert_called_once_with("get_tree_rows", {
        "expansion_tvw": None, "row_type": "root", "tree_group_id": 1,
        "tree_root_id": None, "parent_node_key": None, "row_limit": None,
    }, required=True)
    assert answered == rows and answered[0] is not rows[0]
