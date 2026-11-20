"""Table Map column lineage as ER boxes (#1426).

Every table and view on a Table Map is an ER box with a +/- header row; an
expanded one shows its columns as rows marked primary key, foreign key and
unique. #1425's derivations and the schema's foreign keys attach to rows or to
boxes as each end is expanded; both ends collapsed leave the object map as it
was. The lineage is read through the governed read, never kept.
"""
from __future__ import annotations

from typing import Any

import pytest

from rey_lib.control.control import Control
from rey_lib.errors.error_utils import ConfigError
from rey_lib.inferred_architecture.object_map import SCHEMA_PROJECTION, ObjectMap
from tests.schema_objects import schema_objects

SCHEMA_READ = {"tables": [
    {"identity": {"schema": "risk", "name": "trades", "object_type": "table"},
     "columns": [{"name": "trade_id", "type": "bigint", "nullable": False, "ordinal": 1},
                 {"name": "book_id", "type": "bigint", "nullable": False, "ordinal": 2},
                 {"name": "amount", "type": "numeric", "nullable": True, "ordinal": 3},
                 {"name": "trade_ref", "type": "text", "nullable": False, "ordinal": 4}],
     "primary_key": {"name": "trades_pk", "columns": ["trade_id"]},
     "foreign_keys": [{"name": "trades_book_fk", "columns": ["book_id"],
                       "referenced_schema": "risk", "referenced_table": "books",
                       "referenced_columns": ["book_id"]}],
     "indexes": [{"name": "trades_ref_ix", "columns": ["trade_ref"], "unique": True}],
     "constraints": []},
    {"identity": {"schema": "risk", "name": "books", "object_type": "table"},
     "columns": [{"name": "book_id", "type": "bigint", "nullable": False, "ordinal": 1},
                 {"name": "name", "type": "text", "nullable": False, "ordinal": 2}],
     "primary_key": {"name": "books_pk", "columns": ["book_id"]},
     "foreign_keys": [], "indexes": [],
     "constraints": [{"name": "books_name_uq", "columns": ["name"], "kind": "unique"}]},
]}

#: The Table Map of books: trades references it, the exposure view reads it.
CONTEXT = {"root_count": 1, "root_id": "db:2", "nodes": [
    {"id": "db:2", "kind": "table", "label": "books",
     "identity": {"schema": "risk", "name": "books", "object_type": "table", "signature": ""}},
    {"id": "db:1", "kind": "table", "label": "trades",
     "identity": {"schema": "risk", "name": "trades", "object_type": "table", "signature": ""}},
    {"id": "db:3", "kind": "view", "label": "exposure",
     "identity": {"schema": "risk", "name": "exposure", "object_type": "view", "signature": ""}},
    # A function on the screen: the same panel as a table or view, no +/-.
    {"id": "db:4", "kind": "code", "label": "risk.book_total",
     "identity": {"schema": "risk", "name": "book_total", "object_type": "function",
                  "signature": "bigint"}},
    # A source file on the screen: its identity names no schema (the + crash).
    {"id": "symbol:7", "kind": "code", "label": "risk/load.py",
     "identity": {"repository_key": "risk", "relative_path": "risk/load.py"}},
], "edges": [
    {"id": "reference:60", "source": "db:1", "target": "db:2", "kind": "foreign_key"},
    {"id": "reference:61", "source": "db:3", "target": "db:2", "kind": "read"},
    {"id": "reference:62", "source": "db:3", "target": "db:4", "kind": "call"},
]}


def _column(member: int, name: str, object_type: str, column: str, ordinal: int) -> dict:
    return {"id": f"db:{member}", "schema": "risk", "name": name, "object_type": object_type,
            "column": column, "data_type": "text", "ordinal": ordinal}


COLUMNS = [
    _column(11, "trades", "table", "trade_id", 1), _column(12, "trades", "table", "book_id", 2),
    _column(13, "trades", "table", "amount", 3), _column(14, "trades", "table", "trade_ref", 4),
    _column(21, "books", "table", "book_id", 1), _column(22, "books", "table", "name", 2),
    _column(51, "exposure", "view", "book_name", 1), _column(52, "exposure", "view", "total", 2),
]
TRANSFORMS = [
    {"id": "db:31", "key": "1.q.value.1.book_name", "role": "value", "expression": "b.name",
     "schema": "risk", "name": "exposure", "object_type": "view", "signature": ""},
    {"id": "db:32", "key": "exposure.total", "role": "value", "expression": "sum(t.amount)",
     "schema": "risk", "name": "exposure", "object_type": "view", "signature": ""},
]
EDGES = [
    {"id": "reference:41", "source": "db:22", "target": "db:31", "kind": "derivation_input",
     "evidence": [{"fact_source": "sql", "resolution": "resolved", "evidence": "b.name",
                   "statement_ordinal": 1}]},
    {"id": "reference:42", "source": "db:31", "target": "db:51", "kind": "derivation_output",
     "evidence": []},
    {"id": "reference:43", "source": "db:13", "target": "db:32", "kind": "derivation_input",
     "evidence": []},
    {"id": "reference:44", "source": "db:32", "target": "db:52", "kind": "derivation_output",
     "evidence": []},
]
#: What books.name feeds: the transform and the view column on the map, one column beyond it.
DOWNSTREAM = [
    {"id": "db:31", "member_kind": "transform", "member_name": "book_name",
     "schema": "risk", "name": "exposure", "object_type": "view"},
    {"id": "db:51", "member_kind": "column", "member_name": "book_name",
     "schema": "risk", "name": "exposure", "object_type": "view"},
    {"id": "db:99", "member_kind": "column", "member_name": "label",
     "schema": "report", "name": "book_report", "object_type": "view"},
]


