"""IndexedDatabaseObject: a database object as the code index knows it (backlog 696).

The one owner of the code-index reads about a database object. Its reads are
governed functions read through Control (#541): asserted against a recording
Control -- the whole identity each read is asked by (signature included), the
direction, the record shapes, and that "not indexed" stays distinct from
"nothing related".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rey_lib.db import database_objects
from rey_lib.repository_map import indexed_database_object as module
from rey_lib.repository_map.indexed_database_object import (
    IndexedCodeReach,
    IndexedDatabaseObject,
    IndexedRelation,
)

_ROUTINE = IndexedDatabaseObject(
    schema="control", name="end_step", object_type="procedure", signature="integer, text",
)
_IDENTITY = ("control", "end_step", "procedure", "integer, text")


class _Control:
    """Answers each governed read with what it is given, recording what was asked."""

    def __init__(self, indexed: bool = False, rows: list[dict[str, Any]] | None = None) -> None:
        self.indexed = indexed
        self.rows = rows or []
        self.asked: list[tuple[str, tuple]] = []

    def indexed_object_exists(self, *identity: Any) -> bool:
        self.asked.append(("exists", identity))
        return self.indexed

    def indexed_object_related(self, *arguments: Any) -> list[dict[str, Any]]:
        self.asked.append(("related", arguments))
        return self.rows

    def indexed_object_reached_by_code(self, *identity: Any) -> list[dict[str, Any]]:
        self.asked.append(("reached", identity))
        return self.rows


def test_every_read_is_asked_by_the_whole_identity_signature_included() -> None:
    control = _Control()

    _ROUTINE.is_indexed(control)
    _ROUTINE.related(control, reverse=False)
    _ROUTINE.reached_by_code(control)

    assert control.asked == [("exists", _IDENTITY), ("related", (*_IDENTITY, False)),
                             ("reached", _IDENTITY)]


def test_not_indexed_is_not_the_same_answer_as_nothing_related() -> None:
    assert _ROUTINE.is_indexed(_Control(indexed=False)) is False
    assert _ROUTINE.is_indexed(_Control(indexed=True)) is True
    assert _ROUTINE.related(_Control(indexed=True), reverse=True) == ()


def test_each_direction_asks_its_own_side_of_the_edge() -> None:
    control = _Control()

    _ROUTINE.related(control, reverse=False)
    _ROUTINE.related(control, reverse=True)

    assert [arguments[-1] for _read, arguments in control.asked] == [False, True]


def test_related_objects_arrive_as_records() -> None:
    control = _Control(rows=[{
        "schema_name": "control", "object_name": "batch_step", "object_type": "table",
        "signature": None, "definition": None, "reference_kind": "writes",
    }])

    (relation,) = _ROUTINE.related(control, reverse=False)

    assert relation == IndexedRelation(
        schema="control", name="batch_step", object_type="table", signature="",
        definition="", reference_kind="writes",
    )


def test_reaching_code_arrives_as_records() -> None:
    control = _Control(rows=[{
        "installation": "local", "map_name": "control", "repository_key": "rey_lib",
        "relative_path": "rey_lib/control/control.py", "qualified_name": "Control.end",
        "binding_name": "end_step",
    }])

    (reach,) = _ROUTINE.reached_by_code(control)

    assert reach == IndexedCodeReach(
        installation="local", map_name="control", repository_key="rey_lib",
        relative_path="rey_lib/control/control.py", qualified_name="Control.end",
        binding_name="end_step",
    )


def test_an_object_without_a_signature_is_asked_with_an_empty_one() -> None:
    control = _Control()

    IndexedDatabaseObject(schema="control", name="batch", object_type="table").is_indexed(control)

    assert control.asked[0][1][-1] == ""


def test_it_offers_no_general_query_and_writes_no_sql() -> None:
    """#541: its reads are governed functions through Control; no statement is written here."""
    public = {name for name in vars(IndexedDatabaseObject) if not name.startswith("_")}
    source = Path(module.__file__).read_text(encoding="utf-8")

    assert {"is_indexed", "related", "reached_by_code"} <= public
    assert not public & {"query", "execute", "read", "sql", "rows"}
    assert set(module.__all__) == {"IndexedCodeReach", "IndexedDatabaseObject", "IndexedRelation"}
    assert "SELECT" not in source and "execute_sql" not in source


def test_controls_reads_are_the_governed_bindings() -> None:
    """#541: each read is its control-map binding, asked by the whole identity."""
    from rey_lib.control.control import Control

    calls: list[tuple[str, dict]] = []
    control = Control.__new__(Control)
    control._call_rows = lambda name, variables, required=False: (  # type: ignore[method-assign]
        calls.append((name, variables)) or [])

    assert control.indexed_object_exists(*_IDENTITY) is False
    assert control.indexed_object_related(*_IDENTITY, True) == []
    assert control.indexed_object_reached_by_code(*_IDENTITY) == []

    identity = {"schema": "control", "name": "end_step", "object_type": "procedure",
                "signature": "integer, text"}
    assert calls == [("get_indexed_object", identity),
                     ("get_indexed_object_related", {**identity, "reverse": True}),
                     ("get_indexed_object_reached_by_code", identity)]


def test_the_database_objects_do_not_depend_on_the_code_index() -> None:
    source = Path(database_objects.__file__).read_text(encoding="utf-8")

    assert "repository_map" not in source
    assert "code.db_" not in source
