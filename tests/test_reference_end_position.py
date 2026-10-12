"""Every extracted edge ends where its referenced name ends (backlog 871).

Rey's canonical reference position is a zero-based line and an exclusive
UTF-8 byte offset. Semantic evidence is matched to an edge on its file and
that position, so the position must hold for text that is not ASCII: a
character offset and a byte offset agree only while every character before
the reference is one byte long.
"""

from __future__ import annotations

from pathlib import Path

from rey_lib.repository_map.extractors import extract_executable_references
from rey_lib.repository_map.records import ReferenceEdge


def _edges(tmp_path: Path, name: str, source: str, language: str) -> list[ReferenceEdge]:
    """Extract the edges of one source file.

    Args:
        tmp_path: Directory to write the file in.
        name: File name.
        source: File text.
        language: Extractor language.

    Returns:
        The file's edges.
    """
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return extract_executable_references(path, language)


def _end_of(source: str, line: int, written: str) -> tuple[int, int]:
    """Return where ``written`` ends on a zero-based line, in UTF-8 bytes.

    Args:
        source: File text.
        line: Zero-based line holding the text.
        written: The referenced name as written there.

    Returns:
        Zero-based line and exclusive byte end column.
    """
    text = source.splitlines()[line]
    end = text.index(written) + len(written)
    return line, len(text[:end].encode("utf-8"))


def _only(edges: list[ReferenceEdge], to: str) -> ReferenceEdge:
    """Return the one edge naming ``to``.

    Args:
        edges: Extracted edges.
        to: The reference as recorded.

    Returns:
        That edge.
    """
    matching = [edge for edge in edges if edge.to == to]
    assert len(matching) == 1, [edge.to for edge in edges]
    return matching[0]


class TestNonAsciiTextBeforeTheReference:
    """The column counts bytes, so characters before the reference move it."""

    def test_python(self, tmp_path: Path) -> None:
        """A Python call after non-ASCII text ends at its byte offset."""
        source = 'déjà = "naïve—text"; résumé.mettre_à_jour(1)\n'
        edge = _only(_edges(tmp_path, "u.py", source, "Python"), "résumé.mettre_à_jour")

        assert (edge.end_line, edge.end_column) == _end_of(source, 0, "résumé.mettre_à_jour")
        # Not the character offset: the line holds multi-byte characters.
        assert edge.end_column != source.index("(1)")

    def test_typescript(self, tmp_path: Path) -> None:
        """A TypeScript call after non-ASCII text ends at its byte offset."""
        source = 'const déjà = "naïve—text"; résumé.mettreÀJour(1);\n'
        edge = _only(_edges(tmp_path, "u.ts", source, "TypeScript"), "résumé.mettreÀJour")

        assert (edge.end_line, edge.end_column) == _end_of(source, 0, "résumé.mettreÀJour")
        assert edge.end_column != source.index("(1)")


class TestWhereEachKindEnds:
    """The end is the referenced name's, never the enclosing syntax's."""

    def test_a_call_ends_at_its_callee_not_its_arguments(self, tmp_path: Path) -> None:
        """A call's end is its callee's last name."""
        source = "def f():\n    owner.read(first, second)\n"
        edge = _only(_edges(tmp_path, "c.py", source, "Python"), "owner.read")

        assert (edge.end_line, edge.end_column) == _end_of(source, 1, "owner.read")

    def test_a_chain_across_lines_ends_on_the_last_line(self, tmp_path: Path) -> None:
        """A member chain split over lines ends where its last name ends."""
        source = "def f():\n    return (owner\n        .read(1))\n"
        edge = _only(_edges(tmp_path, "m.py", source, "Python"), "owner.read")

        assert (edge.end_line, edge.end_column) == _end_of(source, 2, ".read")

    def test_lines_are_zero_based(self, tmp_path: Path) -> None:
        """The first line is line 0, whatever the edge's own source_line says."""
        source = "run()\n"
        edge = _only(_edges(tmp_path, "z.py", source, "Python"), "run")

        assert (edge.source_line, edge.end_line, edge.end_column) == (1, 0, 3)

    def test_an_imported_name_ends_before_its_alias(self, tmp_path: Path) -> None:
        """``from m import Name as Other`` ends where Name ends."""
        source = "from pkg.mod import Name as Other, Plain\n"
        edges = _edges(tmp_path, "i.py", source, "Python")

        named = _only(edges, "pkg.mod.Name")
        plain = _only(edges, "pkg.mod.Plain")
        assert (named.end_line, named.end_column) == _end_of(source, 0, "Name")
        assert (plain.end_line, plain.end_column) == _end_of(source, 0, "Plain")

    def test_a_whole_module_import_ends_at_its_dotted_name(self, tmp_path: Path) -> None:
        """``import a.b as c`` ends where a.b ends."""
        source = "import os.path as p\n"
        edge = _only(_edges(tmp_path, "w.py", source, "Python"), "os.path")

        assert (edge.end_line, edge.end_column) == _end_of(source, 0, "os.path")

    def test_a_generic_base_ends_at_its_name(self, tmp_path: Path) -> None:
        """``class C(Base[T])`` names Base, and ends there."""
        source = "class C(Base[T]):\n    pass\n"
        edge = _only(_edges(tmp_path, "g.py", source, "Python"), "Base")

        assert (edge.end_line, edge.end_column) == _end_of(source, 0, "Base")

    def test_a_typescript_call_ends_at_its_callee(self, tmp_path: Path) -> None:
        """A TypeScript call's end is its callee's last name."""
        source = "function f() {\n  owner.read(first, second);\n}\n"
        edge = _only(_edges(tmp_path, "c.ts", source, "TypeScript"), "owner.read")

        assert (edge.end_line, edge.end_column) == _end_of(source, 1, "owner.read")

    def test_a_typescript_named_import_ends_at_each_member(self, tmp_path: Path) -> None:
        """Each member of a named import ends where its own name ends."""
        source = 'import { First, Second } from "./mod";\n'
        edges = _edges(tmp_path, "i.ts", source, "TypeScript")

        first = _only(edges, "./mod.First")
        second = _only(edges, "./mod.Second")
        assert (first.end_line, first.end_column) == _end_of(source, 0, "First")
        assert (second.end_line, second.end_column) == _end_of(source, 0, "Second")

    def test_a_typescript_side_effect_import_ends_at_its_source(self, tmp_path: Path) -> None:
        """An import naming no member ends where its source string ends."""
        source = 'import "./styles.css";\n'
        edge = _only(_edges(tmp_path, "s.ts", source, "TypeScript"), "./styles.css")

        assert (edge.end_line, edge.end_column) == _end_of(source, 0, '"./styles.css"')

    def test_a_typescript_generic_heritage_ends_at_its_name(self, tmp_path: Path) -> None:
        """``implements Port<T>`` names Port, and ends there."""
        source = "class C implements Port<string> {}\n"
        edge = _only(_edges(tmp_path, "h.ts", source, "TypeScript"), "Port")

        assert (edge.end_line, edge.end_column) == _end_of(source, 0, "Port")