def _member(member: int, kind: str, name: str, obj: str, obj_node: int, depth: int,
            upstream: bool, downstream: bool, ordinal: int = 0, expression: str = "") -> dict:
    return {"id": f"db:{member}", "member_kind": kind, "name": name, "data_type": "text",
            "ordinal": ordinal, "role": "value" if kind == "transform" else None,
            "expression": expression or None, "schema": "risk", "object": obj,
            "object_type": "view" if obj == "exposure" else "table", "signature": "",
            "object_id": f"db:{obj_node}", "depth": depth, "upstream": upstream,
            "downstream": downstream}


#: books.name's whole lineage, as code.f_object_map_column_focus answers with no depth.
FOCUS = {"nodes": [
    _member(22, "column", "name", "books", 2, 0, False, False, ordinal=2),
    _member(31, "transform", "1.q.value.1.book_name", "exposure", 3, 1, False, True,
            expression="b.name"),
    _member(51, "column", "book_name", "exposure", 3, 2, False, True, ordinal=1),
], "edges": [
    {"id": "reference:41", "source": "db:22", "target": "db:31", "kind": "derivation_input",
     "evidence": [{"fact_source": "sql", "resolution": "resolved", "evidence": "b.name",
                   "statement_ordinal": 1}]},
    {"id": "reference:42", "source": "db:31", "target": "db:51", "kind": "derivation_output",
     "evidence": []},
]}


class _Control:
    """The shared Control's governed reads, as the map calls them."""

    def __init__(self) -> None:
        self.lineage_calls: list[tuple[list[tuple[str, str, str]], int | None]] = []
        self.focus_calls: list[tuple[int, int | None]] = []

    def object_map_column_focus(self, selected: int, depth: int | None) -> dict[str, Any]:
        self.focus_calls.append((selected, depth))
        # The focused column at depth 0, whichever of the two it is.
        start = f"db:{selected}"
        return {"nodes": [{**one, "depth": 0, "upstream": False, "downstream": False}
                          if one["id"] == start else one for one in FOCUS["nodes"]],
                "edges": FOCUS["edges"]}

    def object_map_table_context(self, *_: Any) -> list[dict[str, Any]]:
        return [CONTEXT]

    def object_map_column_lineage(self, catalog: str, provider: str,
                                  opened: list[tuple[str, str, str]],
                                  selected: int | None) -> dict[str, Any]:
        self.lineage_calls.append((list(opened), selected))
        drawn = set(opened)
        return {"columns": [c for c in COLUMNS
                            if (c["schema"], c["name"], c["object_type"]) in drawn],
                "transforms": TRANSFORMS, "edges": EDGES, "upstream": [],
                "downstream": DOWNSTREAM if selected == 22 else []}


def _map() -> tuple[ObjectMap, _Control]:
    held = ObjectMap({"kind": "database_schema", "connection": "rey apps",
                      "catalog": "rey_apps", "schema": "risk"}, SCHEMA_PROJECTION,
                     schema_objects(SCHEMA_READ, connection="rey apps", catalog="rey_apps",
                                    schema="risk"))
    control = _Control()
    held._control = control
    held._index_installation = "admin"
    held._database_provider = "postgresql"
    return held, control


def _node(projected: dict[str, Any], node_id: str) -> dict[str, Any]:
    return next(n for n in projected["nodes"] if n["id"] == node_id)


def _edges(projected: dict[str, Any], kind: str) -> dict[str, tuple[str, str]]:
    return {e["id"]: (e["source"], e["target"]) for e in projected["edges"] if e["kind"] == kind}


