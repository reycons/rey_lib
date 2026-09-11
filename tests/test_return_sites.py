"""Where a declaration returns, and the shape of what it returns.

Shape, never value. ``return compute()`` proves a call was written; what it
evaluates to is not a parser-proven fact, and no expression text is stored.
"""

from __future__ import annotations

from pathlib import Path

from rey_lib.repository_map import extract_symbols
from rey_lib.repository_map.extractors import extract_return_sites


def _sites(path: Path, language: str) -> list[tuple]:
    """Return each site as (ordinal, has_value, value_kind, value_chain)."""
    return [
        (r.ordinal, r.has_value, r.value_kind, r.value_chain)
        for r in extract_return_sites(path, language, path.name)
    ]


def _owners(path: Path, language: str) -> list[str]:
    """Return the owning declaration of each recorded site, in order."""
    return [
        r.owner_qualified_name
        for r in extract_return_sites(path, language, path.name)
    ]


def _generators(path: Path, language: str) -> dict[str, bool]:
    """Map qualified name to whether the symbol is a generator."""
    return {
        s.qualified_name: s.is_generator
        for s in extract_symbols(path, language, path.name).symbols
    }


class TestOwnershipStopsAtNestedDeclarations:
    """A closure's return is not the enclosing declaration's.

    This is the failure the design exists to prevent: 560 estate returns sit
    inside closures, and attributing them outward would say each enclosing
    declaration returns something it does not.
    """

    def test_a_nested_function_keeps_its_own_return(self, tmp_path: Path) -> None:
        path = tmp_path / "nested.py"
        path.write_text(
            "def outer():\n"
            "    def inner():\n"
            "        return 1\n"
            "    return 5\n",
            encoding="utf-8",
        )

        assert _sites(path, "Python") == [(0, True, "literal", None)]
        assert _owners(path, "Python") == ["outer"]

    def test_a_nested_arrow_keeps_its_own_return(self, tmp_path: Path) -> None:
        """TypeScript reaches the case a Python lambda cannot express.

        A lambda body is one expression, so there is no Python-lambda return
        to test; an arrow has a real statement body and does.
        """
        path = tmp_path / "nested.ts"
        path.write_text(
            "export function outer() {\n"
            "  const inner = () => { return 1; };\n"
            "  return 5;\n"
            "}\n",
            encoding="utf-8",
        )

        assert _sites(path, "TypeScript") == [(0, True, "literal", None)]
        assert _owners(path, "TypeScript") == ["outer"]

    def test_a_method_keeps_its_own_return(self, tmp_path: Path) -> None:
        path = tmp_path / "method.py"
        path.write_text(
            "class C:\n"
            "    def m(self):\n"
            "        def helper():\n"
            "            return 'leaked'\n"
            "        return self.v\n",
            encoding="utf-8",
        )

        assert _sites(path, "Python") == [(0, True, "name_chain", "self.v")]
        assert _owners(path, "Python") == ["C.m"]


