"""The ObjectMap: one live map object, two projections (#1119, #1120).

No database: the five object-map reads and the schema read are stand-ins. What
is proved is the object -- what it reads, how a click or double-click changes it, what each
focused screen projects, and that opening a claim answers the governed subject
and nothing else.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable

import pytest

from rey_lib.errors.error_utils import ConfigError
from rey_lib.inferred_architecture import object_map as module
from rey_lib.inferred_architecture import (
    CLAIM_NODE_TYPE,
    OBJECT_PROJECTION,
    SCHEMA_PROJECTION,
    ObjectMap,
)


def _claim(claim_id: int, name: str, outcome: str | None = "supported") -> dict[str, Any]:
    return {"canonical_object_id": claim_id, "object_key": f"rey_lib:{name}",
            "owner_qualified_name": name, "owner_subject_id": f"rey_lib/{name}.py",
            "evaluation_outcome": outcome, "responsibility": f"{name} things"}


READ: dict[str, Any] = {
    "claims": [_claim(1, "Alpha"), _claim(2, "Beta"), _claim(3, "Gamma", None), _claim(4, "Delta")],
    # 1 -> 2 inside area 10; 1 -> 3 leaves for area 20; 4 is placed nowhere.
    "relationships": [
        {"canonical_object_id": 1, "linked_canonical_object_id": 2, "is_direct": True},
        {"canonical_object_id": 1, "linked_canonical_object_id": 3, "is_direct": False},
        {"canonical_object_id": 4, "linked_canonical_object_id": 2, "is_direct": True},
    ],
    "placements": [
        {"canonical_object_id": 1, "inferred_architecture_area_id": 10},
        {"canonical_object_id": 2, "inferred_architecture_area_id": 10},
        {"canonical_object_id": 3, "inferred_architecture_area_id": 20},
    ],
    "areas": [
        {"inferred_architecture_area_id": 10, "area_label": "Core", "claim_count": 2,
         "internal_relationship_count": 1, "crossing_relationship_count": 1},
        {"inferred_architecture_area_id": 20, "area_label": "Edge", "claim_count": 1,
         "internal_relationship_count": 0, "crossing_relationship_count": 1},
        {"inferred_architecture_area_id": 30, "area_label": "Empty", "claim_count": 0,
         "internal_relationship_count": 0, "crossing_relationship_count": 0},
    ],
    "area_links": [
        {"from_area_id": 10, "to_area_id": 20, "relationship_count": 1,
         "direct_relationship_count": 0},
    ],
}


def _architecture() -> ObjectMap:
    return ObjectMap({"kind": "inferred_architecture", "installation": "admin"},
                     OBJECT_PROJECTION, {name: [dict(r) for r in rows] for name, rows in READ.items()})


def _ids(items: list[dict[str, Any]]) -> list[str]:
    return [item["id"] for item in items]


# -- reading ------------------------------------------------------------------

def test_the_architecture_map_reads_its_five_statements_on_the_hosts_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[str] = []

    class _Query:
        def __init__(self, statement: str) -> None:
            self.statement = statement

        def rows(self, handle: Any) -> tuple[dict[str, Any], ...]:
            assert handle == "handle"
            ran.append(self.statement)
            return ()

    monkeypatch.setattr(module, "DatabaseQuery", _Query)
    connection = SimpleNamespace(handle=lambda: "handle")

    held = ObjectMap.for_architecture(connection, "admin")

    assert held.subject == {"kind": "inferred_architecture", "installation": "admin"}
    assert held.projection_kind == OBJECT_PROJECTION
    assert [view for statement in ran for view in (
        "object_map_claim_vw", "object_map_relationship_vw", "object_map_placement_vw",
        "object_map_area_vw", "object_map_area_link_vw") if view in statement] == [
        "object_map_claim_vw", "object_map_relationship_vw", "object_map_placement_vw",
        "object_map_area_vw", "object_map_area_link_vw"]


def test_a_schema_map_reads_the_schema_as_the_database_domains_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple[Any, ...]] = []

    def read(handle: Any, connection: str, catalog: str, schema: str) -> Any:
        asked.append((handle, connection, catalog, schema))
        return SimpleNamespace(to_dict=lambda: {"tables": []})

    monkeypatch.setattr(module.DatabaseSchema, "read", read)
    connection = SimpleNamespace(handle=lambda: "handle", name="rey apps")

    held = ObjectMap.for_schema(connection, "rey_apps", "control")

    assert asked == [("handle", "rey apps", "rey_apps", "control")]
    assert held.subject == {"kind": "database_schema", "connection": "rey apps",
                            "catalog": "rey_apps", "schema": "control"}
    assert held.projection_kind == SCHEMA_PROJECTION


# -- the object projection -----------------------------------------------------

def test_the_map_opens_on_the_estate() -> None:
    projected = _architecture().projection()

    assert projected["focus"] == {"level": "estate"}
    assert _ids(projected["nodes"]) == ["area:10", "area:20", "area:30"]
    assert [n["role"] for n in projected["nodes"]] == ["", "", "empty"]
    assert projected["edges"] == [{"id": "link:10:20", "kind": "link", "source": "area:10",
                                   "target": "area:20", "weight": 1, "direct": False,
                                   "highlight": []}]
    assert projected["selected"] is None
    assert projected["path"] == [{"value": "estate", "label": "Estate",
                                  "focus": {"level": "estate"}}]
    assert projected["details"] is None


def test_double_clicking_an_area_drills_into_its_claims_and_their_links() -> None:
    held = _architecture()

    assert held.double_click("area:10") is None
    projected = held.projection()

    assert projected["focus"] == {"level": "area", "area_id": 10}
    nodes = {n["id"]: n for n in projected["nodes"]}
    # Inside: 1 and 2. 1 -> 3 leaves to area 20, drawn as that area; 4 is placed
    # nowhere, so it is drawn as itself, outside.
    assert set(nodes) == {"claim:1", "claim:2", "area:20", "claim:4"}
    assert nodes["area:20"]["role"] == "elsewhere"
    assert nodes["claim:4"]["role"] == "outside"
    assert _ids(projected["edges"]) == [
        "relationship:1:2", "relationship:1:3:area:20", "relationship:4:2:claim:4"]
    assert [step["value"] for step in projected["path"]] == ["estate", "area:10"]


def test_double_clicking_a_claim_drills_into_it_with_its_neighbours_and_details() -> None:
    held = _architecture()
    held.double_click("area:10")

    assert held.double_click("claim:1") is None
    projected = held.projection()

    assert projected["focus"] == {"level": "object", "claim_id": 1, "area_id": 10}
    roles = {n["id"]: n["role"] for n in projected["nodes"]}
    assert roles == {"claim:1": "focus", "claim:2": "", "claim:3": ""}
    assert [step["value"] for step in projected["path"]] == ["estate", "area:10", "object:1"]
    assert ["Object", "rey_lib:Alpha"] in projected["details"]["rows"]


# -- interaction (#1099): click selects, double-click drills, right-click opens a claim --

def test_a_first_click_selects_and_the_projection_highlights_its_connections() -> None:
    held = _architecture()
    held.double_click("area:10")

    assert held.click("claim:1") is None
    projected = held.projection()

    assert projected["selected"] == "claim:1"
    marks = {n["id"]: n["highlight"] for n in projected["nodes"]}
    # 1 links to 2 and (through area 20) out; 4 links to 2 -- the second ring.
    assert marks == {"claim:1": ["focus", "first"], "claim:2": ["first"],
                     "area:20": ["first"], "claim:4": ["second"]}
    edges = {e["id"]: e["highlight"] for e in projected["edges"]}
    assert edges == {"relationship:1:2": ["first"], "relationship:1:3:area:20": ["first"],
                     "relationship:4:2:claim:4": ["second"]}


@pytest.mark.parametrize("drill", [[], ["area:10"], ["area:10", "claim:1"]],
                         ids=["from the estate", "from an area", "from the claim itself"])
def test_a_right_click_on_a_claim_opens_the_same_record_at_any_focus(drill: list[str]) -> None:
    held = _architecture()
    for node in drill:
        held.double_click(node)

    assert held.context_click("claim:1") == {"node_type": CLAIM_NODE_TYPE, **READ["claims"][0]}


def test_a_right_click_on_a_held_non_claim_changes_nothing() -> None:
    held = _architecture()
    before = held.projection()

    assert held.context_click("area:10") is None
    assert held.projection() == before


def test_a_click_never_opens_however_often_it_repeats() -> None:
    held = _architecture()
    held.double_click("area:10")

    assert held.click("claim:1") is None
    assert held.click("claim:1") is None
    assert held.projection()["selected"] == "claim:1"


def test_a_click_on_another_node_moves_the_selection() -> None:
    held = _architecture()
    held.double_click("area:10")
    held.click("claim:1")

    held.click("claim:2")

    assert held.projection()["selected"] == "claim:2"


def test_a_click_on_no_node_clears_the_selection() -> None:
    held = _architecture()
    held.click("area:10")

    assert held.click(None) is None
    assert held.projection()["selected"] is None
    assert held.click("area:10") is None  # selected again, not opened


def test_a_double_click_drills_clears_the_selection_and_never_opens() -> None:
    held = _architecture()
    held.click("claim:1")

    assert held.double_click("claim:1") is None
    assert held.projection()["focus"]["level"] == "object"
    assert held.projection()["selected"] is None
    assert held.double_click("claim:1") is None  # a focused claim is never opened by drilling


@pytest.mark.parametrize("node", ["claim:99", "area:99", "claim:x", "table:run"])
def test_a_click_or_double_click_on_a_node_the_map_does_not_hold_is_refused(node: str) -> None:
    held = _architecture()

    with pytest.raises(ConfigError, match="no node"):
        held.click(node)
    with pytest.raises(ConfigError, match="no node"):
        held.double_click(node)
    with pytest.raises(ConfigError, match="no node"):
        held.context_click(node)
    assert held.projection()["selected"] is None


def test_an_opened_claim_answers_only_what_it_is() -> None:
    record = _architecture().context_click("claim:1")

    # Its governed record -- nothing about signing, hosts, actions or panels.
    assert not {"reference", "application", "action", "target", "panel"} & set(record)


def test_the_map_has_no_separate_record_lookup() -> None:
    assert not hasattr(_architecture(), "record")


def test_the_path_moves_the_focus_back() -> None:
    held = _architecture()
    held.double_click("area:10")
    held.double_click("claim:1")

    held.focus(level="estate")

    assert held.projection()["focus"] == {"level": "estate"}


@pytest.mark.parametrize("focus", [
    {"level": "area", "area_id": 99}, {"level": "object", "claim_id": 99}, {"level": "x"},
])
def test_a_focus_on_nothing_the_map_holds_is_refused(focus: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        _architecture().focus(**focus)


# -- the schema projection -------------------------------------------------------

SCHEMA_READ = {"tables": [
    {"identity": {"schema": "control", "name": "run"},
     "columns": [{"name": "run_id", "type": "bigint", "nullable": False, "ordinal": 1},
                 {"name": "note", "type": "text", "nullable": True, "ordinal": 2}],
     "primary_key": {"name": "run_pk", "columns": ["run_id"]},
     "foreign_keys": []},
    {"identity": {"schema": "control", "name": "run_log"},
     "columns": [{"name": "run_id", "type": "bigint", "nullable": False, "ordinal": 1}],
     "primary_key": None,
     "foreign_keys": [
         {"name": "run_log_run_fk", "columns": ["run_id"], "referenced_schema": "control",
          "referenced_table": "run", "referenced_columns": ["run_id"]},
         {"name": "run_log_installation_fk", "columns": ["installation_id"],
          "referenced_schema": "code", "referenced_table": "installation",
          "referenced_columns": ["installation_id"]},
     ]},
]}


def _schema() -> ObjectMap:
    return ObjectMap({"kind": "database_schema", "connection": "rey apps",
                      "catalog": "rey_apps", "schema": "control"},
                     SCHEMA_PROJECTION, SCHEMA_READ)


def test_a_schema_projects_its_tables_and_foreign_keys_closed() -> None:
    projected = _schema().projection()

    assert projected["projection"] == SCHEMA_PROJECTION
    assert _ids(projected["nodes"]) == ["table:run", "table:run_log", "external:code.installation"]
    assert all(not n.get("columns") for n in projected["nodes"])
    assert [(e["source"], e["target"]) for e in projected["edges"]] == [
        ("table:run_log", "table:run"), ("table:run_log", "external:code.installation")]


def test_double_clicking_a_table_opens_it_to_its_columns_and_again_closes_it() -> None:
    held = _schema()

    held.double_click("table:run")
    run = next(n for n in held.projection()["nodes"] if n["id"] == "table:run")
    assert run["columns"] == [
        {"name": "run_id", "type": "bigint", "nullable": False, "primary_key": True},
        {"name": "note", "type": "text", "nullable": True, "primary_key": False}]

    held.double_click("table:run")
    assert held.projection()["focus"] == {"opened": []}


def test_a_schema_map_has_no_claims_to_open() -> None:
    with pytest.raises(ConfigError, match="no node"):
        _schema().context_click("claim:1")


@pytest.mark.parametrize("held, screen", [(_architecture, "object"), (_schema, "schema")])
def test_a_map_states_no_presentation_and_carries_its_own_subject_and_screen(
    held: Callable[[], ObjectMap], screen: str,
) -> None:
    """#1215: presentation is the governed subject's; the map's screen is its own state."""
    held_map = held()
    projected = held_map.projection()

    assert "panel_type" not in projected
    assert not hasattr(ObjectMap, "PANEL_TYPE")
    assert projected["projection"] == screen
    assert projected["subject"] == held_map.subject