def test_collapsed_every_table_is_a_box_with_a_header_and_reads_no_lineage() -> None:
    held, control = _map()

    projected = held.projection()

    assert [(n["id"], n["kind"]) for n in projected["nodes"]] == [
        ("table:trades", "table"), ("table:books", "table"),
        ("header:table:trades", "header"), ("toggle:table:trades", "toggle"),
        ("links:table:trades", "links"), ("controls:table:trades", "controls"),
        ("header:table:books", "header"), ("toggle:table:books", "toggle"),
        ("links:table:books", "links"), ("controls:table:books", "controls")]
    assert _node(projected, "table:trades")["expandable"] is True
    assert _node(projected, "table:trades")["expanded"] is False
    assert _node(projected, "header:table:trades")["parent"] == "table:trades"
    # The +/- is its own node on the header's row.
    assert {key: _node(projected, "toggle:table:trades")[key]
            for key in ("parent", "beside", "expanded")} == {
        "parent": "table:trades", "beside": "header:table:trades", "expanded": False}
    assert _edges(projected, "foreign_key") == {
        "fk:trades:trades_book_fk:0": ("table:trades", "table:books")}
    assert control.lineage_calls == []


def test_the_plus_minus_shows_and_hides_its_columns_and_keeps_the_selection() -> None:
    held, _ = _map()
    held.projection()
    held.click("table:books")

    held.click("toggle:table:trades")
    assert held.projection()["focus"]["opened"] == ["trades"]
    assert _node(held.projection(), "toggle:table:trades")["expanded"] is True
    held.click("toggle:table:trades")

    assert held.projection()["focus"]["opened"] == []
    assert held._selected == "table:books"


def test_tables_views_and_functions_are_one_panel_with_a_qualified_name() -> None:
    held, _ = _map()
    held.focus(table="books", opened=["books"])

    projected = held.projection()

    headers = {n["parent"]: (n["label"], n["object_type"])
               for n in projected["nodes"] if n["kind"] == "header"}
    assert headers == {"table:books": ("risk.books", "table"),
                       "db:1": ("risk.trades", "table"),
                       "db:3": ("risk.exposure", "view"),
                       "db:4": ("risk.book_total", "function")}
    # A function has no columns to show: no +/-; the source file is no panel.
    assert {n["parent"] for n in projected["nodes"] if n["kind"] == "toggle"} == {
        "table:books", "db:1", "db:3"}
    assert _node(projected, "db:4")["expandable"] is True
    assert "expandable" not in _node(projected, "symbol:7")
    # Column rows carry no data type text.
    assert _node(projected, "db:22")["label"] == "name"


def test_a_selected_column_lights_the_objects_not_the_rows() -> None:
    held, _ = _map()
    held.focus(table="books", opened=["books", "db:3"])
    held.projection()

    held.click("db:22")
    projected = held.projection()

    # The object holding the column is the selection; an object holding a column
    # it feeds is lit with it; one holding nothing marked is subdued.
    assert _node(projected, "table:books")["highlight"] == ["focus"]
    assert _node(projected, "db:3")["highlight"] == ["first"]
    assert _node(projected, "db:1")["highlight"] == ["subdued"]


@pytest.mark.parametrize("opened, lineage", [
    ([], None),                                    # every object collapsed
    (["books", "db:1", "db:3"], None),             # expanded: lines end at rows
    (["books", "db:1", "db:3"], "transforms"),     # transforms drawn between
])
def test_a_header_click_lights_the_objects_genealogy_however_expanded(
    opened: list[str], lineage: str | None,
) -> None:
    """Books is joined to trades (a foreign key) and to exposure (exposure reads it,
    and derives from its column); exposure calls the function. The genealogy is
    between objects, whether a line ends at a panel or at one of its rows, and a
    transform between two objects is no generation of its own."""
    held, _ = _map()
    held.focus(table="books", opened=opened, **({"lineage": lineage} if lineage else {}))
    held.projection()

    held.click("header:table:books")
    projected = held.projection()

    marks = {n["id"]: n["highlight"] for n in projected["nodes"]
             if n["id"] in ("table:books", "db:1", "db:3", "db:4")}
    assert marks == {"table:books": ["focus", "first"], "db:1": ["first"],
                     "db:3": ["first"], "db:4": ["second"]}


def test_the_header_controls_sit_together_beside_the_title_in_one_pill() -> None:
    held, _ = _map()

    projected = held.projection()

    controls = {n["id"]: n for n in projected["nodes"]
                if n.get("parent") == "table:trades" and n["kind"] != "header"}
    # The +/- rightmost, the connections control to its left, the pill around them.
    assert (controls["toggle:table:trades"]["beside"], controls["toggle:table:trades"]["ordinal"]) \
        == ("header:table:trades", 0)
    assert (controls["links:table:trades"]["beside"], controls["links:table:trades"]["ordinal"]) \
        == ("header:table:trades", 1)
    assert controls["controls:table:trades"]["wraps"] == "header:table:trades"
    assert controls["links:table:trades"]["hidden"] is False


