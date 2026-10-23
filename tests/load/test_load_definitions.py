"""Source, Transform and Target each state what they are (#1199 S5).

Each ``definition()`` is that object's own configuration state -- the kind in
force and its configuration, plus the Target's write policy and what Source and
Target still need -- read under the object's own act. Nothing is read to answer
it: no records, no columns, no governed facts; nothing said under a kind that is
not in force; nothing an editor offers. The Transform carries no ``missing``,
because what it lacks is only known against its Source's columns.
"""

from __future__ import annotations

import json
from typing import Any

from rey_lib.load import load_operation
from rey_lib.load.source import Source
from rey_lib.load.target import Target
from rey_lib.load.transform import Transform

#: What a definition may never carry: an editor's offer, its projection's extras,
#: or anything read from data.
_NOT_DEFINITION = {"kinds", "fields", "values", "choices", "operations", "governed",
                   "modes", "columns", "source_columns", "target_columns", "column_ids",
                   "column_ordinals", "column_settings", "output_missing", "http_request",
                   "outbound", "field_operations", "missing_columns"}


class _Unreadable:
    """A held collaborator that fails the moment anything reads it."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"definition() read its held collaborator ({name})")


def test_a_source_states_its_kind_configuration_and_what_it_still_needs() -> None:
    source = Source.from_declaration({
        "values": {"source-connection": "warehouse", "file": "/data/elsewhere.csv"},
        "selected": "database",
    })

    definition = source.definition()

    assert json.loads(json.dumps(definition)) == definition
    # In force only: the file said under the file kind is not part of what it is.
    assert definition == {
        "kind": "database",
        "configuration": {"source-connection": "warehouse", "statement": None},
        "missing": ["statement"],
    }


def test_a_file_source_that_does_not_exist_still_answers_unread() -> None:
    source = Source.from_declaration({"values": {"file": "/no/such/file.csv"}})

    assert source.definition() == {
        "kind": "file",
        "configuration": {"file": "/no/such/file.csv", "file-type": None,
                          "file-mutation-id": None},
        "missing": [],
    }


def test_a_target_states_its_kind_configuration_write_policy_and_what_it_needs() -> None:
    target = Target.from_declaration({
        "values": {"connection": "warehouse", "table": "public.orders", "replace": True,
                   "out-file": "/out/never.csv"},
        "selected": "database",
    })

    definition = target.definition()

    assert json.loads(json.dumps(definition)) == definition
    assert definition["kind"] == "database"
    assert definition["write_policy"] == "replace"
    assert definition["missing"] == []
    assert definition["configuration"]["table"] == "public.orders"
    assert "out-file" not in definition["configuration"]
    assert not set(definition) & _NOT_DEFINITION


def test_a_target_never_reads_its_table_to_answer(monkeypatch: Any) -> None:
    def no_table(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("definition() read the destination table")

    monkeypatch.setattr(load_operation, "_destination_identity", no_table)
    target = Target.from_declaration({"values": {"connection": "c", "table": "t"}})

    assert target.definition()["kind"] == "database"


def test_a_transform_states_its_kind_configuration_and_declaration() -> None:
    declared = {"columns": [{"name": "id", "source": "id"}]}
    transform = Transform.from_declaration({
        "values": {"transform": json.dumps(declared), "transform-file": "/never.yaml"},
        "selected": "declaration",
    })

    definition = transform.definition()

    assert json.loads(json.dumps(definition)) == definition
    assert definition == {
        "kind": "declaration",
        "configuration": {"transform": json.dumps(declared)},
        "declaration": declared,
    }


def test_a_transform_never_reads_its_held_source_or_target() -> None:
    transform = Transform.from_declaration({})
    transform._source = _Unreadable()
    transform._target = _Unreadable()

    definition = transform.definition()

    assert definition == {"kind": "identity", "configuration": {}, "declaration": None}
    assert "missing" not in definition
