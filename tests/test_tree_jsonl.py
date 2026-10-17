"""A configured JSONL file, as canonical node records -- moved with the feeder (backlog 1058).

What this object owns is the shape: it is handed a record, asks the JSONL
reader for the level that record names, and returns what came back. It parses
nothing, selects nothing and signs nothing -- selection is the reader's, and
these hold that it stays there.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.errors.error_utils import ConfigError
from rey_lib.tree import jsonl
from rey_lib.tree.jsonl import JsonlDataObject

#: The context the feeder is handed: its installation's PathResolver authorizes
#: the file. Here it answers with where the file would be, as the resolver the
#: Console's tests stood in for did.
_CTX = SimpleNamespace(paths=SimpleNamespace(
    authorize_configured_file=lambda root, relative_path: f"{root}/{relative_path}",
))

#: What the reader answers with. A JsonlRecord carries the parsed object and
#: the line it came from; only the object crosses into a row.
RECORDS = [
    {"record_id": "n:1", "parent_id": None, "label": "rey_lib",
     "node_type": "repository", "has_children": True, "repository": "rey_lib"},
    {"record_id": "n:2", "parent_id": None, "label": "ftp_sync",
     "node_type": "repository", "has_children": False, "repository": "ftp_sync"},
]

#: What a root is handed. ``path`` carries the configured token as stored, and
#: tree_definitions resolves it to a place before any feeder is asked -- so what
#: arrives here is already a location, never a token or a root name.
DECLARATION = {
    "path": "/resolved/context",
    "file": "03_repository_map.architecture.generated.jsonl",
    "parent_field": "parent_id",
    "key_field": "record_id",
    "label_field": "label",
    "node_type_field": "node_type",
    "has_children_field": "has_children",
    "filters": {"record_type": "architecture_node"},
}

SOURCE = {
    "node_key": "4:7",
    "label": "Architecture",
    "tree_object_key": "jsonl",
    "tree_configuration": DECLARATION,
}


class _Row:
    """One JsonlRecord, as the reader returns it."""

    def __init__(self, record: dict[str, Any]) -> None:
        self.record = record
        self.line_number = 1


@pytest.fixture
def asked(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record what the reader was asked, and answer with RECORDS."""
    calls: list[dict[str, Any]] = []

    def _read(path: Any, *, filters: Any = None, fields: Any = None) -> list[_Row]:
        calls.append({"path": path, "filters": filters})
        return [_Row(record) for record in RECORDS]

    monkeypatch.setattr("rey_lib.tree.jsonl.read_jsonl_file", _read)
    return calls


# -- the level is a filter ---------------------------------------------------


def test_the_level_is_asked_of_the_reader(asked) -> None:
    """Selection is the reader's. The feeder composes a filter and hands it over."""
    JsonlDataObject().children(_CTX, SOURCE)

    assert asked == [{
        "path": "/resolved/context/03_repository_map.architecture.generated.jsonl",
        "filters": {"record_type": "architecture_node", "parent_id": None},
    }]


def test_a_root_selects_on_a_null_parent(asked) -> None:
    """A root declares no parent, so the level is the records naming none.

    Absent and null are not the same thing to the reader, and a root is null.
    """
    JsonlDataObject().children(_CTX, SOURCE)

    assert asked[0]["filters"]["parent_id"] is None


def test_a_child_level_selects_on_its_parent(asked) -> None:
    """The declaration advances, exactly as a folder's path does."""
    child = JsonlDataObject().children(_CTX, SOURCE)[0]

    JsonlDataObject().children(_CTX, child)

    assert asked[1]["filters"] == {
        "record_type": "architecture_node", "parent_id": "n:1",
    }


def test_the_source_is_carried_forward_unchanged(asked) -> None:
    """A record found inside a source opens by the rule its parent opened by."""
    child = JsonlDataObject().children(_CTX, SOURCE)[0]

    assert child["tree_object_key"] == "jsonl"
    assert child["tree_configuration"] == {
        **DECLARATION, "root": "/resolved/context", "parent": "n:1",
    }


def test_a_child_carries_the_place_rather_than_the_token(asked) -> None:
    """The tree resolves a root's token once; a child is handed the answer.

    Expanding it again here would be a second resolution beside the one
    tree_definitions performs, and a child would be the level that broke when
    the token moved.
    """
    child = JsonlDataObject().children(_CTX, SOURCE)[0]

    assert child["tree_configuration"]["root"] == "/resolved/context"
    JsonlDataObject().children(_CTX, child)

    assert asked[1]["path"] == (
        "/resolved/context/03_repository_map.architecture.generated.jsonl"
    )


# -- what a row carries ------------------------------------------------------


def test_a_record_becomes_a_row_carrying_its_own_fields(asked) -> None:
    """A mapping binds to a column the row exposes, and the row is the record."""
    row = JsonlDataObject().children(_CTX, SOURCE)[0]

    assert row["label"] == "rey_lib"
    assert row["node_type"] == "repository"
    assert row["row_type"] == "node"
    assert row["is_enabled"] is True
    # The record's own fields reached the row, so a target may name one.
    assert row["repository"] == "rey_lib"
    assert row["record_id"] == "n:1"