@pytest.mark.parametrize("opened", [[], ["books", "db:1", "db:3"]])
def test_hiding_an_objects_connections_hides_its_lines_only(opened: list[str]) -> None:
    """Every line into or out of the object -- at its panel or at its rows -- goes;
    the object, its rows and every other line stay; a second click restores them."""
    held, _ = _map()
    held.focus(table="books", opened=opened)
    before = held.projection()
    held.click("table:books")

    held.click("links:db:3")
    hidden = held.projection()

    nodes = {n["id"]: n for n in hidden["nodes"]}
    owner = {key: one.get("parent") or key for key, one in nodes.items()}
    lines = {e["id"]: (owner.get(e["source"], e["source"]), owner.get(e["target"], e["target"]))
             for e in hidden["edges"]}
    assert all("db:3" not in ends for ends in lines.values())
    assert {e["id"] for e in before["edges"]} - set(lines) == {
        e["id"] for e in before["edges"]
        if "db:3" in (owner.get(e["source"], e["source"]), owner.get(e["target"], e["target"]))}
    assert {n["id"] for n in before["nodes"]} == set(nodes)
    assert nodes["links:db:3"]["hidden"] is True
    assert held._selected == "table:books"
    assert hidden["focus"]["hidden_connections"] == ["db:3"]

    held.click("links:db:3")
    restored = held.projection()
    assert {e["id"] for e in restored["edges"]} == {e["id"] for e in before["edges"]}
    assert "hidden_connections" not in restored["focus"]


def _view_actions(held: ObjectMap) -> dict[str, dict[str, Any]]:
    return {one["role"]["act"]: one for one in held.projection()["actions"]}


def test_the_whole_views_plus_minus_is_the_declared_action_that_applies() -> None:
    """#1426 / #1151: the view's +/- -- expand while some table or view is closed,
    collapse once all are open -- each its role and the complete command, sent
    back unchanged."""
    held, _ = _map()
    held.focus(table="books")

    actions = _view_actions(held)
    assert actions["expand"]["role"]["on"] == "opened"
    assert "collapse" not in actions
    held.focus(**actions["expand"]["command"]["focus"])
    projected = held.projection()
    assert sorted(projected["focus"]["opened"]) == ["books", "db:1", "db:3"]
    assert {n["parent"] for n in projected["nodes"] if n["kind"] == "column"} == {
        "table:books", "db:1", "db:3"}

    actions = _view_actions(held)
    assert "expand" not in actions
    held.focus(**actions["collapse"]["command"]["focus"])
    assert held.projection()["focus"]["opened"] == []


def test_the_whole_views_connections_are_the_declared_action_that_applies() -> None:
    """Hide every object's connections while none is hidden; show them all once
    any is -- tables, views and functions alike."""
    held, _ = _map()
    held.focus(table="books")

    actions = _view_actions(held)
    assert actions["hide"]["role"]["on"] == "hidden_connections"
    held.focus(**actions["hide"]["command"]["focus"])
    projected = held.projection()
    assert sorted(projected["focus"]["hidden_connections"]) == [
        "db:1", "db:3", "db:4", "table:books"]
    assert projected["edges"] == []

    actions = _view_actions(held)
    assert "hide" not in actions
    held.focus(**actions["show"]["command"]["focus"])
    restored = held.projection()
    assert "hidden_connections" not in restored["focus"]
    assert restored["edges"]


def test_hidden_connections_must_be_objects_on_the_screen() -> None:
    held, _ = _map()

    with pytest.raises(ConfigError, match="draws no object"):
        held.focus(hidden_connections=["db:3"])


def test_a_click_on_the_header_selects_its_object_and_opens_nothing() -> None:
    held, _ = _map()
    held.projection()

    held.click("header:table:trades")

    assert held.projection()["focus"]["opened"] == []
    assert held._selected == "table:trades"


def test_expanded_rows_carry_primary_foreign_and_unique_markers() -> None:
    held, control = _map()
    held.focus(opened=["trades", "books"])

    projected = held.projection()

    marks = {n["id"]: (n["primary_key"], n["foreign_key"], n["unique"])
             for n in projected["nodes"] if n["kind"] == "column"}
    assert marks == {"db:11": (True, False, False), "db:12": (False, True, False),
                     "db:13": (False, False, False), "db:14": (False, False, True),
                     "db:21": (True, False, False), "db:22": (False, False, True)}
    assert _node(projected, "db:11")["parent"] == "table:trades"
    assert control.lineage_calls[0] == (
        [("risk", "trades", "table"), ("risk", "books", "table")], None)


@pytest.mark.parametrize("opened, ends", [
    (["trades", "books"], ("db:12", "db:21")),
    (["trades"], ("db:12", "table:books")),
    (["books"], ("table:trades", "db:21")),
])
def test_a_foreign_key_attaches_to_rows_or_boxes_as_each_end_is_expanded(
    opened: list[str], ends: tuple[str, str],
) -> None:
    held, _ = _map()
    held.focus(opened=opened)

    assert _edges(held.projection(), "foreign_key") == {
        "fk_column:table:trades:trades_book_fk:0:0": ends}


