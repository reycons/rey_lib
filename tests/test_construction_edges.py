"""A construction is a call edge to what it constructs, in both languages (backlog 768).

Python writes a construction as a call, ``ObjectTree()``, and has always been
indexed as one. TypeScript writes ``new ObjectTree()``; the grammar names that
callee ``constructor`` rather than ``function``, and reading the wrong field
recorded no edge at all -- so who constructs a TypeScript object could not be
answered from the index.
"""

from __future__ import annotations

from pathlib import Path

from rey_lib.repository_map.extractors import extract_call_arguments, extract_executable_references


def _edges(tmp_path: Path, name: str, source: str, language: str) -> list[tuple[str, str]]:
    """Return (edge kind, target) for every non-import edge of one file.

    Args:
        tmp_path: Directory to write the file in.
        name: File name.
        source: File text.
        language: Extractor language.

    Returns:
        The edges, in extraction order.
    """
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return [
        (edge.edge_kind, edge.to)
        for edge in extract_executable_references(path, language)
        if edge.edge_kind != "import"
    ]


class TestTheSameConstructionInBothLanguages:
    """Equivalent constructions produce equivalent edges."""

    def test_a_bare_class(self, tmp_path: Path) -> None:
        """ObjectTree() and new ObjectTree() are both a call to ObjectTree."""
        python = _edges(tmp_path, "a.py", "def make():\n    return ObjectTree(1)\n", "Python")
        typescript = _edges(
            tmp_path, "a.ts", "export function make() {\n  return new ObjectTree(1);\n}\n", "TypeScript"
        )

        assert python == typescript == [("call", "ObjectTree")]

    def test_a_qualified_class(self, tmp_path: Path) -> None:
        """ns.Thing() and new ns.Thing() are one call, not a call and an access."""
        python = _edges(tmp_path, "b.py", "def make():\n    return ns.Thing()\n", "Python")
        typescript = _edges(
            tmp_path, "b.ts", "export function make() {\n  return new ns.Thing();\n}\n", "TypeScript"
        )

        assert python == typescript == [("call", "ns.Thing")]


class TestTypeScriptConstruction:
    """The construction edge carries what every call edge carries."""

    def test_evidence_and_end_position(self, tmp_path: Path) -> None:
        """It is proved by new_expression and ends where the class name ends."""
        source = "export function make() {\n  const t = new ObjectTree(1);\n}\n"
        path = tmp_path / "c.ts"
        path.write_text(source, encoding="utf-8")

        edge = next(e for e in extract_executable_references(path, "TypeScript") if e.to == "ObjectTree")

        assert (edge.edge_kind, edge.evidence) == ("call", "new_expression")
        assert (edge.end_line, edge.end_column) == (1, source.splitlines()[1].index("(1)"))

    def test_its_arguments_belong_to_it(self, tmp_path: Path) -> None:
        """A construction's arguments are recorded against its call edge."""
        path = tmp_path / "d.ts"
        path.write_text("export function make() {\n  new ObjectTree(host, 'tree');\n}\n", encoding="utf-8")

        arguments = [
            (a.callee, a.edge_kind, a.ordinal)
            for a in extract_call_arguments(path, "TypeScript", "d.ts")
        ]

        assert arguments == [("ObjectTree", "call", 0), ("ObjectTree", "call", 1)]
