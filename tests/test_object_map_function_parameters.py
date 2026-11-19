"""A function opens like a view on the database map (#1426): its parameters in
two groups by their mode, and every row's data-type category for its icon."""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.db.database_objects import (
    DatabaseParameter, data_type_category, parameter_role,
)
from rey_lib.db.postgres_references import VALUE, carries_forward
from rey_lib.inferred_architecture.object_map import ObjectMap, _transforms_anything


@pytest.mark.parametrize(("data_type", "category"), [
    ("text", "text"), ("character varying", "text"), ("character varying(40)", "text"),
    ("varchar(255)", "text"), ("nvarchar", "text"),
    ("integer", "number"), ("bigint", "number"), ("numeric(10,2)", "number"),
    ("double precision", "number"), ("real", "number"), ("decimal", "number"),
    ("date", "temporal"), ("timestamp with time zone", "temporal"),
    ("time without time zone", "temporal"), ("interval", "temporal"),
    ("boolean", "boolean"), ("bool", "boolean"),
    ("json", "json"), ("jsonb", "json"),
    ("uuid", "identifier"), ("uniqueidentifier", "identifier"),
    ("bytea", "other"), ("text[]", "other"), ("USER-DEFINED", "other"),
    ("", "other"), (None, "other"),
])
def test_a_data_type_is_classified_by_its_name_alone(data_type: str | None,
                                                      category: str) -> None:
    """Each reported type falls in its category; an array or unknown type is other."""
    assert data_type_category(data_type) == category


def test_a_parameter_mode_has_one_role_wherever_it_is_read() -> None:
    """IN and VARIADIC are inputs, OUT and TABLE outputs; the parameter agrees."""
    assert [parameter_role(mode) for mode in ("IN", "VARIADIC", "OUT", "TABLE", "INOUT")] == [
        "input", "input", "output", "output", "input_output"]
    assert DatabaseParameter("p", 1, "text", "TABLE", False).role == "output"


def _row(owner: str, name: str, ordinal: int, mode: str | None) -> dict[str, Any]:
    """One opened row as the map builds it, its mode where it is a parameter."""
    return {"id": f"db:{owner}.{name}", "kind": "column", "parent": owner, "label": name,
            "ordinal": ordinal, **({"mode": mode} if mode else {})}


def test_a_functions_inputs_are_grouped_at_the_top_and_its_outputs_below() -> None:
    """Inputs under their heading, then outputs under theirs, by mode and call order."""
    rows = [_row("f", "o_total", 3, "TABLE"), _row("f", "p_from", 1, "IN"),
            _row("f", "o_name", 4, "OUT"), _row("f", "p_names", 2, "VARIADIC"),
            _row("t", "c", 1, None)]
    columns = {row["id"]: row for row in rows}
    identities = {"f": ("s", "f", "function"), "t": ("s", "t", "table")}

    headings = ObjectMap._parameter_groups(columns, identities)

    assert [(one["id"], one["kind"], one["parent"], one["label"], one["ordinal"])
            for one in headings] == [("inputs:f", "group", "f", "Inputs", 1),
                                     ("outputs:f", "group", "f", "Outputs", 4)]
    ordered = sorted((one for one in rows if one["parent"] == "f"),
                     key=lambda one: one["ordinal"])
    assert [(one["label"], one["ordinal"]) for one in ordered] == [
        ("p_from", 2), ("p_names", 3), ("o_total", 5), ("o_name", 6)]
    # A table's columns keep their own order; it has no groups.
    assert columns["db:t.c"]["ordinal"] == 1


def test_a_group_with_no_parameters_has_no_heading() -> None:
    """A function that takes nothing shows its outputs alone."""
    columns = {"db:f.o": _row("f", "o", 1, "TABLE")}

    headings = ObjectMap._parameter_groups(columns, {"f": ("s", "f", "function")})

    assert [one["id"] for one in headings] == ["outputs:f"]
    assert columns["db:f.o"]["ordinal"] == 2


@pytest.mark.parametrize(("expression", "carried"), [
    ("a.ai_experience_id", True), ("ai_experience_id", True), ("s.t.c", True),
    ("(a.c)::text", False), ("lower(a.c)", False), ("a.c + 1", False),
    ("COALESCE(a.c, b.c)", False), ("'x'", False), ("a.*", False), ("not sql (", False),
])
def test_a_transform_that_only_carries_a_column_forward_is_known(expression: str,
                                                                  carried: bool) -> None:
    """A bare column reference carries the column forward; anything that changes it does not."""
    assert carries_forward(expression) is carried


def test_the_map_draws_no_transform_for_a_carried_forward_column() -> None:
    """Carried forward: no transform drawn. A real transform, and any non-value role: drawn."""
    assert not _transforms_anything(VALUE, "a.c")
    assert _transforms_anything(VALUE, "lower(a.c)")
    assert _transforms_anything("join", "a.c")
    assert _transforms_anything(VALUE, None)


def _hiding(focus: dict[str, Any]) -> list[str]:
    """The lines left once an object's lines of each kind are hidden, by id.

    Two panels a and b with a row each, a transform t, and an external x: one
    header line between the panels, one to the external, one column line, and
    one through the transform.
    """
    nodes = [{"id": "a", "kind": "view"}, {"id": "b", "kind": "table"},
             {"id": "x", "kind": "external"}, {"id": "t", "kind": "transform"},
             {"id": "a.c", "kind": "column", "parent": "a"},
             {"id": "b.c", "kind": "column", "parent": "b"}]
    edges = [{"id": "a-b", "source": "a", "target": "b"},
             {"id": "b-x", "source": "b", "target": "x"},
             {"id": "a.c-b.c", "source": "a.c", "target": "b.c"},
             {"id": "a.c-t", "source": "a.c", "target": "t"}]
    one = ObjectMap.__new__(ObjectMap)
    one._focus = focus
    one._drop_hidden_connections(nodes, edges)
    return [edge["id"] for edge in edges]


def test_an_objects_header_lines_and_column_lines_hide_on_their_own() -> None:
    """Hiding header lines keeps column lines, and hiding column lines keeps header lines."""
    assert _hiding({}) == ["a-b", "b-x", "a.c-b.c", "a.c-t"]
    assert _hiding({"hidden_relationships": ["a"]}) == ["b-x", "a.c-b.c", "a.c-t"]
    assert _hiding({"hidden_connections": ["a"]}) == ["a-b", "b-x"]
    assert _hiding({"hidden_relationships": ["a", "b"], "hidden_connections": ["b"]}) == [
        "a.c-t"]
