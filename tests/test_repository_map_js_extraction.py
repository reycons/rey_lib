"""Focused tests for JS/TS/TSX symbol and reference extraction.

Contract: rey_repository_map_generator.sgc.yaml (INC-002B).

The negative fixtures mirror the Python ones exactly: comments, string
literals and nested/local declarations must be structurally incapable of
producing a symbol or an edge.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rey_lib.repository_map import (
    EDGE_KIND_CALL,
    EDGE_KIND_GLOBAL_REFERENCE,
    EDGE_KIND_IMPORT,
    EDGE_KIND_PROPERTY_ACCESS,
    EDGE_KIND_RE_EXPORT,
    SYMBOL_KIND_CLASS,
    SYMBOL_KIND_ENUM,
    SYMBOL_KIND_FUNCTION,
    SYMBOL_KIND_GLOBAL_PUBLICATION,
    SYMBOL_KIND_INTERFACE,
    SYMBOL_KIND_RE_EXPORT,
    SYMBOL_KIND_TYPE_ALIAS,
    SYMBOL_KIND_VARIABLE,
    extract_executable_references,
    extract_symbols,
    supported_languages,
)

SOURCE = """\
import defaultThing from "./default_thing";
import { helper, other } from "./helpers";
import * as namespace from "./namespace";
import "./side_effect";
export { republished } from "./origin";

// commentedCall() must never be counted as a caller.
const LITERAL = "stringCall() is text, not syntax";
let mutable = 1;

export function topFunction() {
  const localValue = 1;
  function nestedFunction() {
    return 2;
  }
  class NestedClass {}
  helper();
  return [localValue, nestedFunction, NestedClass];
}

export class TopClass {
  method() {
    this.internalState;
    this.otherMethod();
    return other();
  }
}

function usesGlobals() {
  window.ReyConsole.refresh();
  return window.ReyTree;
}

window.ReyExtractorProbe = topFunction;

const shorthand = namespace.CONSTANT;
export { shorthand };
"""

TSX_SOURCE = """\
import { render } from "./render";

interface PanelProps {
  title: string;
}

type PanelKind = "wide" | "narrow";

enum PanelState {
  Open,
  Closed,
}