def test_column_flow_attaches_to_rows_or_boxes_through_hidden_transforms() -> None:
    held, _ = _map()

    held.focus(table="books", opened=["books", "db:3"])
    assert _edges(held.projection(), "derivation") == {
        "column_flow:db:22:db:51": ("db:22", "db:51"),
        "column_flow:db:13:db:52": ("db:1", "db:52")}
    held.focus(table="books", opened=["db:3"])
    assert _edges(held.projection(), "derivation") == {
        "column_flow:db:22:db:51": ("table:books", "db:51"),
        "column_flow:db:13:db:52": ("db:1", "db:52")}


def test_edges_converging_on_a_collapsed_box_stay_individually_identifiable() -> None:
    held, _ = _map()
    held.focus(table="books", opened=["books", "db:1"])

    assert _edges(held.projection(), "derivation") == {
        "column_flow:db:22:db:51": ("db:22", "db:3"),
        "column_flow:db:13:db:52": ("db:13", "db:3")}


def test_both_ends_collapsed_leave_the_object_map_unchanged() -> None:
    held, control = _map()
    held.focus(table="books")

    projected = held.projection()

    assert _edges(projected, "derivation") == {}
    assert {e["id"] for e in projected["edges"]} == {"reference:60", "reference:61",
                                                      "reference:62"}
    assert control.lineage_calls == []


def test_transforms_show_inside_an_expanded_object_and_stay_hidden_in_a_collapsed_one() -> None:
    held, _ = _map()

    held.focus(table="books", opened=["books", "db:3"], lineage="transforms")
    projected = held.projection()
    # A short label (role, and what it applies); the expression stays in the details.
    assert _node(projected, "db:31")["label"] == "book_name ← b.name"
    assert _node(projected, "db:31")["expression"] == "b.name"
    assert _edges(projected, "derivation") == {
        "reference:41": ("db:22", "db:31"), "reference:42": ("db:31", "db:51"),
        "reference:43": ("db:1", "db:32"), "reference:44": ("db:32", "db:52")}
    held.focus(table="books", opened=["books"], lineage="transforms")
    projected = held.projection()
    assert not any(n["kind"] == "transform" for n in projected["nodes"])
    assert _edges(projected, "derivation") == {"column_flow:db:22:db:51": ("db:22", "db:3")}


def test_a_selected_row_lights_what_it_feeds_collapsed_boxes_included() -> None:
    held, control = _map()
    held.focus(table="books", opened=["books"])
    held.projection()

    held.click("db:22")
    projected = held.projection()

    assert control.lineage_calls[-1][1] == 22
    assert _node(projected, "db:22")["highlight"] == ["focus"]
    assert _node(projected, "db:3")["highlight"] == ["downstream"]
    assert _node(projected, "header:table:books")["highlight"] == []
    assert _node(projected, "db:1")["highlight"] == ["subdued"]
    flow = next(e for e in projected["edges"] if e["id"] == "column_flow:db:22:db:51")
    assert flow["highlight"] == ["downstream"]
    assert ["Downstream", "1 on the map; 1 columns in 1 objects beyond it"] in (
        projected["details"]["rows"])


def _selected_columns(projected: dict[str, Any]) -> set[str]:
    return {n["id"] for n in projected["nodes"]
            if n["kind"] == "column" and n.get("highlight") == ["focus"]}


def test_a_selected_foreign_key_shows_its_columns_as_selected() -> None:
    """A selected line's columns read as selected columns (#1426): both ends, where drawn."""
    held, _ = _map()
    held.focus(opened=["trades", "books"])
    held.projection()

    held.click("fk_column:table:trades:trades_book_fk:0:0")

    assert _selected_columns(held.projection()) == {"db:12", "db:21"}


def test_a_selected_line_into_a_collapsed_object_shows_only_its_drawn_column() -> None:
    held, _ = _map()
    held.focus(opened=["trades"])
    held.projection()

    held.click("fk_column:table:trades:trades_book_fk:0:0")
    projected = held.projection()

    # books stays collapsed: the selection expands nothing.
    assert _selected_columns(projected) == {"db:12"}
    assert projected["focus"]["opened"] == ["trades"]
    assert not any(n.get("parent") == "table:books" and n["kind"] == "column"
                   for n in projected["nodes"])


def test_a_selected_column_flow_shows_its_two_columns_as_selected() -> None:
    held, _ = _map()
    held.focus(table="books", opened=["books", "db:3"])
    held.projection()

    held.click("column_flow:db:22:db:51")

    assert _selected_columns(held.projection()) == {"db:22", "db:51"}


def test_a_selected_line_that_names_no_column_shows_none() -> None:
    held, _ = _map()
    held.focus(table="books", opened=["books"])
    held.projection()

    held.click("reference:61")

    assert _selected_columns(held.projection()) == set()


