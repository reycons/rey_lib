"""The canonical Tree's group and root composition (#1218).

Which governed groups a placement surfaces, and which roots a group exposes with
their configuration resolved. Nothing here builds a web representation.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.errors.error_utils import ConfigError
from rey_lib.tree import composition
from rey_lib.tree.composition import RUNTIME_BINDINGS, tree_groups, tree_roots
from rey_lib.tree.persisted import TreeDataRow


class _Paths:
    """An installation PathResolver stand-in: every token resolves under /install."""

    def resolve(self, token: str) -> str:
        """Resolve one token."""
        return f"/install/{token}"


def _ctx() -> SimpleNamespace:
    """An installation context, as an application runs under one."""
    return SimpleNamespace(installation=SimpleNamespace(name="local"), paths=_Paths())


def _groups(monkeypatch: pytest.MonkeyPatch, rows: list[dict]) -> dict[str, Any]:
    """Stub the routine read; record what was asked."""
    asked: dict[str, Any] = {}
    monkeypatch.setattr(composition, "shared_connection", lambda ctx, name: (
        asked.__setitem__("connection", name) or SimpleNamespace(handle=lambda: None)))

    def query(statement: str, params: dict[str, Any]) -> SimpleNamespace:
        asked["statement"], asked["params"] = statement, params
        return SimpleNamespace(rows=lambda handle: rows)

    monkeypatch.setattr(composition, "DatabaseQuery", query)
    return asked


def _roots(monkeypatch: pytest.MonkeyPatch, rows: list[dict]) -> list[dict[str, Any]]:
    """Stub the persisted Tree; record the reads."""
    reads: list[dict[str, Any]] = []

    class _Tree:
        def rows(self, ctx: Any, **scope: Any) -> tuple[TreeDataRow, ...]:
            reads.append(scope)
            return tuple(TreeDataRow(values=dict(row)) for row in rows)

    monkeypatch.setattr(composition, "persisted_tree", _Tree)
    return reads


def test_groups_are_the_routines_answer_for_the_installation_and_placement(monkeypatch) -> None:
    asked = _groups(monkeypatch, [
        {"tree_group_id": 7, "tree_group_key": "development", "label": "Development",
         "icon": "tree", "panel_key": "explorer", "panel_configuration": None},
    ])

    found = tree_groups(_ctx(), "explorer")

    assert asked["connection"] == "tree"
    assert "tree.f_tree_tabs(:installation, :placement)" in asked["statement"]
    assert asked["params"] == {"installation": "local", "placement": "explorer"}
    assert found == [{"tree_group_id": 7, "tree_group_key": "development",
                      "label": "Development", "icon": "tree", "panel_key": "explorer",
                      "panel_configuration": {}}]


def test_the_groups_statement_is_the_packaged_file(monkeypatch) -> None:
    asked = _groups(monkeypatch, [])

    tree_groups(_ctx(), "explorer")

    packaged = (composition._SQL_DIR / "tree_groups.sql").read_text(encoding="utf-8")
    assert asked["statement"] == packaged


def test_no_placement_is_refused() -> None:
    with pytest.raises(ConfigError, match="named placement"):
        tree_groups(_ctx(), "")


def test_a_context_naming_no_installation_is_refused() -> None:
    with pytest.raises(ConfigError, match="names none"):
        tree_groups(SimpleNamespace(paths=_Paths()), "explorer")


def test_roots_are_the_groups_persisted_roots_and_disabled_ones_are_not_exposed(
    monkeypatch,
) -> None:
    reads = _roots(monkeypatch, [
        {"node_key": "r1", "is_enabled": True, "tree_configuration": {}},
        {"node_key": "r2", "is_enabled": "f", "tree_configuration": {}},
    ])

    found = tree_roots(_ctx(), 7)

    assert reads == [{"row_type": "root", "tree_group_id": 7}]
    assert [root["node_key"] for root in found] == ["r1"]


def test_a_whole_path_token_resolves_through_the_installation(monkeypatch) -> None:
    _roots(monkeypatch, [{"node_key": "r", "is_enabled": True, "tree_configuration": {
        "path": "{configs}", "label": "{not whole} token", "filters": {"a": "{configs}"},
    }}])

    (root,) = tree_roots(_ctx(), 7)

    assert root["tree_configuration"] == {
        "path": "/install/configs", "label": "{not whole} token", "filters": {"a": "{configs}"},
    }


def test_a_runtime_binding_is_supplied_by_the_composition(monkeypatch) -> None:
    _roots(monkeypatch, [{"node_key": "r", "is_enabled": True,
                          "tree_configuration": {"connection": "{connection}"}}])

    (root,) = tree_roots(_ctx(), 7, {"connection": "warehouse", "path": "/elsewhere"})

    assert root["tree_configuration"] == {"connection": "warehouse"}
    assert RUNTIME_BINDINGS == frozenset({"connection"})


def test_a_missing_runtime_binding_is_refused_not_left_blank(monkeypatch) -> None:
    _roots(monkeypatch, [{"node_key": "r", "is_enabled": True,
                          "tree_configuration": {"connection": "{connection}"}}])

    with pytest.raises(ConfigError, match="no 'connection' was given"):
        tree_roots(_ctx(), 7)


def test_refresh_in_configuration_states_whether_a_root_may_be_read_again(monkeypatch) -> None:
    _roots(monkeypatch, [
        {"node_key": "a", "is_enabled": True, "tree_configuration": {"refresh": True}},
        {"node_key": "b", "is_enabled": True, "tree_configuration": {}},
    ])

    first, second = tree_roots(_ctx(), 7)

    assert first["refreshable"] is True
    assert "refreshable" not in second


class _Traversal:
    """A TreeTraversal stand-in: what it was built from and asked, and the children it yields."""

    built: list[tuple[Any, Any]] = []
    asked: list[tuple[Any, ...]] = []

    def __init__(self, persisted: Any, sources: Any) -> None:
        """Record what the traversal was built over."""
        _Traversal.built.append((persisted, sources))

    def children(self, ctx: Any, record: dict, projection: Any, fresh: bool) -> list[dict]:
        """Yield one enabled and one disabled child."""
        _Traversal.asked.append((record, projection, fresh))
        return [{"node_key": "c1", "is_enabled": "t"}, {"node_key": "c2", "is_enabled": False}]


def test_a_nodes_children_are_the_canonical_traversals_enabled_children(monkeypatch) -> None:
    _Traversal.built, _Traversal.asked = [], []
    monkeypatch.setattr(composition, "TreeTraversal", _Traversal)
    monkeypatch.setattr(composition, "persisted_tree", lambda: "persisted")
    monkeypatch.setattr(composition._tree, "rey_lib_sources", lambda: {"folder": "files"})
    projection = object()

    children = composition.tree_children(_ctx(), {"node_key": "7:1"}, projection, fresh=True)

    assert children == [{"node_key": "c1", "is_enabled": "t"}]
    assert _Traversal.built == [("persisted", {"folder": "files"})]
    assert _Traversal.asked == [({"node_key": "7:1"}, projection, True)]