export function Panel(props: PanelProps) {
  return <div className="panel">{render(props.title)}</div>;
}
"""


@pytest.fixture()
def js_path(tmp_path: Path) -> Path:
    """Write the JavaScript fixture and return its path."""
    path = tmp_path / "sample.js"
    path.write_text(SOURCE, encoding="utf-8")
    return path


@pytest.fixture()
def tsx_path(tmp_path: Path) -> Path:
    """Write the TSX fixture and return its path."""
    path = tmp_path / "panel.tsx"
    path.write_text(TSX_SOURCE, encoding="utf-8")
    return path


def _symbols(path: Path, language: str) -> dict[str, str]:
    """Return a name-to-symbol_kind mapping for a fixture file."""
    inventory = extract_symbols(path, language)
    return {symbol.name: symbol.symbol_kind for symbol in inventory.symbols}


def test_all_three_languages_are_registered() -> None:
    """JS, TS and TSX resolve through the registry, not a language branch."""
    registered = supported_languages()

    assert {"JavaScript", "TypeScript", "TSX", "Python"} <= set(registered)


def test_top_level_declarations_are_recorded_with_their_kind(js_path: Path) -> None:
    """Functions, classes and module-level bindings each get their kind."""
    symbols = _symbols(js_path, "JavaScript")

    assert symbols["topFunction"] == SYMBOL_KIND_FUNCTION
    assert symbols["usesGlobals"] == SYMBOL_KIND_FUNCTION
    assert symbols["TopClass"] == SYMBOL_KIND_CLASS
    assert symbols["LITERAL"] == SYMBOL_KIND_VARIABLE
    assert symbols["mutable"] == SYMBOL_KIND_VARIABLE


def test_nested_and_local_declarations_are_not_top_level(js_path: Path) -> None:
    """Anything declared inside another body never reaches the inventory."""
    symbols = _symbols(js_path, "JavaScript")

    assert "nestedFunction" not in symbols
    assert "NestedClass" not in symbols
    assert "localValue" not in symbols
    assert "method" not in symbols


def test_a_local_shadowing_a_top_level_name_changes_nothing(tmp_path: Path) -> None:
    """Mutation fixture: a local matching a function name adds no symbol."""
    baseline = tmp_path / "baseline.js"
    baseline.write_text("function handler() { return 1; }\n", encoding="utf-8")
    mutated = tmp_path / "mutated.js"
    mutated.write_text(
        "function handler() { const handler = 2; return handler; }\n",
        encoding="utf-8",
    )

    before = extract_symbols(baseline, "JavaScript")
    after = extract_symbols(mutated, "JavaScript")

    assert len(before.symbols) == len(after.symbols) == 1


def test_export_marks_the_declaration_without_duplicating_it(js_path: Path) -> None:
    """An exported declaration is flagged, not emitted a second time."""
    inventory = extract_symbols(js_path, "JavaScript")
    names = [symbol.name for symbol in inventory.symbols]
    exported = {symbol.name for symbol in inventory.symbols if symbol.exported}

    assert names.count("topFunction") == 1
    assert names.count("TopClass") == 1
    assert {"topFunction", "TopClass", "shorthand"} <= exported
    assert "LITERAL" not in exported


def test_export_from_is_a_re_export(js_path: Path) -> None:
    """A republished name is a re_export symbol, located at its specifier."""
    inventory = extract_symbols(js_path, "JavaScript")
    re_exports = inventory.of_kind(SYMBOL_KIND_RE_EXPORT)

    assert [symbol.name for symbol in re_exports] == ["republished"]
    assert re_exports[0].exported is True
    assert re_exports[0].source_line == 5


def test_window_assignment_is_a_global_publication(js_path: Path) -> None:
    """window.X = ... publishes a global, recorded as its own symbol kind."""
    inventory = extract_symbols(js_path, "JavaScript")
    publications = inventory.of_kind(SYMBOL_KIND_GLOBAL_PUBLICATION)

    assert [symbol.name for symbol in publications] == ["window.ReyExtractorProbe"]
    # A window publication is not an ES export.
    assert publications[0].exported is False


def test_calls_come_only_from_executable_syntax(js_path: Path) -> None:
    """Comments and string literals produce no call edges."""
    targets = {
        edge.to
        for edge in extract_executable_references(js_path, "JavaScript")
        if edge.edge_kind == EDGE_KIND_CALL
    }

    assert "helper" in targets
    assert "other" in targets
    assert "commentedCall" not in targets
    assert "stringCall" not in targets


def test_a_call_added_only_in_a_comment_changes_no_count(tmp_path: Path) -> None:
    """Mutation fixture: a commented call must not change the caller count."""
    baseline = tmp_path / "baseline.js"
    baseline.write_text("function f() { real(); }\n", encoding="utf-8")
    mutated = tmp_path / "mutated.js"
    mutated.write_text("function f() { real(); /* fake(); */ }\n", encoding="utf-8")

    before = extract_executable_references(baseline, "JavaScript")
    after = extract_executable_references(mutated, "JavaScript")

    assert [edge.to for edge in before] == [edge.to for edge in after] == ["real"]


def test_member_call_is_one_call_not_also_a_property_access(tmp_path: Path) -> None:
    """A member call yields a single edge for the callee expression."""
    path = tmp_path / "member.js"
    path.write_text('import mod from "./mod";\nmod.run();\n', encoding="utf-8")

    edges = [
        edge for edge in extract_executable_references(path, "JavaScript") if edge.to == "mod.run"
    ]

    assert [edge.edge_kind for edge in edges] == [EDGE_KIND_CALL]


def test_this_rooted_references_are_excluded(js_path: Path) -> None:
    """this.x and this.method() never cross a file, so neither is an edge."""
    targets = {edge.to for edge in extract_executable_references(js_path, "JavaScript")}

    assert not any(target.startswith("this.") for target in targets)


def test_import_edges_carry_specifier_and_member(js_path: Path) -> None:
    """Named imports name their member; other forms name the module."""
    targets = {
        edge.to
        for edge in extract_executable_references(js_path, "JavaScript")
        if edge.edge_kind == EDGE_KIND_IMPORT
    }

    assert "./helpers.helper" in targets
    assert "./helpers.other" in targets
    assert "./default_thing" in targets
    assert "./namespace" in targets
    assert "./side_effect" in targets


def test_export_from_emits_a_re_export_edge(js_path: Path) -> None:
    """Re-export is an edge kind as well as a symbol kind."""
    edges = [
        edge
        for edge in extract_executable_references(js_path, "JavaScript")
        if edge.edge_kind == EDGE_KIND_RE_EXPORT
    ]

    assert [edge.to for edge in edges] == ["./origin.republished"]


def test_global_consumers_are_one_kind_of_fact(js_path: Path) -> None:
    """window usage is a global_reference whether called or merely read."""
    edges = [
        edge
        for edge in extract_executable_references(js_path, "JavaScript")
        if edge.edge_kind == EDGE_KIND_GLOBAL_REFERENCE
    ]
    targets = {edge.to for edge in edges}

    assert "window.ReyConsole.refresh" in targets
    assert "window.ReyTree" in targets
    # The publication target is a declaration, not a consumer.
    assert "window.ReyExtractorProbe" not in targets


def test_property_access_is_recorded_for_plain_chains(js_path: Path) -> None:
    """A non-call member chain is recorded as a property access."""
    targets = {
        edge.to
        for edge in extract_executable_references(js_path, "JavaScript")
        if edge.edge_kind == EDGE_KIND_PROPERTY_ACCESS
    }

    assert "namespace.CONSTANT" in targets


def test_typescript_type_declarations_carry_their_own_kind(tsx_path: Path) -> None:
    """Interface, enum and type alias are not squeezed into class/variable."""
    symbols = _symbols(tsx_path, "TSX")

    assert symbols["Panel"] == SYMBOL_KIND_FUNCTION
    assert symbols["PanelProps"] == SYMBOL_KIND_INTERFACE
    assert symbols["PanelKind"] == SYMBOL_KIND_TYPE_ALIAS
    assert symbols["PanelState"] == SYMBOL_KIND_ENUM


def test_an_abstract_class_is_still_a_class(tmp_path: Path) -> None:
    """Abstract classes keep the class kind rather than gaining their own."""
    path = tmp_path / "abstract.ts"
    path.write_text("export abstract class Base {}\n", encoding="utf-8")

    assert _symbols(path, "TypeScript")["Base"] == SYMBOL_KIND_CLASS


def test_tsx_calls_inside_jsx_are_executable(tsx_path: Path) -> None:
    """A call inside a JSX expression is a real call."""
    targets = {
        edge.to
        for edge in extract_executable_references(tsx_path, "TSX")
        if edge.edge_kind == EDGE_KIND_CALL
    }

    assert "render" in targets


def test_records_match_the_jsonl_shapes(tmp_path: Path) -> None:
    """Symbols and edges serialize to the shared record contract."""
    path = tmp_path / "small.js"
    path.write_text("function go() { run(); }\n", encoding="utf-8")

    symbol = extract_symbols(path, "JavaScript", "pkg/small.js").to_records()[0]
    edge = extract_executable_references(path, "JavaScript", "pkg/small.js")[0].to_dict()

    assert symbol == {
        "record_type": "symbol",
        "record_id": "symbol:pkg/small.js:go:1:9",
        "source_path": "pkg/small.js",
        "source_line": 1,
        "source_column": 9,
        "name": "go",
        "symbol_kind": SYMBOL_KIND_FUNCTION,
        "exported": False,
    }
    assert edge == {
        "record_type": "dependency_edge",
        "record_id": "edge:pkg/small.js:1:16:call:run",
        "source_path": "pkg/small.js",
        "source_line": 1,
        "from": "file:pkg/small.js",
        "to": "run",
        "edge_kind": EDGE_KIND_CALL,
        "evidence": "call_expression",
    }


def test_every_record_is_one_json_line(js_path: Path) -> None:
    """Symbols and edges both serialize to single-line complete JSON."""
    records = extract_symbols(js_path, "JavaScript").to_records()
    records += [e.to_dict() for e in extract_executable_references(js_path, "JavaScript")]

    assert records
    for record in records:
        line = json.dumps(record, separators=(",", ":"))

        assert "\n" not in line
        assert json.loads(line)["record_id"] == record["record_id"]


def test_extraction_is_deterministically_ordered(js_path: Path) -> None:
    """Two runs agree, and edges sort by line, column, kind and target."""
    first = extract_executable_references(js_path, "JavaScript")
    second = extract_executable_references(js_path, "JavaScript")

    assert first == second
    assert first == sorted(
        first,
        key=lambda edge: (edge.source_line, edge.source_column, edge.edge_kind, edge.to),
    )
    assert extract_symbols(js_path, "JavaScript") == extract_symbols(js_path, "JavaScript")


def test_positions_are_one_indexed_lines_and_zero_indexed_columns(tmp_path: Path) -> None:
    """Tree-sitter points are normalized to the record contract's convention."""
    path = tmp_path / "positions.js"
    path.write_text("\n\n  function later() {}\n", encoding="utf-8")

    symbol = extract_symbols(path, "JavaScript").symbols[0]

    assert (symbol.source_line, symbol.source_column) == (3, 11)
