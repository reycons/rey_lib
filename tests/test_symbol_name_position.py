"""Every symbol records where its name itself is written (backlog 872, D3).

The construct's start -- a decorator, ``def``, ``export`` -- stays the
symbol's identity; the name position is added beside it, in Rey's canonical
coordinates (zero-based line, UTF-8 byte offset), because that is what
semantic evidence names a definition by. A name that cannot be located
exactly has no position: it is never estimated.
"""

from __future__ import annotations

from pathlib import Path

from rey_lib.repository_map.extractors import extract_symbols
from rey_lib.repository_map.records import SymbolRecord


def _symbols(tmp_path: Path, name: str, source: str, language: str) -> dict[str, SymbolRecord]:
    """Extract one file's symbols, by qualified name.

    Args:
        tmp_path: Directory to write the file in.
        name: File name.
        source: File text.
        language: Extractor language.

    Returns:
        The symbols keyed by qualified name.
    """
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return {symbol.qualified_name: symbol for symbol in extract_symbols(path, language).symbols}


def _name_at(source: str, line: int, written: str) -> tuple[int, int]:
    """Return where ``written`` begins on a zero-based line, in UTF-8 bytes.

    Args:
        source: File text.
        line: Zero-based line holding the name.
        written: The name.

    Returns:
        Zero-based line and byte column.
    """
    text = source.splitlines()[line]
    return line, len(text[:text.index(written)].encode("utf-8"))


def _position(symbol: SymbolRecord) -> tuple[int | None, int | None]:
    """Return a symbol's recorded name position.

    Args:
        symbol: The symbol.

    Returns:
        Its name line and column.
    """
    return symbol.name_line, symbol.name_column


class TestPython:
    """The name follows the keywords; the construct start is unchanged."""

    def test_a_function_class_and_method(self, tmp_path: Path) -> None:
        """def, class and a method each record their name's own position."""
        source = "def run():\n    pass\n\n\nclass Thing:\n    def go(self):\n        pass\n"
        symbols = _symbols(tmp_path, "a.py", source, "Python")

        assert _position(symbols["run"]) == _name_at(source, 0, "run")
        assert _position(symbols["Thing"]) == _name_at(source, 4, "Thing")
        assert _position(symbols["Thing.go"]) == _name_at(source, 5, "go")
        # The identity keeps the construct's start.
        assert (symbols["Thing.go"].source_line, symbols["Thing.go"].source_column) == (6, 4)

    def test_async_and_decorated(self, tmp_path: Path) -> None:
        """async def and a decorated definition name the def line's name."""
        source = "@cached\nasync def   fetch():\n    pass\n"
        fetch = _symbols(tmp_path, "b.py", source, "Python")["fetch"]

        assert _position(fetch) == _name_at(source, 1, "fetch")
        assert fetch.source_line == 2

    def test_non_ascii_before_and_in_the_name(self, tmp_path: Path) -> None:
        """Byte offsets, not characters: non-ASCII names and text move them."""
        source = "class Café:\n    def méthode(self):\n        pass\n\nrésumé = 'é'\n"
        symbols = _symbols(tmp_path, "c.py", source, "Python")

        assert _position(symbols["Café"]) == _name_at(source, 0, "Café")
        assert _position(symbols["Café.méthode"]) == _name_at(source, 1, "méthode")
        assert _position(symbols["résumé"]) == (4, 0)

    def test_a_variable_is_its_own_target(self, tmp_path: Path) -> None:
        """An assignment's name is where its target is written."""
        source = "first, = (1,)\nLIMIT: int = 3\nx = y = 2\n"
        symbols = _symbols(tmp_path, "d.py", source, "Python")

        assert _position(symbols["LIMIT"]) == (1, 0)
        assert _position(symbols["y"]) == _name_at(source, 2, "y")

    def test_a_name_on_another_line_is_not_guessed(self, tmp_path: Path) -> None:
        """A name continued past the def line has no recorded position."""
        source = "def \\\n    split():\n    pass\n"
        split = _symbols(tmp_path, "e.py", source, "Python")["split"]

        assert _position(split) == (None, None)

    def test_a_reexport_has_no_name_position(self, tmp_path: Path) -> None:
        """A re-export's declaration is in another module."""
        source = "from pkg.mod import Thing\n\n__all__ = ['Thing']\n"
        thing = _symbols(tmp_path, "__init__.py", source, "Python")["Thing"]

        assert _position(thing) == (None, None)


class TestTypeScript:
    """tree-sitter's identifier node is the name; export does not move it."""

    def test_exported_function_class_method(self, tmp_path: Path) -> None:
        """The name, not the export keyword, is the recorded position."""
        source = (
            "export function applyTheme(): void {}\n"
            "export class Panel {\n"
            "  draw(): void {}\n"
            "}\n"
        )
        symbols = _symbols(tmp_path, "a.ts", source, "TypeScript")

        assert _position(symbols["applyTheme"]) == _name_at(source, 0, "applyTheme")
        assert _position(symbols["Panel"]) == _name_at(source, 1, "Panel")
        assert _position(symbols["Panel.draw"]) == _name_at(source, 2, "draw")

    def test_non_ascii_before_the_name(self, tmp_path: Path) -> None:
        """Byte offsets after non-ASCII text on the same line."""
        source = 'const déjà = "é"; export function résumé(): void {}\n'
        symbols = _symbols(tmp_path, "b.ts", source, "TypeScript")

        assert _position(symbols["résumé"]) == _name_at(source, 0, "résumé")
        assert _position(symbols["déjà"]) == _name_at(source, 0, "déjà")
