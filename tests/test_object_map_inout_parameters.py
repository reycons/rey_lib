"""An INOUT parameter is shown under both Inputs and Outputs (#1451).

It is ONE parameter: its own row (under Inputs) keeps its identity and its lineage
lines; the row under Outputs is a display row whose id is derived for presentation
and resolves to the same parameter, so selection and explain act identically from
either. Each group is numbered on its own, so no two rows share a position.
"""
from __future__ import annotations

from typing import Any

from rey_lib.inferred_architecture.object_map import SCHEMA_PROJECTION, ObjectMap
from tests import test_object_map_column_lineage as lineage
from tests.schema_objects import schema_objects

#: book_total's parameters, in call order: an input, an INOUT and an output.
PARAMETERS = [
    {**lineage._column(61, "book_total", "function", "p_book", 1), "mode": "IN"},
    {**lineage._column(62, "book_total", "function", "io_total", 2), "mode": "INOUT"},
    {**lineage._column(63, "book_total", "function", "o_sum", 3), "mode": "OUT"},
]
#: One lineage line into the INOUT parameter: books.book_id feeds it.
INTO_INOUT = {"id": "reference:71", "source": "db:21", "target": "db:62",
              "kind": "derivation_input", "evidence": []}


class _Control(lineage._Control):
    """The lineage fixture's governed reads, with book_total's parameters."""

    def object_map_column_lineage(self, catalog: str, provider: str,
                                  opened: list[tuple[str, str, str]],
                                  selected: int | None) -> dict[str, Any]:
        read = super().object_map_column_lineage(catalog, provider, opened, selected)
        drawn = set(opened)
        return {**read,
                "columns": read["columns"] + [p for p in PARAMETERS
                                              if (p["schema"], p["name"], p["object_type"]) in drawn],
                "edges": read["edges"] + [INTO_INOUT]}


def inout_map() -> ObjectMap:
    """books' Table Map with books and the function book_total opened."""
    held = ObjectMap({"kind": "database_schema", "connection": "rey apps",
                      "catalog": "rey_apps", "schema": "risk"}, SCHEMA_PROJECTION,
                     schema_objects(lineage.SCHEMA_READ, connection="rey apps", catalog="rey_apps",
                                    schema="risk"))
    held._control = _Control()
    held._index_installation = "admin"
    held._database_provider = "postgresql"
    held.focus(table="books", opened=["books", "db:4"])
    # Projected once, as serve does before any gesture: the drawn rows are what a
    # gesture is checked against.
    held.projection()
    return held


def _rows(projected: dict[str, Any]) -> list[tuple[str, str, int]]:
    """book_total's rows in panel order: id, label and position."""
    return [(one["id"], one.get("label", ""), one["ordinal"])
            for one in sorted((n for n in projected["nodes"]
                               if n.get("parent") == "db:4" and n["kind"] in ("group", "column")),
                              key=lambda n: n["ordinal"])]


def test_an_inout_parameter_is_under_inputs_and_outputs_each_group_numbered_on_its_own() -> None:
    assert _rows(inout_map().projection()) == [
        ("inputs:db:4", "Inputs", 1), ("db:61", "p_book", 2), ("db:62", "io_total", 3),
        ("outputs:db:4", "Outputs", 4), ("outputs:db:62", "io_total", 5), ("db:63", "o_sum", 6),
    ]


def test_the_outputs_row_is_the_same_parameter_and_keeps_its_inout_classification() -> None:
    nodes = {n["id"]: n for n in inout_map().projection()["nodes"]}
    shown, own = nodes["outputs:db:62"], nodes["db:62"]
    assert shown["member"] == "db:62" and "member" not in own
    assert shown["mode"] == own["mode"] == "INOUT"
    assert {k: v for k, v in shown.items() if k not in ("id", "member", "ordinal", "highlight")} == {
        k: v for k, v in own.items() if k not in ("id", "ordinal", "highlight")}


def test_lineage_stays_on_the_parameters_own_row_with_no_duplicate_line() -> None:
    edges = inout_map().projection()["edges"]
    ends = [(e.get("from"), e.get("to")) for e in edges if "db:62" in (e.get("from"), e.get("to"))
            or "outputs:db:62" in (e["source"], e["target"])]
    assert ends == [("db:21", "db:62")]


def test_selecting_either_row_selects_the_one_parameter_and_marks_both() -> None:
    for row in ("db:62", "outputs:db:62"):
        held = inout_map()
        held.click(row)
        projected = held.projection()
        assert projected["selected"] == "db:62", row
        marks = {n["id"]: n.get("highlight") for n in projected["nodes"]}
        assert marks["db:62"] == marks["outputs:db:62"] == ["focus"], row


def test_explain_and_right_click_answer_the_same_from_either_row() -> None:
    answers = []
    for row in ("db:62", "outputs:db:62"):
        held = inout_map()
        answers.append((held.explain_member(row), held._selected, held.context_click(row), held._selected))
    assert answers[0] == answers[1]
    assert answers[0][0]["member"] == "db:62"
