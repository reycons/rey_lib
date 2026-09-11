"""Which symbol a reference was written inside.

Positional, half-open, and refused when it cannot be proved. An edge is
unattributed because nothing contains it -- never because of what kind of edge
it is.
"""

from __future__ import annotations

from pathlib import Path

from rey_lib.repository_map import extract_executable_references, extract_symbols
from rey_lib.repository_map.records import attributed_edges


def _attributed(path: Path, language: str) -> dict[tuple[int, str], str]:
    """Map (line, target) to the symbol each edge was attributed to."""
    symbols = extract_symbols(path, language).symbols
    edges = attributed_edges(
        symbols, extract_executable_references(path, language)
    )
    return {(edge.source_line, edge.to): edge.from_symbol for edge in edges}


class TestWhatContainsWhat:
    """The narrowest containing declaration owns the reference."""

    def test_a_call_in_a_method_belongs_to_the_method(self, tmp_path: Path) -> None:
        """Not to the class. Both contain it; the method is narrower."""
        path = tmp_path / "owner.py"
        path.write_text(
            "class Holder:\n"
            "    def act(self):\n"
            "        helper()\n",
            encoding="utf-8",
        )

        assert _attributed(path, "Python")[(3, "helper")] == "Holder.act"

    def test_a_call_in_a_nested_function_belongs_to_the_recorded_symbol(
        self, tmp_path: Path
    ) -> None:
        """The inventory stops at one level, so the answer names something addressable."""
        path = tmp_path / "nested.py"
        path.write_text(
            "def outer():\n"
            "    def inner():\n"
            "        helper()\n"
            "    return inner\n",
            encoding="utf-8",
        )

        assert _attributed(path, "Python")[(3, "helper")] == "outer"


class TestNoEdgeKindIsSpecial:
    """Attribution is positional. Kind never decides it."""

    def test_a_module_level_import_is_unattributed(self, tmp_path: Path) -> None:
        """Nothing contains it -- which is why, not because it is an import."""
        path = tmp_path / "top.py"
        path.write_text("import os\n\n\ndef work():\n    return os\n", encoding="utf-8")

        assert _attributed(path, "Python")[(1, "os")] == ""

    def test_an_import_inside_a_function_belongs_to_it(self, tmp_path: Path) -> None:
        """The real case: finalize_run_log imports inside its own body.

        A rule that excluded imports by kind would lose this, and "what does
        this function depend on?" would be wrong about it.
        """
        path = tmp_path / "local.py"
        path.write_text(
            "def work():\n"
            "    import json\n"
            "    return json\n",
            encoding="utf-8",
        )

        assert _attributed(path, "Python")[(2, "json")] == "work"


class TestBoundaries:
    """Half-open containment, proved where it differs from inclusive."""

    def test_two_declarations_on_one_line_own_their_own_edges(
        self, tmp_path: Path
    ) -> None:
        """Separable only by column. Line containment cannot do this."""
        path = tmp_path / "pair.ts"
        path.write_text("const a=f();const b=g();\n", encoding="utf-8")

        attributed = _attributed(path, "TypeScript")

        assert attributed[(1, "f")] == "a"
        assert attributed[(1, "g")] == "b"

    def test_an_edge_at_a_symbols_end_belongs_to_the_next(self) -> None:
        """The end of one span is the start of the next; the position is in one.

        Built from controlled positions rather than from source, because two
        declarations whose spans exactly touch do not arise at declarator level
        in either grammar -- ``const a=1;const b=g();`` yields [1:6..1:9) and
        [1:16..1:21), with the keyword between them. The predicate still has to
        be right for the case, and this is the case that separates half-open
        from inclusive: an inclusive test puts the position inside both and
        then picks between two equally valid matches.
        """
        from rey_lib.repository_map.records import ReferenceEdge, SymbolRecord

        first = SymbolRecord(
            source_path="x.ts", source_line=1, source_column=0, name="first",
            symbol_kind="variable", end_line=1, end_column=10,
        )
        second = SymbolRecord(
            source_path="x.ts", source_line=1, source_column=10, name="second",
            symbol_kind="variable", end_line=1, end_column=20,
        )
        at_the_boundary = ReferenceEdge(
            source_path="x.ts", source_line=1, source_column=10,
            from_id="file:x.ts", to="g", edge_kind="call",
            evidence="call_expression",
        )

        placed = attributed_edges([first, second], [at_the_boundary])[0]

        assert placed.from_symbol == "second"
        assert placed.from_symbol_column == 10


class TestItRefusesRatherThanGuesses:
    """A tie is not an answer."""

    def test_equal_spans_produce_no_attribution(self) -> None:
        """Two symbols sharing one span cannot be told apart, so neither wins."""
        from rey_lib.repository_map.records import ReferenceEdge, SymbolRecord

        twin = dict(source_path="x.py", source_line=1, source_column=0,
                    symbol_kind="function", end_line=5, end_column=0)
        symbols = [SymbolRecord(name="one", **twin), SymbolRecord(name="two", **twin)]
        edge = ReferenceEdge(
            source_path="x.py", source_line=2, source_column=4,
            from_id="file:x.py", to="helper", edge_kind="call", evidence="ast.Call",
        )

        assert attributed_edges(symbols, [edge])[0].from_symbol == ""

    def test_a_tie_does_not_depend_on_the_order_supplied(self) -> None:
        """Reversing the input must not change the answer."""
        from rey_lib.repository_map.records import ReferenceEdge, SymbolRecord

        twin = dict(source_path="x.py", source_line=1, source_column=0,
                    symbol_kind="function", end_line=5, end_column=0)
        symbols = [SymbolRecord(name="one", **twin), SymbolRecord(name="two", **twin)]
        edge = ReferenceEdge(
            source_path="x.py", source_line=2, source_column=4,
            from_id="file:x.py", to="helper", edge_kind="call", evidence="ast.Call",
        )

        forward = attributed_edges(symbols, [edge])[0].from_symbol
        reverse = attributed_edges(list(reversed(symbols)), [edge])[0].from_symbol
        assert forward == reverse == ""


def test_the_join_key_is_the_whole_tuple(tmp_path: Path) -> None:
    """A qualified name is not unique in a file, so position travels with it."""
    path = tmp_path / "twice.py"
    path.write_text(
        "def handler():\n    first()\n\n\ndef handler():\n    second()\n",
        encoding="utf-8",
    )
    symbols = extract_symbols(path, "Python").symbols
    edges = attributed_edges(symbols, extract_executable_references(path, "Python"))

    placed = {edge.to: (edge.from_symbol, edge.from_symbol_line) for edge in edges}
    assert placed["first"] == ("handler", 1)
    assert placed["second"] == ("handler", 5)


def test_from_id_is_unchanged_by_attribution(tmp_path: Path) -> None:
    """Attribution is additive. Nothing reading from_id today is affected."""
    path = tmp_path / "same.py"
    path.write_text("def work():\n    helper()\n", encoding="utf-8")

    before = extract_executable_references(path, "Python", "pkg/same.py")
    after = attributed_edges(
        extract_symbols(path, "Python", "pkg/same.py").symbols, before
    )

    assert [edge.from_id for edge in after] == [edge.from_id for edge in before]
    assert len(after) == len(before)