def test_a_selected_foreign_key_pair_is_described() -> None:
    held, _ = _map()
    held.focus(opened=["trades"])
    held.projection()

    held.click("fk_column:table:trades:trades_book_fk:0:0")

    assert held.projection()["details"]["rows"] == [
        ["Relationship", "foreign_key"], ["Source", "risk.trades.book_id"],
        ["Target", "risk.books.book_id"], ["Constraint", "trades_book_fk"]]


def test_a_header_right_click_and_drill_act_on_its_object() -> None:
    held, _ = _map()
    held.projection()

    opened = held.context_click("header:table:trades")
    assert opened["identity"]["name"] == "trades"
    held.double_click("header:table:books")
    assert held.projection()["focus"]["table"] == "books"


def test_the_transforms_toggle_carries_the_focus_a_press_sends() -> None:
    held, _ = _map()
    held.focus(table="books", opened=["books"])

    toggle = held.projection()["toggles"][0]
    assert (toggle["label"], toggle["pressed"]) == ("Transforms", False)
    held.focus(**toggle["focus"])
    toggle = held.projection()["toggles"][0]

    assert toggle["pressed"] is True
    assert toggle["focus"] == {"opened": ["books"], "table": "books", "lineage": "columns"}


def _focused() -> tuple[ObjectMap, _Control]:
    """books.name double-clicked on books' Table Map, books expanded."""
    held, control = _map()
    held.focus(table="books", opened=["books"])
    held.projection()
    held.double_click("db:22")
    return held, control


def test_a_double_clicked_column_focuses_its_lineage_to_three_graph_edges() -> None:
    held, control = _focused()

    projected = held.projection()

    assert control.focus_calls == [(22, None)]
    assert projected["focus"] == {"opened": ["books"], "column": "db:22"}
    assert [step["label"] for step in projected["path"]] == ["risk", "books", "risk.books.name"]
    # The approved visual: boxes of their name and participating columns, the
    # transform a small node on the line between them, read left to right.
    assert [(n["id"], n["kind"], n.get("parent"), n["label"]) for n in projected["nodes"]] == [
        ("db:2", "table", None, "risk.books"),
        ("header:db:2", "header", "db:2", "risk.books"),
        ("db:3", "view", None, "risk.exposure"),
        ("header:db:3", "header", "db:3", "risk.exposure"),
        ("db:22", "column", "db:2", "name"), ("db:51", "column", "db:3", "book_name"),
        ("db:31", "transform", None, "book_name ← b.name")]
    assert {e["id"]: (e["source"], e["target"]) for e in projected["edges"]} == {
        "reference:41": ("db:22", "db:31"), "reference:42": ("db:31", "db:51")}
    # Each header names its object's type: the icon that defines it.
    assert [(n["id"], n["object_type"]) for n in projected["nodes"] if n["kind"] == "header"] == [
        ("header:db:2", "table"), ("header:db:3", "view")]
    assert projected["direction"] == "RIGHT"
    assert [one["label"] for one in projected["legend"]] == [
        "Table / View", "Column", "Transform", "Column derivation (flow)"]


def test_a_transform_on_no_path_between_drawn_columns_is_not_drawn() -> None:
    held, control = _map()
    held.focus(table="books", opened=["books"])
    held.projection()
    dead_end = {"id": "db:33", "member_kind": "transform", "name": "x", "data_type": None,
                "ordinal": 0, "role": "filter", "expression": "b.name IS NOT NULL",
                "schema": "risk", "object": "exposure", "object_type": "view",
                "signature": "", "object_id": "db:3", "depth": 1, "upstream": False,
                "downstream": True}
    FOCUS["nodes"].append(dead_end)
    FOCUS["edges"].append({"id": "reference:49", "source": "db:22", "target": "db:33",
                           "kind": "derivation_input", "evidence": []})
    try:
        held.double_click("db:22")
        projected = held.projection()
    finally:
        FOCUS["nodes"].remove(dead_end)
        FOCUS["edges"].pop()

    assert "db:33" not in {n["id"] for n in projected["nodes"]}
    assert "reference:49" not in {e["id"] for e in projected["edges"]}


def test_right_click_on_a_line_or_transform_states_its_expression_and_evidence() -> None:
    held, _ = _focused()
    held.projection()

    held.context_click("reference:41")
    line = held.projection()["details"]["rows"]
    held.context_click("db:31")
    transform = held.projection()["details"]["rows"]

    assert line[:5] == [["Relationship", "derivation_input"], ["Source", "risk.books.name"],
                        ["Target", "book_name ← b.name"], ["Expression", "b.name"],
                        ["Evidence", "sql · resolved · b.name"]]
    assert transform[:4] == [["Transform", "book_name ← b.name"], ["Expression", "b.name"],
                             ["Object", "risk.exposure"], ["Depth", 1]]
    assert ["Evidence", "sql · resolved · b.name"] in transform


