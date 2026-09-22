"""What a class declares it derives from.

The index could not answer "which classes extend Panel?" -- 35 of them do --
so the console migration answered it by grep. Nor could it follow an inherited
method: 279 of 893 self.* call sites resolved to nothing because the method is
declared on an ancestor.

One relation, two languages, and TWO KINDS: extending a class brings its
members, implementing an interface brings none.
"""

from __future__ import annotations

from pathlib import Path

from rey_lib.repository_map import (
    EDGE_KIND_IMPLEMENTS,
    EDGE_KIND_INHERITS,
    extract_executable_references,
    extract_symbols,
)
from rey_lib.repository_map.records import attributed_edges


def _heritage(tmp_path: Path, name: str, language: str, source: str) -> list:
    """Return the inherits/implements edges one source declares, attributed."""
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    inventory = extract_symbols(path, language, name)
    edges = attributed_edges(
        inventory.symbols, extract_executable_references(path, language, name)
    )
    return [e for e in edges
            if e.edge_kind in {EDGE_KIND_INHERITS, EDGE_KIND_IMPLEMENTS}]


class TestExtendsAndImplementsAreDifferentRelations:
    """One kind would answer neither question."""

    def test_they_carry_different_kinds(self, tmp_path: Path) -> None:
        """A class that implements an interface does not inherit from it.

        No members arrive. Recording both as 'inherits' would make
        WHERE edge_kind = 'inherits' return things the class does not
        inherit, with nothing on the row to say which is which.
        """
        edges = _heritage(
            tmp_path, "x.ts", "TypeScript",
            "class Sub extends Panel implements TreeDisplayNode {\n  m() {}\n}\n",
        )

        assert sorted((e.edge_kind, e.to) for e in edges) == [
            (EDGE_KIND_IMPLEMENTS, "TreeDisplayNode"),
            (EDGE_KIND_INHERITS, "Panel"),
        ]

    def test_every_interface_listed_gets_its_own_edge(self, tmp_path: Path) -> None:
        """One fact each, for the reason an argument is one row each."""
        edges = _heritage(
            tmp_path, "x.ts", "TypeScript",
            "class Sub implements First, Second {\n  m() {}\n}\n",
        )

        assert [e.to for e in edges] == ["First", "Second"]
        assert {e.edge_kind for e in edges} == {EDGE_KIND_IMPLEMENTS}

    def test_python_has_no_implements(self, tmp_path: Path) -> None:
        """ABC and Protocol conformance is written AS a base class.

        So it is genuinely inheritance and needs no second kind -- the
        absence of implements rows here is the language, not a gap.
        """
        edges = _heritage(
            tmp_path, "x.py", "Python",
            "from abc import ABC\nclass Sub(ABC):\n    pass\n",
        )

        assert [(e.edge_kind, e.to) for e in edges] == [(EDGE_KIND_INHERITS, "ABC")]


class TestTheEdgeNamesTheSubclass:
    """Resolution is a join, and this is the column it joins on."""

    def test_from_symbol_is_the_declaring_class(self, tmp_path: Path) -> None:
        """No attribution rule of its own.

        A heritage clause sits on the class's own line, before any method
        begins, so the narrowest symbol containing it is the class itself.
        """
        edges = _heritage(
            tmp_path, "x.py", "Python",
            "class Base:\n    pass\nclass Sub(Base):\n    def m(self):\n        pass\n",
        )

        assert [(e.from_symbol, e.to) for e in edges] == [("Sub", "Base")]

    def test_a_dotted_base_is_recorded_as_written(self, tmp_path: Path) -> None:
        """Like every other edge. Resolution stays a query."""
        edges = _heritage(
            tmp_path, "x.py", "Python",
            "import enum\nclass E(enum.Enum):\n    A = 1\n",
        )

        assert [e.to for e in edges] == ["enum.Enum"]

    def test_a_class_declaring_nothing_produces_no_edge(self, tmp_path: Path) -> None:
        """789 of 951 Python classes declare no base. Absence is a fact."""
        assert _heritage(tmp_path, "x.py", "Python", "class Plain:\n    pass\n") == []


class TestTypeParametersAreNotTheBase:
    """Panel<T> derives from Panel."""

    def test_typescript_generic_arguments_are_stripped(self, tmp_path: Path) -> None:
        """Keeping them would give a target matching no declared class."""
        edges = _heritage(
            tmp_path, "x.ts", "TypeScript",
            "class Sub extends Panel<Row> {\n  m() {}\n}\n",
        )

        assert [e.to for e in edges] == ["Panel"]

    def test_python_subscripted_bases_are_stripped(self, tmp_path: Path) -> None:
        """The same rule, written once per language rather than once."""
        edges = _heritage(
            tmp_path, "x.py", "Python",
            "class Sub(Generic[T]):\n    pass\n",
        )

        assert [e.to for e in edges] == ["Generic"]
