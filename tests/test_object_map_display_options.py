"""A database map's display options (#1426 Map Options).

Generations (on a map focused on one object) and object types are shown or
hidden by toggles the map declares; hiding takes out the objects, their rows and
every line with an end among them, never the focused object. The options filter
what the map holds: they never read the database again. Expand All / Collapse
All act on the objects shown; a hidden object keeps whether it is open.
"""
from __future__ import annotations

from typing import Any

import pytest

from rey_lib.errors.error_utils import ConfigError
from rey_lib.inferred_architecture.object_map import SCHEMA_PROJECTION, ObjectMap

SCHEMA_READ = {"tables": [
    {"identity": {"schema": "risk", "name": "books", "object_type": "table"},
     "columns": [{"name": "book_id", "type": "bigint", "nullable": False, "ordinal": 1}],
     "primary_key": {"name": "books_pk", "columns": ["book_id"]},
     "foreign_keys": [], "indexes": [], "constraints": []},
    {"identity": {"schema": "risk", "name": "trades", "object_type": "table"},
     "columns": [{"name": "book_id", "type": "bigint", "nullable": False, "ordinal": 1}],
     "primary_key": None,
     "foreign_keys": [{"name": "trades_book_fk", "columns": ["book_id"],
                       "referenced_schema": "risk", "referenced_table": "books",
                       "referenced_columns": ["book_id"]}],
     "indexes": [], "constraints": []},
]}

#: books focused: trades and the exposure view one hop away, a function and a
#: procedure two hops away -- as code.f_object_map_table_context answers, with depth.
CONTEXT = {"root_count": 1, "root_id": "db:2", "nodes": [
    {"id": "db:2", "kind": "table", "depth": 0,
     "identity": {"schema": "risk", "name": "books", "object_type": "table", "signature": ""}},
    {"id": "db:1", "kind": "table", "depth": 1,
     "identity": {"schema": "risk", "name": "trades", "object_type": "table", "signature": ""}},
    {"id": "db:3", "kind": "view", "depth": 1,
     "identity": {"schema": "risk", "name": "exposure", "object_type": "view", "signature": ""}},
    {"id": "db:4", "kind": "code", "depth": 2,
     "identity": {"schema": "risk", "name": "book_total", "object_type": "function",
                  "signature": "bigint"}},
    {"id": "db:5", "kind": "code", "depth": 2,
     "identity": {"schema": "risk", "name": "p_trade_load", "object_type": "procedure",
                  "signature": ""}},
], "edges": [
    {"id": "reference:60", "source": "db:1", "target": "db:2", "kind": "foreign_key"},
    {"id": "reference:61", "source": "db:3", "target": "db:2", "kind": "read"},
    {"id": "reference:62", "source": "db:3", "target": "db:4", "kind": "call"},
    {"id": "reference:63", "source": "db:5", "target": "db:1", "kind": "write"},
]}


class _Control:
    """The shared Control's governed reads, counted."""

    def __init__(self) -> None:
        self.context_reads = 0

    def object_map_table_context(self, *_: Any) -> list[dict[str, Any]]:
        self.context_reads += 1
        return [CONTEXT]

    def object_map_column_lineage(self, *_: Any) -> dict[str, Any]:
        return {"columns": [], "transforms": [], "edges": [], "upstream": [], "downstream": []}


def _map() -> tuple[ObjectMap, _Control]:
    held = ObjectMap({"kind": "database_schema", "connection": "rey apps",
                      "catalog": "rey_apps", "schema": "risk"}, SCHEMA_PROJECTION, SCHEMA_READ)
    control = _Control()
    held._control = control
    held._index_installation = "admin"
    held._database_provider = "postgresql"
    return held, control


def _objects(projected: dict[str, Any]) -> set[str]:
    return {n["id"] for n in projected["nodes"] if not n.get("parent") and n["kind"] != "transform"}


def _ends(projected: dict[str, Any]) -> set[str]:
    owner = {n["id"]: n.get("parent") or n["id"] for n in projected["nodes"]}
    return {owner.get(end, end) for e in projected["edges"] for end in (e["source"], e["target"])}