class TestTheShapeOfAReturnedExpression:
    """Each kind is a shape the grammar gives directly."""

    def test_a_proven_chain_is_separated_from_an_attribute(
        self, tmp_path: Path
    ) -> None:
        """``factory().state`` is not a name chain, and is not called one.

        If the chain helper refuses an expression, the parser has proved an
        attribute access whose receiver is something else. Calling both
        name_chain would put a known grammar form in the wrong category.
        """
        path = tmp_path / "chain.py"
        path.write_text(
            "def go(self, factory):\n"
            "    if a:\n"
            "        return self.a.b\n"
            "    return factory().state\n",
            encoding="utf-8",
        )

        assert _sites(path, "Python") == [
            (0, True, "name_chain", "self.a.b"),
            (1, True, "attribute", None),
        ]

    def test_a_bare_return_records_absence_rather_than_a_gap(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "bare.py"
        path.write_text("def go():\n    return\n", encoding="utf-8")

        assert _sites(path, "Python") == [(0, False, None, None)]

    def test_none_is_distinct_from_another_literal(self, tmp_path: Path) -> None:
        """"Does it return None" is a question people actually ask."""
        path = tmp_path / "none.py"
        path.write_text(
            "def go(flag):\n"
            "    if flag:\n"
            "        return None\n"
            "    return 0\n",
            encoding="utf-8",
        )

        assert _sites(path, "Python") == [
            (0, True, "none_literal", None),
            (1, True, "literal", None),
        ]

    def test_each_remaining_kind_is_recorded(self, tmp_path: Path) -> None:
        path = tmp_path / "kinds.py"
        path.write_text(
            "def go(d, k, flag):\n"
            "    if a: return d[k]\n"
            "    if b: return compute()\n"
            "    if c: return [1, 2]\n"
            "    if e: return [x for x in d]\n"
            "    if f: return 1 if flag else 2\n"
            "    if g: return k + 1\n"
            "    return k\n",
            encoding="utf-8",
        )

        assert [s[2] for s in _sites(path, "Python")] == [
            "subscript", "call", "collection_literal", "comprehension",
            "conditional", "operation", "name_chain",
        ]

    def test_an_f_string_stores_no_part_of_its_contents(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "fstring.py"
        path.write_text(
            'def go(secret):\n    return f"token-{secret}"\n', encoding="utf-8"
        )

        sites = _sites(path, "Python")

        assert sites == [(0, True, "f_string", None)]
        assert "token" not in str(sites)

    def test_typescript_undefined_is_neither_null_nor_an_identifier(
        self, tmp_path: Path
    ) -> None:
        """It has its own grammar node, so it falls to `other`.

        Folding it into none_literal would erase a distinction TypeScript
        makes, and calling it a name_chain would be false: it is not an
        identifier. Measured at 5 of 756 TypeScript return sites, so it stays
        in the residue rather than earning a kind.
        """
        path = tmp_path / "undef.ts"
        path.write_text(
            "export function go(obj) {\n"
            "  if (a) { return null; }\n"
            "  if (b) { return undefined; }\n"
            "  if (c) { return obj.prop; }\n"
            "  return;\n"
            "}\n",
            encoding="utf-8",
        )

        assert _sites(path, "TypeScript") == [
            (0, True, "none_literal", None),
            (1, True, "other", None),
            (2, True, "name_chain", "obj.prop"),
            (3, False, None, None),
        ]


class TestIsGenerator:
    """Declaration context a consumer cannot otherwise get without the source.

    It does not correct a return site -- a generator really does contain
    ``return 2``. It is what separates that site from an ordinary one.
    """

    def test_yield_and_yield_from_both_count(self, tmp_path: Path) -> None:
        path = tmp_path / "gen.py"
        path.write_text(
            "def plain():\n    return 1\n\n"
            "def yielding():\n    yield 1\n\n"
            "def delegating(other):\n    yield from other\n",
            encoding="utf-8",
        )

        assert _generators(path, "Python") == {
            "plain": False, "yielding": True, "delegating": True,
        }

    def test_a_closures_yield_does_not_make_its_owner_a_generator(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "closure.py"
        path.write_text(
            "def outer():\n"
            "    def inner():\n"
            "        yield 1\n"
            "    return inner\n",
            encoding="utf-8",
        )

        assert _generators(path, "Python")["outer"] is False

    def test_a_generators_return_site_is_recorded_as_written(
        self, tmp_path: Path
    ) -> None:
        """The site is not suppressed; is_generator is what qualifies it."""
        path = tmp_path / "genreturn.py"
        path.write_text(
            "def g():\n    yield 1\n    return 2\n", encoding="utf-8"
        )

        assert _sites(path, "Python") == [(0, True, "literal", None)]
        assert _generators(path, "Python")["g"] is True

    def test_typescript_generator_forms(self, tmp_path: Path) -> None:
        """A `function*` with no yield is still a generator."""
        path = tmp_path / "gen.ts"
        path.write_text(
            "export function plain() { return 1; }\n"
            "export function* empty() { return 1; }\n"
            "export class K { *gen() { yield 1; } plain() { return 1; } }\n",
            encoding="utf-8",
        )

        found = _generators(path, "TypeScript")

        assert found["plain"] is False
        assert found["empty"] is True
        assert found["K.gen"] is True
        assert found["K.plain"] is False


class TestTheBenchmarkSubject:
    """The subject did not move, so its true answer is asserted here."""

    def test_finalize_run_log_returns_three_collection_literals(self) -> None:
        path = Path(__file__).resolve().parents[1] / "rey_lib/logs/summary.py"

        found = [
            (r.ordinal, r.value_kind)
            for r in extract_return_sites(path, "Python", path.name)
            if r.owner_qualified_name == "finalize_run_log"
        ]

        assert found == [
            (0, "collection_literal"),
            (1, "collection_literal"),
            (2, "collection_literal"),
        ]
