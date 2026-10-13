"""Where a declaration raises, and the name it raises.

A name, never a type. The parser proves that a name was called; it does not
prove the name is an exception class, and across this estate many raises call a
factory function or a local variable.
"""

from __future__ import annotations

from pathlib import Path

from rey_lib.repository_map.extractors import extract_raise_sites


def _sites(path: Path, language: str) -> list[tuple]:
    """Return each site as (ordinal, is_bare, kind, chain, callee, cause)."""
    return [
        (r.ordinal, r.is_bare, r.value_kind, r.value_chain, r.callee_chain,
         r.has_cause)
        for r in extract_raise_sites(path, language, path.name)
    ]


def _owners(path: Path, language: str) -> list[str]:
    """Return the owning declaration of each recorded site."""
    return [
        r.owner_qualified_name
        for r in extract_raise_sites(path, language, path.name)
    ]


class TestTheNameIsANameAndNotAType:
    """The distinction the whole table turns on."""

    def test_a_call_records_the_callee_and_no_expression_chain(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "call.py"
        path.write_text(
            "def go(mod):\n    raise mod.Error('x')\n", encoding="utf-8"
        )

        assert _sites(path, "Python") == [
            (0, False, "call", None, "mod.Error", False)
        ]

    def test_a_bare_name_records_the_chain_and_no_callee(
        self, tmp_path: Path
    ) -> None:
        """``raise err`` raises a variable, and the parser says only that."""
        path = tmp_path / "name.py"
        path.write_text("def go(err):\n    raise err\n", encoding="utf-8")

        assert _sites(path, "Python") == [
            (0, False, "name_chain", "err", None, False)
        ]

    def test_an_unproven_chain_is_absent_rather_than_text(
        self, tmp_path: Path
    ) -> None:
        """``factory().Error`` is not a proved chain, so nothing is stored.

        Storing the source text would put an expression in the index, which is
        the boundary every fact table here keeps.
        """
        path = tmp_path / "impure.py"
        path.write_text(
            "def go(factory):\n    raise factory().Error('x')\n", encoding="utf-8"
        )

        assert _sites(path, "Python") == [(0, False, "call", None, None, False)]

    def test_a_factory_call_is_recorded_like_any_other_call(
        self, tmp_path: Path
    ) -> None:
        """This is why the column is not called exception_type.

        The parser cannot tell a class from a function that returns one, and
        the benchmark subject raises a factory five times out of eight.
        """
        path = tmp_path / "factory.py"
        path.write_text(
            "def go(ctx):\n    raise _failure(ctx, step_id='x')\n",
            encoding="utf-8",
        )

        assert _sites(path, "Python") == [
            (0, False, "call", None, "_failure", False)
        ]


class TestBareAndCauseAreDifferentFacts:
    """Re-raising and chaining a cause are different syntax."""

    def test_a_bare_raise_records_nothing_else(self, tmp_path: Path) -> None:
        path = tmp_path / "bare.py"
        path.write_text(
            "def go():\n"
            "    try:\n        pass\n"
            "    except Exception:\n        raise\n",
            encoding="utf-8",
        )

        assert _sites(path, "Python") == [(0, True, None, None, None, False)]

    def test_a_cause_is_recorded_and_its_absence_is_too(
        self, tmp_path: Path
    ) -> None:
        """The estate's contract requires `from original` on a re-raise."""
        path = tmp_path / "cause.py"
        path.write_text(
            "def go(original):\n"
            "    if a:\n        raise ValueError('x') from original\n"
            "    raise ValueError('y')\n",
            encoding="utf-8",
        )

        assert [(s[4], s[5]) for s in _sites(path, "Python")] == [
            ("ValueError", True),
            ("ValueError", False),
        ]


class TestOwnershipStopsAtNestedDeclarations:
    """Same boundary as returns and writes."""

    def test_a_nested_function_keeps_its_own_raise(self, tmp_path: Path) -> None:
        path = tmp_path / "nested.py"
        path.write_text(
            "def outer():\n"
            "    def inner():\n"
            "        raise RuntimeError('leaked')\n"
            "    raise ValueError('own')\n",
            encoding="utf-8",
        )

        assert _owners(path, "Python") == ["outer"]
        assert [s[4] for s in _sites(path, "Python")] == ["ValueError"]

    def test_a_nested_arrow_keeps_its_own_throw(self, tmp_path: Path) -> None:
        path = tmp_path / "nested.ts"
        path.write_text(
            "export function outer() {\n"
            "  const inner = () => { throw new Error('leaked'); };\n"
            "  throw new Error('own');\n"
            "}\n",
            encoding="utf-8",
        )

        assert _owners(path, "TypeScript") == ["outer"]


class TestTypeScript:
    """`new X()` is the common form and earns its own kind."""

    def test_a_construction_records_its_constructor(self, tmp_path: Path) -> None:
        path = tmp_path / "new.ts"
        path.write_text(
            "export function go(mod) {\n"
            "  if (a) { throw new Error('x'); }\n"
            "  throw new mod.Failure('y');\n"
            "}\n",
            encoding="utf-8",
        )

        assert _sites(path, "TypeScript") == [
            (0, False, "construction", None, "Error", False),
            (1, False, "construction", None, "mod.Failure", False),
        ]

    def test_typescript_is_never_bare_and_never_carries_a_cause(
        self, tmp_path: Path
    ) -> None:
        """Neither construct exists in the language.

        The columns are there because Python has them, not invented for
        symmetry -- and a cause passed to an Error constructor is an argument,
        not this syntax.
        """
        path = tmp_path / "flags.ts"
        path.write_text(
            "export function go(err) {\n"
            "  if (a) { throw err; }\n"
            "  throw new Error('x', { cause: err });\n"
            "}\n",
            encoding="utf-8",
        )

        found = _sites(path, "TypeScript")

        assert [(s[1], s[5]) for s in found] == [(False, False), (False, False)]
        assert [s[2] for s in found] == ["name_chain", "construction"]


class TestTheBenchmarkSubject:
    """Asserted here because it is the argument for the column's name."""

    def test_run_workflow_raises_a_class_three_times_and_a_factory_five(
        self,
    ) -> None:
        path = (Path(__file__).resolve().parents[1]
                / "rey_lib/workflow/coordinator.py")

        found = [
            r.callee_chain
            for r in extract_raise_sites(path, "Python", path.name)
            if r.owner_qualified_name == "run_workflow"
        ]

        assert len(found) == 8
        assert found.count("WorkflowError") == 3
        assert found.count("_pre_execution_failure") == 5