def test_a_schema_map_selects_a_table_and_never_opens() -> None:
    held = _schema()

    assert held.click("table:run") is None
    assert held.context_click("table:run") is None
    projected = held.projection()
    assert projected["selected"] == "table:run"
    assert {n["id"]: n["highlight"] for n in projected["nodes"]}["table:run_log"] == ["first"]


def test_object_map_projects_its_legend_at_every_focus():
    obj = _architecture()
    legend = obj.projection()["legend"]
    assert {entry["label"] for entry in legend} >= {
        "Architecture area", "Object — supported", "Second-order links",
    }
    obj.double_click("area:10")
    assert obj.projection()["legend"] == legend
    obj.double_click("claim:1")
    assert obj.projection()["legend"] == legend


# -- inspect (#1199 S4): the live map's own semantic state, for the focused scope --

#: What inspect may never carry: presentation, or the mapped things' own figures.
_NOT_MAP_STATE = {"panel_type", "legend", "highlight", "details", "nodes", "edges",
                  "projection", "outcome", "claim_count", "internal_relationship_count",
                  "crossing_relationship_count", "weight", "direct", "columns",
                  "referenced_columns"}


def _keys(value: Any) -> set[str]:
    """Every key anywhere inside ``value``."""
    if isinstance(value, dict):
        return set(value) | set().union(*(_keys(one) for one in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(one) for one in value)) if value else set()
    return set()


