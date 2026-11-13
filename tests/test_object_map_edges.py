"""Code-map edge gestures reuse the live map, index evidence and file opener."""
from types import SimpleNamespace

import pytest

from rey_lib.errors.error_utils import AppError
from rey_lib.inferred_architecture import object_map as module
from tests.test_object_map import _architecture


SITE = {"repository_key": "rey_lib", "relative_path": "rey_lib/example.py",
        "source_line": 42, "source_column": 4, "source_identity": "Alpha.run",
        "target_identity": "Beta.read", "edge_kind": "call", "resolution": "resolved"}


def _held(monkeypatch, rows):
    """The shared Control's governed edge-evidence read (#1360): its calls recorded."""
    calls = []

    class Control:
        def object_map_edge_evidence(self, sources, targets):
            calls.append(("object_map_edge_evidence",
                          {"sources": list(sources), "targets": list(targets)}))
            return rows

    held = _architecture()
    held._index_connection = SimpleNamespace(handle=lambda: "index")
    held._control = Control()
    return held, calls


def test_single_click_highlights_only_the_edge_and_its_endpoints():
    held = _architecture()
    held.focus(level="area", area_id=10)
    before = dict(held.projection()["focus"])
    held.click("relationship:1:2")
    projected = held.projection()
    assert projected["focus"] == before
    assert projected["selected"] == "relationship:1:2"
    nodes = {n["id"]: n["highlight"] for n in projected["nodes"]}
    assert nodes["claim:1"] == nodes["claim:2"] == ["first"]
    assert nodes["claim:4"] == []
    edges = {e["id"]: e["highlight"] for e in projected["edges"]}
    assert edges["relationship:1:2"] == ["focus", "first"]
    assert edges["relationship:1:3:area:20"] == []


def test_double_click_shows_indexed_call_sites_and_right_click_uses_the_file_opener(monkeypatch):
    held, calls = _held(monkeypatch, [SITE])
    held.focus(level="area", area_id=10)
    held.double_click("relationship:1:2")
    assert calls[0] == ("object_map_edge_evidence", {"sources": [1], "targets": [2]})
    projected = held.projection()
    assert projected["focus"] == {"level": "area", "area_id": 10}
    assert "Alpha.run → Beta.read" in projected["details"]["rows"][-1][1]
    assert "rey_lib/rey_lib/example.py:42" in projected["details"]["rows"][-1][1]
    assert held.inspect()["selected"] == "relationship:1:2"
    assert held.context_click("relationship:1:2") == {"identity": {
        "kind": "configured_file", "root": "{apps}",
        "relative_path": "rey_lib/rey_lib/example.py"}}
    assert len(calls) == 1


def test_area_link_inspection_reads_only_the_claim_pairs_mapped_by_that_link(monkeypatch):
    held, calls = _held(monkeypatch, [SITE])
    held.double_click("link:10:20")
    assert calls[0] == ("object_map_edge_evidence", {"sources": [1, 2], "targets": [3]})
    assert ["Source", "Core"] in held.projection()["details"]["rows"]
    assert ["Target", "Edge"] in held.projection()["details"]["rows"]


def test_missing_evidence_is_stated_and_never_replaced_by_a_guessed_owner_file(monkeypatch):
    held, _ = _held(monkeypatch, [])
    held.focus(level="area", area_id=10)
    held.double_click("relationship:1:2")
    assert ["Evidence", "No indexed call/reference locations are available."] in held.projection()["details"]["rows"]
    with pytest.raises(AppError, match="no indexed source"):
        held.context_click("relationship:1:2")


def test_multiple_source_files_refuse_an_arbitrary_first_file(monkeypatch):
    held, _ = _held(monkeypatch, [SITE, {**SITE, "relative_path": "rey_lib/other.py"}])
    held.focus(level="area", area_id=10)
    held.double_click("relationship:1:2")
    before = held.projection()
    with pytest.raises(AppError, match="2 source files"):
        held.context_click("relationship:1:2")
    assert held.projection() == before


def test_nonvisible_edges_are_not_accepted_as_arbitrary_index_queries(monkeypatch):
    held, calls = _held(monkeypatch, [SITE])
    for gesture in (held.click, held.double_click, held.context_click):
        with pytest.raises(AppError, match="no node"):
            gesture("relationship:1:2")  # The estate currently shows area links only.
    assert calls == []


def test_schema_edge_click_projects_relationship_details_and_highlights():
    from tests.test_object_map import _schema
    held = _schema()
    edge = held.projection()["edges"][0]
    held.click(edge["id"])
    projected = held.projection()
    assert projected["selected"] == edge["id"]
    assert ["Relationship", "foreign_key"] in projected["details"]["rows"]
    assert ["Columns", "run_id"] in projected["details"]["rows"]
    assert next(e for e in projected["edges"] if e["id"] == edge["id"])["highlight"] == ["focus", "first"]
    held.click(None)
    assert held.projection()["details"] is None


@pytest.mark.parametrize("gesture", ["click", "double_click", "context_click"])
def test_schema_relationship_gestures_select_the_relationship_not_a_node(gesture):
    from tests.test_object_map import _schema
    held = _schema()
    edge = held.projection()["edges"][0]
    before = held.projection()["focus"]
    assert getattr(held, gesture)(edge["id"]) is None
    projected = held.projection()
    assert projected["selected"] == edge["id"]
    assert projected["focus"] == before
    assert ["Relationship", "foreign_key"] in projected["details"]["rows"]
