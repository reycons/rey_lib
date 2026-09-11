"""What a declaration declares it takes.

Declared, never inferred. What a caller may pass is a language question; what
the signature says is a syntactic one, and only the second is recorded.
"""

from __future__ import annotations

from pathlib import Path

from rey_lib.repository_map.extractors import extract_declared_parameters
from rey_lib.repository_map import extract_symbols


def _params(path: Path, language: str) -> dict[str, dict[str, object]]:
    """Map parameter name to its recorded facts."""
    return {
        record.name: {
            "ordinal": record.ordinal,
            "kind": record.parameter_kind,
            "has_default": record.has_default,
            "is_optional": record.is_optional,
            "annotation": record.annotation,
            "owner": record.owner_qualified_name,
        }
        for record in extract_declared_parameters(path, language, path.name)
    }


class TestPython:
    """All five forms, in the order they are written."""

    def test_every_kind_is_recorded_in_declaration_order(self, tmp_path: Path) -> None:
        path = tmp_path / "five.py"
        path.write_text(
            "def go(a, /, b, c=1, *rest, d, e=2, **kw):\n    pass\n", encoding="utf-8"
        )

        found = _params(path, "Python")

        assert [(n, f["ordinal"], f["kind"]) for n, f in found.items()] == [
            ("a", 0, "positional_only"),
            ("b", 1, "positional"),
            ("c", 2, "positional"),
            ("rest", 3, "var_positional"),
            ("d", 4, "keyword_only"),
            ("e", 5, "keyword_only"),
            ("kw", 6, "var_keyword"),
        ]

    def test_defaults_are_right_aligned(self, tmp_path: Path) -> None:
        """Python attaches defaults to the last positional forms, not the first."""
        path = tmp_path / "defaults.py"
        path.write_text("def go(a, b, c=1, d=2):\n    pass\n", encoding="utf-8")

        found = _params(path, "Python")

        assert [found[n]["has_default"] for n in ("a", "b", "c", "d")] == [
            False, False, True, True,
        ]

    def test_a_keyword_only_default_is_its_own_alignment(self, tmp_path: Path) -> None:
        """kw_defaults is positional with None for the undefaulted."""
        path = tmp_path / "kwonly.py"
        path.write_text("def go(*, a, b=1, c):\n    pass\n", encoding="utf-8")

        found = _params(path, "Python")

        assert found["a"]["has_default"] is False
        assert found["b"]["has_default"] is True
        assert found["c"]["has_default"] is False

    def test_python_has_no_syntactic_optional(self, tmp_path: Path) -> None:
        """A default is the only way to omit one, and that is has_default's fact."""
        path = tmp_path / "opt.py"
        path.write_text("def go(a=1):\n    pass\n", encoding="utf-8")

        assert _params(path, "Python")["a"]["is_optional"] is False

    def test_a_method_is_owned_by_its_class(self, tmp_path: Path) -> None:
        path = tmp_path / "owned.py"
        path.write_text("class Holder:\n    def act(self, x):\n        pass\n", "utf-8")

        assert _params(path, "Python")["x"]["owner"] == "Holder.act"

    def test_a_closure_declares_nothing_the_index_records(self, tmp_path: Path) -> None:
        """The inventory stops at one level, and parameters keep that boundary."""
        path = tmp_path / "closure.py"
        path.write_text(
            "def outer(a):\n    def inner(b):\n        return b\n    return inner\n",
            encoding="utf-8",
        )

        assert set(_params(path, "Python")) == {"a"}


class TestTypeScript:
    """Default and optional are different facts and must never collapse."""

    def test_a_default_is_found_by_its_value_not_its_node_type(
        self, tmp_path: Path
    ) -> None:
        """tree-sitter calls ``b = 2`` a required_parameter carrying a value."""
        path = tmp_path / "d.ts"
        path.write_text("export function go(b = 2) {}\n", encoding="utf-8")

        found = _params(path, "TypeScript")["b"]

        assert found["has_default"] is True
        assert found["is_optional"] is False

    def test_an_optional_marker_is_not_a_default(self, tmp_path: Path) -> None:
        """``b?: T`` may be omitted and declares no value."""
        path = tmp_path / "o.ts"
        path.write_text("export function go(b?: string) {}\n", encoding="utf-8")

        found = _params(path, "TypeScript")["b"]

        assert found["is_optional"] is True
        assert found["has_default"] is False

    def test_a_rest_element_is_var_positional(self, tmp_path: Path) -> None:
        path = tmp_path / "r.ts"
        path.write_text("export function go(...rest: number[]) {}\n", encoding="utf-8")

        found = _params(path, "TypeScript")["rest"]

        assert found["kind"] == "var_positional"
        assert found["has_default"] is False
        assert found["is_optional"] is False


class TestAnnotations:
    """Absent is None, never an empty string."""

    def test_an_unannotated_parameter_stores_none(self, tmp_path: Path) -> None:
        path = tmp_path / "bare.py"
        path.write_text("def go(a):\n    pass\n", encoding="utf-8")

        assert _params(path, "Python")["a"]["annotation"] is None

    def test_an_annotated_parameter_stores_what_is_written(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "ann.py"
        path.write_text("def go(a: dict[str, int]):\n    pass\n", encoding="utf-8")

        assert _params(path, "Python")["a"]["annotation"] == "dict[str, int]"


class TestDeclaredReturnIsNotBehaviour:
    """The distinction C1 exists to keep."""

    def test_a_declared_return_is_recorded(self, tmp_path: Path) -> None:
        path = tmp_path / "r.py"
        path.write_text('def f() -> int:\n    return "x"\n', encoding="utf-8")

        declared = {s.qualified_name: s.returns_annotation
                    for s in extract_symbols(path, "Python").symbols}

        # It declares int. It returns a string. Only the first is a fact the
        # parser proves, and the index must not claim the second.
        assert declared["f"] == "int"

    def test_an_undeclared_return_is_none(self, tmp_path: Path) -> None:
        path = tmp_path / "n.py"
        path.write_text("def f():\n    return 1\n", encoding="utf-8")

        declared = {s.qualified_name: s.returns_annotation
                    for s in extract_symbols(path, "Python").symbols}

        assert declared["f"] is None

    def test_typescript_declares_its_return(self, tmp_path: Path) -> None:
        path = tmp_path / "r.ts"
        path.write_text("export function go(): void {}\n", encoding="utf-8")

        declared = {s.qualified_name: s.returns_annotation
                    for s in extract_symbols(path, "TypeScript").symbols}

        assert declared["go"] == "void"


def test_a_default_expression_is_never_stored(tmp_path: Path) -> None:
    """Presence is the fact. The expression is source, and stays there."""
    path = tmp_path / "secret.py"
    path.write_text("def go(a=some_call(1, 2)):\n    pass\n", encoding="utf-8")

    records = extract_declared_parameters(path, "Python", "secret.py")

    assert records[0].has_default is True
    assert "some_call" not in str(records[0].to_dict())