def test_a_record_field_beats_a_declaration_scalar(asked, monkeypatch) -> None:
    """Configuration says where to read; a record says what it is.

    The declaration's resolved scalars are carried so a mapping can bind a
    place the source stands beside -- the file a record names, rather than the
    file the records are in. They sit *beneath* the record, because a
    declaration that could redefine a record's own identity would let
    configuration rewrite data.
    """
    declared = {**DECLARATION, "source_root": "/resolved/apps", "label": "from configuration"}
    record = {**SOURCE, "tree_configuration": declared}

    row = JsonlDataObject().children(_CTX, record)[0]

    # Carried, so a target may bind it.
    assert row["source_root"] == "/resolved/apps"
    # Overlaid, because the record owns what it is.
    assert row["label"] == "rey_lib"


def test_has_children_is_carried_not_inferred(asked) -> None:
    """Whether a record has records of its own is the file's answer."""
    rows = JsonlDataObject().children(_CTX, SOURCE)

    assert rows[0]["has_children"] is True
    assert rows[1]["has_children"] is False
    # A leaf names no source, so nothing asks the file about it again.
    assert rows[1]["tree_object_key"] is None


def test_no_action_is_named(asked) -> None:
    """Which action a kind is assigned is tree.node_type_action's answer."""
    for row in JsonlDataObject().children(_CTX, SOURCE):
        assert "text_click" not in row
        assert "buttons" not in row
        assert "reference" not in row


def test_records_leave_without_identity(asked) -> None:
    """Which key a created record carries is not this object's answer."""
    for row in JsonlDataObject().children(_CTX, SOURCE):
        assert "node_key" not in row
        assert "parent_node_key" not in row


def test_refresh_is_offered_at_the_declared_root_only(asked) -> None:
    """A record found inside a source is reached by asking its parent again."""
    for row in JsonlDataObject().children(_CTX, SOURCE):
        assert row["refreshable"] is False


def test_nothing_survives_between_calls() -> None:
    """Every level is one filtered read, so a level holds nothing."""
    assert JsonlDataObject().state_key(SOURCE) is None


# -- what it refuses ---------------------------------------------------------


@pytest.mark.parametrize(
    "missing",
    ["path", "file", "parent_field", "key_field", "label_field"],
)
def test_an_incomplete_declaration_is_refused(
    asked, missing: str,
) -> None:
    """A source that cannot say how a level is addressed is a fault, not empty.

    Answering it with no children would draw an empty branch, which looks
    exactly like a file that holds nothing.
    """
    declared = {key: value for key, value in DECLARATION.items() if key != missing}
    record = {**SOURCE, "tree_configuration": declared}

    with pytest.raises(ConfigError, match="root or path" if missing == "path" else missing):
        JsonlDataObject().children(_CTX, record)


def test_a_record_standing_on_no_declaration_is_refused(asked) -> None:
    with pytest.raises(ConfigError, match="must declare"):
        JsonlDataObject().children(_CTX, {"node_key": "4:7"})


# -- the boundary, held structurally -----------------------------------------

FEEDER = Path(jsonl.__file__)

#: What the Console must not read for a JSONL-sourced tree. The projection did
#: the join once; a consumer repeating it is the drift the artifact removes.
FORBIDDEN_SOURCES = (
    "01_core_architecture.yaml",
    "03_repository_map.rey_",
    "rey_lib.repository_map",
    "parse_yaml",
)


def test_the_feeder_selects_no_records_of_its_own() -> None:
    """JSONL matching has one implementation, and it is not here.

    The rule is about records, not about comprehensions: iterating the
    declaration to check it is fine, and choosing among what the reader
    returned is not. So this reads the loop that walks the reader's result and
    holds that it carries no condition -- one more `if` there would be a second
    interpretation of matching semantics, living where nobody would look.
    """
    tree = ast.parse(FEEDER.read_text(encoding="utf-8"))
    over_records = [
        generator
        for node in ast.walk(tree)
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp))
        for generator in node.generators
        if isinstance(generator.iter, ast.Call)
        and getattr(generator.iter.func, "id", "") == "read_jsonl_file"
    ]

    assert len(over_records) == 1, "the reader's result is walked exactly once"
    assert over_records[0].ifs == []
    assert "filter(" not in FEEDER.read_text(encoding="utf-8")


def _code_strings(tree: ast.Module) -> list[str]:
    """Every string the module *uses*, with its prose left out.

    Docstrings are where the contract is stated, and stating that a domain may
    never be named requires naming one. What matters is whether a domain
    reaches the code, so the guard reads the code.
    """
    prose = {
        id(node.body[0].value)
        for node in [tree, *ast.walk(tree)]
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in prose
    ]


def test_the_feeder_names_no_artifact_and_no_domain() -> None:
    """A file it understood would be a feeder per artifact.

    The declaration is the generic JSONL-to-tree contract: where the file is,
    and which of its fields fill the canonical row. A field name only one file
    could answer would be a domain leaking into a source contract.
    """
    used = _code_strings(ast.parse(FEEDER.read_text(encoding="utf-8")))

    for named in ("architecture", "record_id", "parent_id", "repository"):
        assert not [text for text in used if named in text], named


def test_the_console_reads_only_the_generated_artifact() -> None:
    """Increment 2 joined meaning to structure once. This holds it joined."""
    source = FEEDER.read_text(encoding="utf-8")

    for forbidden in FORBIDDEN_SOURCES:
        assert forbidden not in source
