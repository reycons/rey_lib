"""Focused tests for syntax-aware Python symbol and reference extraction.

Contract: rey_repository_map_generator.sgc.yaml (INC-002A).

The negative fixtures matter as much as the positive ones: comments,
docstrings, string literals and local declarations must be structurally
incapable of producing a symbol or an edge.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rey_lib.repository_map import (
    EDGE_KIND_CALL,
    EDGE_KIND_IMPORT,
    EDGE_KIND_PROPERTY_ACCESS,
    EDGE_KIND_RE_EXPORT,
    SYMBOL_KIND_CLASS,
    SYMBOL_KIND_FUNCTION,
    SYMBOL_KIND_RE_EXPORT,
    SYMBOL_KIND_VARIABLE,
    extract_executable_references,
    extract_symbols,
    supported_languages,
)

SOURCE = '''"""Module docstring mentioning ghost_call() which is not a call."""

import os
import os.path as os_path
from collections import OrderedDict
from .relative import helper as aliased_helper

__all__ = ["top_function", "TopClass", "aliased_helper"]

# commented_call() must never be counted as a caller.
LITERAL = "string_call() is text, not syntax"
ANNOTATED: int = 3


def top_function():
    """Docstring naming docstring_call() harmlessly."""
    local_value = 1
    top_function = 2

    def nested_function():
        return 3

    class NestedClass:
        pass

    os_path.join("a", "b")
    return local_value, top_function, nested_function, NestedClass


async def async_top_function():
    return None


class TopClass:
    class_attribute = 1

    def method(self):
        self.attribute_access
        self.other_method()
        return OrderedDict()

    def other_method(self):
        return None
'''


@pytest.fixture()
def module_path(tmp_path: Path) -> Path:
    """Write the fixture module and return its path."""
    path = tmp_path / "sample.py"
    path.write_text(SOURCE, encoding="utf-8")
    return path


def _symbols(module_path: Path) -> dict[str, str]:
    """Return a name-to-symbol_kind mapping for the fixture module."""
    inventory = extract_symbols(module_path, "Python")
    return {symbol.name: symbol.symbol_kind for symbol in inventory.symbols}


def test_python_is_a_registered_language() -> None:
    """Python resolves through the registry rather than a language branch."""
    assert "Python" in supported_languages()


def test_top_level_declarations_are_recorded_with_their_kind(module_path: Path) -> None:
    """Functions, classes and module-level variables each get their kind."""
    symbols = _symbols(module_path)

    assert symbols["top_function"] == SYMBOL_KIND_FUNCTION
    assert symbols["async_top_function"] == SYMBOL_KIND_FUNCTION
    assert symbols["TopClass"] == SYMBOL_KIND_CLASS
    assert symbols["LITERAL"] == SYMBOL_KIND_VARIABLE
    assert symbols["ANNOTATED"] == SYMBOL_KIND_VARIABLE


def test_nested_declarations_are_not_top_level(module_path: Path) -> None:
    """Anything declared inside another body never reaches the inventory."""
    symbols = _symbols(module_path)

    assert "nested_function" not in symbols
    assert "NestedClass" not in symbols
    assert "method" not in symbols
    assert "class_attribute" not in symbols
    assert "local_value" not in symbols


def test_local_shadowing_a_top_level_name_changes_nothing(tmp_path: Path) -> None:
    """Mutation fixture: a local matching a function name adds no symbol."""
    baseline = tmp_path / "baseline.py"
    baseline.write_text("def handler():\n    return 1\n", encoding="utf-8")
    mutated = tmp_path / "mutated.py"
    mutated.write_text(
        "def handler():\n    handler = 2\n    return handler\n",
        encoding="utf-8",
    )

    before = extract_symbols(baseline, "Python")
    after = extract_symbols(mutated, "Python")

    assert len(before.symbols) == len(after.symbols) == 1


def test_dunder_all_marks_exported_without_duplicating_a_symbol(module_path: Path) -> None:
    """An exported declaration is flagged, not emitted a second time."""
    inventory = extract_symbols(module_path, "Python")
    names = [symbol.name for symbol in inventory.symbols]
    exported = {symbol.name for symbol in inventory.symbols if symbol.exported}

    assert names.count("top_function") == 1
    assert exported == {"top_function", "TopClass", "aliased_helper"}
    assert "__all__" not in names


def test_an_exported_import_becomes_a_re_export(module_path: Path) -> None:
    """A republished import is a re_export, located at the import line."""
    inventory = extract_symbols(module_path, "Python")
    re_exports = inventory.of_kind(SYMBOL_KIND_RE_EXPORT)

    assert [symbol.name for symbol in re_exports] == ["aliased_helper"]
    assert re_exports[0].exported is True
    assert re_exports[0].source_line == 6


def test_symbol_serializes_to_the_jsonl_symbol_record_shape(module_path: Path) -> None:
    """A declaration serializes as one complete 'symbol' record."""
    inventory = extract_symbols(module_path, "Python", "pkg/sample.py")
    record = next(r for r in inventory.to_records() if r["name"] == "TopClass")

    assert record == {
        "record_type": "symbol",
        "record_id": "symbol:pkg/sample.py:TopClass:34:0",
        "source_path": "pkg/sample.py",
        "source_line": 34,
        "source_column": 0,
        "name": "TopClass",
        "symbol_kind": SYMBOL_KIND_CLASS,
        "exported": True,
    }


def test_duplicate_top_level_names_stay_two_facts(tmp_path: Path) -> None:
    """A redefined name must not cost the fact store a record.

    The generator scans facts; it does not enforce a unique-name convention.
    """
    path = tmp_path / "duplicate.py"
    path.write_text("def handler():\n    return 1\n\n\ndef handler():\n    return 2\n", "utf-8")

    inventory = extract_symbols(path, "Python", "duplicate.py")
    records = inventory.to_records()

    assert [record["name"] for record in records] == ["handler", "handler"]
    assert len({record["record_id"] for record in records}) == 2


def test_calls_come_only_from_executable_syntax(module_path: Path) -> None:
    """Comments, docstrings and string literals produce no call edges."""
    targets = {
        edge.to
        for edge in extract_executable_references(module_path, "Python")
        if edge.edge_kind == EDGE_KIND_CALL
    }

    assert "os_path.join" in targets
    assert "OrderedDict" in targets
    assert "ghost_call" not in targets
    assert "commented_call" not in targets
    assert "string_call" not in targets
    assert "docstring_call" not in targets


def test_a_call_added_only_in_a_comment_changes_no_count(tmp_path: Path) -> None:
    """Mutation fixture: a commented call must not change the caller count."""
    baseline = tmp_path / "baseline.py"
    baseline.write_text("def f():\n    real()\n", encoding="utf-8")
    mutated = tmp_path / "mutated.py"
    mutated.write_text("def f():\n    real()\n    # fake()\n", encoding="utf-8")

    before = extract_executable_references(baseline, "Python")
    after = extract_executable_references(mutated, "Python")

    assert [edge.to for edge in before] == [edge.to for edge in after] == ["real"]


def test_member_call_is_one_call_not_also_a_property_access(module_path: Path) -> None:
    """A member call yields a single call edge for the callee expression."""
    edges = extract_executable_references(module_path, "Python")
    join_edges = [edge for edge in edges if edge.to == "os_path.join"]

    assert [edge.edge_kind for edge in join_edges] == [EDGE_KIND_CALL]


def test_self_rooted_references_are_excluded(module_path: Path) -> None:
    """self.attr and self.method() never cross a file, so neither is an edge."""
    targets = {edge.to for edge in extract_executable_references(module_path, "Python")}

    assert not any(target.startswith("self.") for target in targets)


def test_import_edges_carry_module_and_member(module_path: Path) -> None:
    """Import edges name what was imported, including relative levels."""
    targets = {
        edge.to
        for edge in extract_executable_references(module_path, "Python")
        if edge.edge_kind == EDGE_KIND_IMPORT
    }

    assert "os" in targets
    assert "collections.OrderedDict" in targets
    assert ".relative.helper" in targets


def test_a_republished_import_also_emits_a_re_export_edge(module_path: Path) -> None:
    """Re-export is an edge kind as well as a symbol kind."""
    edges = [
        edge
        for edge in extract_executable_references(module_path, "Python")
        if edge.edge_kind == EDGE_KIND_RE_EXPORT
    ]

    assert [edge.to for edge in edges] == [".relative.helper"]
    assert edges[0].evidence == "__all__"


def test_property_access_is_recorded_for_plain_chains(tmp_path: Path) -> None:
    """A non-call attribute chain is recorded as a property access."""
    path = tmp_path / "access.py"
    path.write_text("import settings\n\n\ndef f():\n    return settings.VALUE\n", encoding="utf-8")

    kinds = {
        edge.to: edge.edge_kind
        for edge in extract_executable_references(path, "Python")
        if edge.edge_kind == EDGE_KIND_PROPERTY_ACCESS
    }

    assert kinds == {"settings.VALUE": EDGE_KIND_PROPERTY_ACCESS}


def test_edge_serializes_to_the_jsonl_dependency_edge_shape(tmp_path: Path) -> None:
    """An edge serializes as one complete 'dependency_edge' record."""
    path = tmp_path / "call.py"
    path.write_text("def f():\n    handler()\n", encoding="utf-8")

    edges = extract_executable_references(path, "Python", "pkg/call.py")

    assert [edge.to_dict() for edge in edges] == [
        {
            "record_type": "dependency_edge",
            "record_id": "edge:pkg/call.py:2:4:call:handler",
            "source_path": "pkg/call.py",
            "source_line": 2,
            "from": "file:pkg/call.py",
            "to": "handler",
            "edge_kind": EDGE_KIND_CALL,
            "evidence": "ast.Call",
        }
    ]


def test_two_references_on_one_line_keep_distinct_identities(tmp_path: Path) -> None:
    """record_id uses line and column, so one line can carry two facts."""
    path = tmp_path / "twice.py"
    path.write_text("def f():\n    a(); a()\n", encoding="utf-8")

    ids = [edge.record_id for edge in extract_executable_references(path, "Python", "twice.py")]

    assert len(ids) == len(set(ids)) == 2


def test_every_record_is_one_json_line(module_path: Path) -> None:
    """Symbols and edges both serialize to single-line complete JSON."""
    records = extract_symbols(module_path, "Python").to_records()
    records += [edge.to_dict() for edge in extract_executable_references(module_path, "Python")]

    assert records
    for record in records:
        line = json.dumps(record, separators=(",", ":"))

        assert "\n" not in line
        assert json.loads(line)["record_id"] == record["record_id"]


def test_every_edge_carries_verifiable_evidence(module_path: Path) -> None:
    """Each edge names its file, a positive line and its proving syntax."""
    edges = extract_executable_references(module_path, "Python")

    assert edges
    for edge in edges:
        assert edge.source_path == module_path.as_posix()
        assert edge.source_line > 0
        assert edge.edge_kind and edge.to and edge.evidence


def test_extraction_is_deterministically_ordered(module_path: Path) -> None:
    """Two runs agree, and edges sort by line, column, kind and target."""
    first = extract_executable_references(module_path, "Python")
    second = extract_executable_references(module_path, "Python")

    assert first == second
    assert first == sorted(
        first,
        key=lambda edge: (edge.source_line, edge.source_column, edge.edge_kind, edge.to),
    )
    assert extract_symbols(module_path, "Python") == extract_symbols(module_path, "Python")


def test_recorded_path_can_be_overridden(module_path: Path) -> None:
    """The orchestrator can record repository-relative paths directly."""
    edges = extract_executable_references(module_path, "Python", "pkg/sample.py")
    inventory = extract_symbols(module_path, "Python", "pkg/sample.py")

    assert inventory.path == "pkg/sample.py"
    assert {edge.source_path for edge in edges} == {"pkg/sample.py"}
    assert {edge.from_id for edge in edges} == {"file:pkg/sample.py"}


def test_unsupported_language_is_refused_not_emptied() -> None:
    """An unregistered language raises rather than reporting 'no references'."""
    with pytest.raises(ValueError, match="No repository-map extractor"):
        extract_executable_references(Path("widget.ts"), "TypeScript")


def test_unparseable_python_names_the_file(tmp_path: Path) -> None:
    """A syntax error is reported against the offending file."""
    path = tmp_path / "broken.py"
    path.write_text("def broken(:\n", encoding="utf-8")

    with pytest.raises(ValueError, match="broken.py"):
        extract_symbols(path, "Python")