def test_inspect_states_the_map_its_focus_and_the_estate_as_map_identities() -> None:
    assert _architecture().inspect() == {
        "subject": {"kind": "inferred_architecture", "installation": "admin"},
        "map": OBJECT_PROJECTION,
        "focus": {"level": "estate"},
        "path": [{"value": "estate", "label": "Estate"}],
        "selected": None,
        "elements": [
            {"id": "area:10", "kind": "area", "area_id": 10, "label": "Core", "role": ""},
            {"id": "area:20", "kind": "area", "area_id": 20, "label": "Edge", "role": ""},
            {"id": "area:30", "kind": "area", "area_id": 30, "label": "Empty", "role": "empty"},
        ],
        "relations": [{"id": "link:10:20", "kind": "link", "source": "area:10",
                       "target": "area:20"}],
    }


def test_inspect_follows_the_live_focus_and_carries_no_presentation_or_source_figures() -> None:
    held = _architecture()
    held.double_click("area:10")
    area = held.inspect()
    held.double_click("claim:1")
    claim = held.inspect()

    assert area["focus"] == {"level": "area", "area_id": 10}
    assert {e["id"]: e["role"] for e in area["elements"]} == {
        "claim:1": "", "claim:2": "", "area:20": "elsewhere", "claim:4": "outside"}
    assert [r["id"] for r in area["relations"]] == [
        "relationship:1:2", "relationship:1:3:area:20", "relationship:4:2:claim:4"]
    assert claim["path"] == [{"value": "estate", "label": "Estate"},
                             {"value": "area:10", "label": "Core"},
                             {"value": "object:1", "label": "Alpha"}]
    assert {e["id"]: e["role"] for e in claim["elements"]} == {
        "claim:1": "focus", "claim:2": "", "claim:3": ""}
    assert not (_keys(area) | _keys(claim)) & _NOT_MAP_STATE


