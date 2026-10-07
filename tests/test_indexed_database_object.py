"""IndexedDatabaseObject: a database object as the code index knows it (backlog 696).

The one owner of the code-index reads about a database object. Asserted against
a recording adapter: each read's statement, the whole identity it is bound to
(signature included), the record shapes, and that "not indexed" stays distinct
from "nothing related".
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


class _Adapter:
    """Answers each statement with the rows it is given, recording what was asked."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.asked: list[tuple[str, dict, str]] = []

    def execute_sql(self, _conn: Any, statement: str, params: dict, mode: str) -> list:
        self.asked.append((statement, dict(params), mode))
        return self.rows


def test_every_read_is_bound_to_the_whole_identity_signature_included() -> None:
    adapter = _Adapter()

    _ROUTINE.is_indexed(object(), adapter)
    _ROUTINE.related(object(), reverse=False, adapter=adapter)
    _ROUTINE.reached_by_code(object(), adapter)

    expected = {
        "schema": "control", "object": "end_step", "object_type": "procedure",
        "signature": "integer, text",
    }
    assert [params for _statement, params, _mode in adapter.asked] == [expected] * 3
    assert {mode for _s, _p, mode in adapter.asked} == {"dataset_result"}


def test_not_indexed_is_not_the_same_answer_as_nothing_related() -> None:
    nothing = _Adapter([])

    assert _ROUTINE.is_indexed(object(), nothing) is False
    assert _ROUTINE.is_indexed(object(), _Adapter([{"?column?": 1}])) is True
    assert _ROUTINE.related(object(), reverse=True, adapter=nothing) == ()


def test_each_direction_asks_its_own_side_of_the_edge() -> None:
    adapter = _Adapter()

    _ROUTINE.related(object(), reverse=False, adapter=adapter)
    _ROUTINE.related(object(), reverse=True, adapter=adapter)

    depends_on, used_by = (statement for statement, _p, _m in adapter.asked)
    assert "mine.db_node_id  = r.source_node_id" in depends_on
    assert "mine.db_node_id  = r.target_node_id" in used_by


def test_related_objects_arrive_as_records() -> None:
    adapter = _Adapter([{
        "schema_name": "control", "object_name": "batch_step", "object_type": "table",
        "signature": None, "definition": None, "reference_kind": "writes",
    }])

    (relation,) = _ROUTINE.related(object(), reverse=False, adapter=adapter)

    assert relation == IndexedRelation(
        schema="control", name="batch_step", object_type="table", signature="",
        definition="", reference_kind="writes",
    )


def test_reaching_code_arrives_as_records() -> None:
    adapter = _Adapter([{
        "installation": "local", "map_name": "control", "repository_key": "rey_lib",
        "relative_path": "rey_lib/control/control.py", "qualified_name": "Control.end",
        "binding_name": "end_step",
    }])

    (reach,) = _ROUTINE.reached_by_code(object(), adapter)

    assert reach == IndexedCodeReach(
        installation="local", map_name="control", repository_key="rey_lib",
        relative_path="rey_lib/control/control.py", qualified_name="Control.end",
        binding_name="end_step",
    )
    statement = adapter.asked[0][0]
    assert "code.db_binding_vw" in statement and "b.resolves_now" in statement


def test_an_object_without_a_signature_is_asked_with_an_empty_one() -> None:
    adapter = _Adapter()

    IndexedDatabaseObject(schema="control", name="batch", object_type="table").is_indexed(
        object(), adapter,
    )

    assert adapter.asked[0][1]["signature"] == ""


def test_its_statements_are_private_and_it_offers_no_general_query() -> None:
    public = {name for name in vars(IndexedDatabaseObject) if not name.startswith("_")}

    assert {"is_indexed", "related", "reached_by_code"} <= public
    assert not public & {"query", "execute", "read", "sql", "rows"}
    assert set(module.__all__) == {"IndexedCodeReach", "IndexedDatabaseObject", "IndexedRelation"}


def test_the_database_objects_do_not_depend_on_the_code_index() -> None:
    source = Path(database_objects.__file__).read_text(encoding="utf-8")

    assert "repository_map" not in source
    assert "code.db_" not in source
