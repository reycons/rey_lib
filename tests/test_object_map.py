"""The ObjectMap: one live map object, two projections (#1119, #1120).

No database: the five object-map reads and the schema read are stand-ins. What
is proved is the object -- what it reads, how a press moves its focus, what each
focused screen projects, and that opening a claim answers the governed subject
and nothing else.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

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
                                   "target": "area:20", "weight": 1, "direct": False}]
    assert projected["path"] == [{"value": "estate", "label": "Estate",
                                  "focus": {"level": "estate"}}]
    assert projected["details"] is None


def test_pressing_an_area_focuses_its_claims_and_their_links() -> None:
    held = _architecture()

    assert held.press("area:10") is None
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


def test_pressing_a_claim_focuses_it_with_its_neighbours_and_details() -> None:
    held = _architecture()
    held.press("area:10")

    assert held.press("claim:1") is None
    projected = held.projection()

    assert projected["focus"] == {"level": "object", "claim_id": 1, "area_id": 10}
    roles = {n["id"]: n["role"] for n in projected["nodes"]}
    assert roles == {"claim:1": "focus", "claim:2": "", "claim:3": ""}
    assert [step["value"] for step in projected["path"]] == ["estate", "area:10", "object:1"]
    assert ["Object", "rey_lib:Alpha"] in projected["details"]["rows"]


def test_pressing_the_focused_claim_opens_it_as_its_governed_subject() -> None:
    held = _architecture()
    held.press("claim:1")

    opened = held.press("claim:1")

    assert opened == {"node_type": CLAIM_NODE_TYPE, **READ["claims"][0]}
    # It answers what the claim is -- nothing about signing, hosts or panels.
    assert not {"reference", "application", "action", "target"} & set(opened)


def test_open_claim_refuses_a_claim_the_map_does_not_hold() -> None:
    with pytest.raises(ConfigError, match="no claim"):
        _architecture().open_claim(99)


def test_the_path_moves_the_focus_back() -> None:
    held = _architecture()
    held.press("area:10")
    held.press("claim:1")

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


def test_pressing_a_table_opens_it_to_its_columns_and_again_closes_it() -> None:
    held = _schema()

    held.press("table:run")
    run = next(n for n in held.projection()["nodes"] if n["id"] == "table:run")
    assert run["columns"] == [
        {"name": "run_id", "type": "bigint", "nullable": False, "primary_key": True},
        {"name": "note", "type": "text", "nullable": True, "primary_key": False}]

    held.press("table:run")
    assert held.projection()["focus"] == {"opened": []}


def test_a_schema_map_has_no_claims_to_open() -> None:
    with pytest.raises(ConfigError, match="no claim"):
        _schema().open_claim(1)