def _action(projected: dict[str, Any], on: str) -> dict[str, Any]:
    return next(a for a in projected["actions"] if a["role"]["on"] == on)


def _press(held: ObjectMap, projected: dict[str, Any], on: str) -> dict[str, Any]:
    held.focus(**_action(projected, on)["command"]["focus"])
    return held.projection()


def test_a_hidden_generation_takes_its_objects_and_their_lines_and_never_reads_again() -> None:
    held, control = _map()
    held.focus(table="books")
    assert _objects(held.projection()) == {"table:books", "db:1", "db:3", "db:4", "db:5"}
    reads = control.context_reads

    held.focus(table="books", hidden_generations=[2])
    projected = held.projection()

    assert _objects(projected) == {"table:books", "db:1", "db:3"}
    assert not {"db:4", "db:5"} & _ends(projected)
    assert control.context_reads == reads


def test_the_focused_object_stays_whatever_is_hidden() -> None:
    held, _ = _map()
    held.focus(table="books", hidden_generations=[1, 2], hidden_object_types=["table"])

    assert _objects(held.projection()) == {"table:books"}


def test_each_generation_hides_on_its_own() -> None:
    held, _ = _map()
    held.focus(table="books", hidden_generations=[1])

    assert _objects(held.projection()) == {"table:books", "db:4", "db:5"}


def test_a_hidden_type_takes_its_objects_and_their_lines() -> None:
    held, _ = _map()
    held.focus(table="books", hidden_object_types=["view", "procedure"])
    projected = held.projection()

    assert _objects(projected) == {"table:books", "db:1", "db:4"}
    assert not {"db:3", "db:5"} & _ends(projected)


def test_the_toggles_are_declared_actions_a_press_flips() -> None:
    held, control = _map()
    held.focus(table="books")
    projected = held.projection()
    reads = control.context_reads

    assert [a["role"] for a in projected["actions"]][:6] == [
        {"act": "hide", "on": on} for on in
        ("first_order", "second_order", "tables", "views", "functions", "procedures")]
    hidden = _press(held, projected, "second_order")
    assert _objects(hidden) == {"table:books", "db:1", "db:3"}
    assert _action(hidden, "second_order")["role"]["act"] == "show"
    shown = _press(held, hidden, "second_order")
    assert _objects(shown) == {"table:books", "db:1", "db:3", "db:4", "db:5"}
    assert _action(shown, "second_order")["role"]["act"] == "hide"
    assert "hidden_generations" not in held._focus
    assert control.context_reads == reads


def test_the_schema_overview_offers_the_types_and_no_generations() -> None:
    held, _ = _map()

    ons = [a["role"]["on"] for a in held.projection()["actions"]]

    assert "first_order" not in ons and "second_order" not in ons
    assert {"tables", "views", "functions", "procedures"} <= set(ons)


def test_expand_and_collapse_act_on_the_objects_shown_and_keep_a_hidden_ones_state() -> None:
    held, _ = _map()
    held.focus(table="books", opened=["db:3"], hidden_object_types=["view"])
    projected = held.projection()

    expand = _action(projected, "opened")
    assert expand["role"]["act"] == "expand"
    # Every object shown that opens -- tables, functions, procedures -- and the hidden view as it was.
    assert set(expand["command"]["focus"]["opened"]) == {"books", "db:1", "db:4", "db:5", "db:3"}
    held.focus(**expand["command"]["focus"])
    collapse = _action(held.projection(), "opened")
    assert collapse["role"]["act"] == "collapse"
    assert collapse["command"]["focus"]["opened"] == ["db:3"]


@pytest.mark.parametrize("focus", [{"hidden_generations": [3]},
                                   {"hidden_object_types": ["sequence"]}])
def test_an_unknown_option_is_refused(focus: dict[str, Any]) -> None:
    held, _ = _map()

    with pytest.raises(ConfigError):
        held.focus(table="books", **focus)