def test_the_focused_lineage_is_marked_and_described_from_the_column() -> None:
    held, _ = _focused()

    projected = held.projection()

    marks = {n["id"]: n["highlight"] for n in projected["nodes"]}
    assert (marks["db:22"], marks["db:31"], marks["db:51"]) == (
        ["focus"], ["downstream"], ["downstream"])
    assert {e["id"]: e["highlight"] for e in projected["edges"]} == {
        "reference:41": ["downstream"], "reference:42": ["downstream"]}
    assert projected["details"]["rows"] == [
        ["Column", "name"], ["Type", "text"], ["Object", "risk.books"], ["Depth", 0],
        ["Lineage", "0 upstream, 1 downstream columns in its lineage"]]


def test_on_the_focused_lineage_a_row_refocuses_a_header_selects_and_the_path_returns() -> None:
    held, control = _focused()
    held.projection()

    held.click("header:db:3")
    assert held.projection()["details"]["rows"][0] == ["Object", "risk.exposure"]
    held.double_click("db:51")
    projected = held.projection()
    assert control.focus_calls[-1] == (51, None)
    assert projected["path"][-1]["label"] == "risk.exposure.book_name"

    held.focus(**projected["path"][1]["focus"])
    assert held.projection()["focus"] == {"opened": ["books"], "table": "books"}


@pytest.mark.parametrize("expression, head", [
    ("jsonb_agg(jsonb_build_object('k', t.k) ORDER BY t.sort_order)", "jsonb_agg"),
    ("jsonb_build_object('is_enabled', d.is_enabled)", "jsonb_build_object"),
    ("ai.ai", "ai.ai"),
    ("CAST(b.label AS varchar)", "b.label"),
    ("COALESCE(tp.profile_key, dp.profile_key)", "COALESCE"),
    ("CASE WHEN a THEN 1 ELSE 2 END", "CASE"),
    ("a + b", ""),
    ("not ( valid", ""),
])
def test_a_transform_label_names_what_its_expression_applies(expression: str, head: str) -> None:
    from rey_lib.db.postgres_references import expression_head

    assert expression_head(expression) == head


@pytest.mark.parametrize("key, role, expression, label", [
    # control.application_installation_vw, as #1425 keys its transforms.
    ("1.q.cte:definition.value.2.ai", "value",
     "jsonb_build_object('is_enabled', d.is_enabled)", "definition.ai ← jsonb_build_object"),
    ("1.q.cte:task.value.2.tasks", "value", "jsonb_agg(jsonb_build_object('k', t.k))",
     "task.tasks ← jsonb_agg"),
    ("1.q.value.12.ai", "value", "ai.ai", "ai ← ai.ai"),
    ("1.q.sub:s.value.1.total", "value", "a + b", "s.total ← value"),
    ("1.q.cte:x.l.value.1.n", "value", "max(n)", "x.n ← max"),
    ("1.q.join.1.x", "join", "a.id = b.id", "join"),
    ("not a key", "value", "max(n)", "value → max"),
])
def test_a_transform_is_labelled_by_where_it_sits_and_what_it_produces(
    key: str, role: str, expression: str, label: str,
) -> None:
    from rey_lib.inferred_architecture.object_map import _transform_label

    assert _transform_label(role, expression, key) == label


def test_an_explained_row_is_a_member_named_and_selected_on_either_screen() -> None:
    """#1399 / #1426: a drawn column row is a member of the running map."""
    held, _ = _map()
    held.focus(table="books", opened=["books"])
    held.projection()

    member = held.explain_member("db:22")

    assert member == {"member": "db:22", "schema": "risk", "object": "books",
                      "object_type": "table", "column": "name"}
    assert held._selected == "db:22"
    assert held.explain_member("table:books") is None
    assert held.explain_member("header:table:books") is None
    focused, _ = _focused()
    focused.projection()
    assert focused.explain_member("db:51")["object"] == "exposure"


def test_column_lineage_reads_the_selected_columns_lineage_with_expressions_and_evidence() -> None:
    held, control = _map()
    held.focus(table="books", opened=["books"])
    held.projection()
    held.explain_member("db:22")

    lineage = held.column_lineage()

    assert control.focus_calls[-1] == (22, None)
    assert lineage["column"] == {"id": "db:22", "schema": "risk", "object": "books",
                                 "object_type": "table", "column": "name"}
    assert lineage["depth"] is None
    assert [(c["column"], c["depth"], c["downstream"]) for c in lineage["columns"]] == [
        ("name", 0, False), ("book_name", 2, True)]
    assert lineage["transforms"] == [{
        "id": "db:31", "label": "book_name ← b.name", "role": "value", "expression": "b.name",
        "schema": "risk", "object": "exposure", "object_type": "view", "depth": 1,
        "upstream": False, "downstream": True}]
    assert lineage["steps"][0] == {
        "source": "db:22", "target": "db:31", "kind": "derivation_input",
        "evidence": [{"fact_source": "sql", "resolution": "resolved", "evidence": "b.name",
                      "statement_ordinal": 1}]}