def test_inspect_reports_the_selection_under_the_maps_own_rule() -> None:
    held = _architecture()
    held.click("area:10")
    assert held.inspect()["selected"] == "area:10"

    # A drill clears the selection; the next inspection says so.
    held.double_click("area:10")
    assert held.inspect()["selected"] is None


def test_inspect_moves_nothing() -> None:
    held = _architecture()
    held.click("area:10")
    before = held.projection()

    held.inspect()

    assert held.projection() == before


def test_inspect_of_a_schema_relates_its_tables_and_never_carries_their_columns() -> None:
    held = _schema()
    held.double_click("table:run")
    inspected = held.inspect()

    assert inspected["map"] == SCHEMA_PROJECTION
    assert inspected["focus"] == {"opened": ["run"]}
    assert inspected["path"] == [{"value": "schema", "label": "control"}]
    assert inspected["elements"] == [
        {"id": "table:run", "kind": "table", "table": "run", "label": "run"},
        {"id": "table:run_log", "kind": "table", "table": "run_log", "label": "run_log"},
        {"id": "external:code.installation", "kind": "external", "label": "code.installation"},
    ]
    assert [(r["source"], r["target"]) for r in inspected["relations"]] == [
        ("table:run_log", "table:run"), ("table:run_log", "external:code.installation")]
    assert not _keys(inspected) & _NOT_MAP_STATE