def test_column_lineage_refuses_without_a_selected_column() -> None:
    from rey_lib.errors.error_utils import AppError

    held, _ = _map()
    held.projection()

    with pytest.raises(AppError, match="no selected column"):
        held.column_lineage()


def test_the_focus_read_is_the_governed_binding() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    control = Control.__new__(Control)
    control._call_rows = lambda name, variables, required=False: (  # type: ignore[method-assign]
        calls.append((name, variables)) or [])

    answer = control.object_map_column_focus(22, 3)

    assert calls == [("get_object_map_column_focus", {"selected": 22, "depth": 3})]
    assert answer == {"nodes": [], "edges": []}


def test_the_reach_read_is_the_governed_binding() -> None:
    """#1429: a database object's full reach, through its procedure-map binding."""
    calls: list[tuple[str, dict[str, Any]]] = []
    control = Control.__new__(Control)
    control._call_rows = lambda name, variables, required=False: (  # type: ignore[method-assign]
        calls.append((name, variables)) or [])

    answer = control.database_object_reach("rey_apps", "postgres", "risk", "books", "table",
                                           "", 100)

    assert calls == [("get_database_object_reach", {
        "catalog": "rey_apps", "provider": "postgres", "schema": "risk", "name": "books",
        "object_type": "table", "signature": "", "limit": 100})]
    # Not indexed is said as none, never as an empty reach.
    assert answer is None


def test_a_transform_is_a_member_explained_with_its_full_bounded_lineage() -> None:
    """#1429: a drawn transform is explained from the map, its whole reach read."""
    held, control = _map()
    held.focus(table="books", opened=["books", "db:3"], lineage="transforms")
    held.projection()
    asked: list[tuple[int, int | None]] = []
    # exposure.total's transform: trades.amount feeds it, it feeds exposure.total.
    control.object_map_column_focus = lambda selected, depth: asked.append((selected, depth)) or {
        "nodes": [
            _member(32, "transform", "exposure.total", "exposure", 3, 0, False, False,
                    expression="sum(t.amount)"),
            _member(13, "column", "amount", "trades", 1, 1, True, False, ordinal=3),
            _member(52, "column", "total", "exposure", 3, 1, False, True, ordinal=2)],
        "edges": [{"id": "reference:43", "source": "db:13", "target": "db:32",
                   "kind": "derivation_input", "evidence": []},
                  {"id": "reference:44", "source": "db:32", "target": "db:52",
                   "kind": "derivation_output", "evidence": []}]}

    member = held.explain_member("db:32")
    lineage = held.column_lineage(1)

    assert member["member"] == "db:32" and member["member_kind"] == "transform"
    assert member["expression"] == "sum(t.amount)"
    # No depth: the full reach, whatever the map draws.
    assert asked == [(32, None)]
    assert lineage["member"]["member_kind"] == "transform"
    assert lineage["totals"] == {"columns": 2, "transforms": 1, "steps": 2}
    assert lineage["max_depth"] == 1
    # Bounded to the tool's limit, never undercounted.
    assert lineage["limit"] == 1 and lineage["truncated"] is True
    assert len(lineage["columns"]) == 1 and len(lineage["steps"]) == 1


def test_a_line_is_a_member_explained_with_its_relationship_and_its_ends_lineage() -> None:
    held, _ = _map()
    held.focus(table="books", opened=["books", "db:3"])
    held.projection()

    member = held.explain_member("column_flow:db:22:db:51")
    lineage = held.column_lineage()

    assert member["member"] == "column_flow:db:22:db:51"
    assert member["member_kind"] == "relationship" and member["relationship"] == "derivation"
    assert lineage["line"]["relationship"] == "derivation"
    assert lineage["line"]["through"] == ["db:31"]
    assert [end["column"]["id"] for end in lineage["ends"]] == ["db:22", "db:51"]
    assert all(end["truncated"] is False for end in lineage["ends"])


def test_an_object_header_is_no_member_and_goes_on_to_its_owner() -> None:
    held, _ = _map()
    held.focus(table="books", opened=["books"])
    held.projection()

    assert held.explain_member("header:table:books") is None


def test_only_tables_and_views_on_the_screen_open() -> None:
    held, _ = _map()

    with pytest.raises(ConfigError, match="no table or view"):
        held.focus(opened=["db:3"])


def test_the_read_is_the_governed_binding() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    control = Control.__new__(Control)
    control._call_rows = lambda name, variables, required=False: (  # type: ignore[method-assign]
        calls.append((name, variables)) or [])

    answer = control.object_map_column_lineage("rey_apps", "postgresql",
                                               [("risk", "trades", "table")], 7)

    assert calls == [("get_object_map_column_lineage", {
        "catalog": "rey_apps", "provider": "postgresql", "schemas": ["risk"],
        "names": ["trades"], "object_types": ["table"], "selected": 7})]
    assert answer == {"columns": [], "transforms": [], "edges": [], "upstream": [],
                      "downstream": []}
