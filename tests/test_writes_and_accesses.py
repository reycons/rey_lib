"""What a declaration writes, and which access forms it contains.

Two records split by direction, so one piece of syntax is never two facts. And
a hard line: ``code.access`` records that a call was written, never that it
read anything.
"""

from __future__ import annotations

from pathlib import Path

from rey_lib.repository_map.extractors import extract_writes_and_accesses


def _facts(path: Path, language: str) -> tuple[list, list]:
    return extract_writes_and_accesses(path, language, path.name)


class TestWritesAreSyntacticOnly:
    """Assignments. Not mutation, which a call can do and syntax cannot prove."""

    def test_each_target_shape_is_recorded(self, tmp_path: Path) -> None:
        path = tmp_path / "w.py"
        path.write_text(
            "def go(d, name):\n"
            "    x = 1\n"
            "    self.foo.bar = 2\n"
            "    payload['k'] = 3\n"
            "    d[name] = 4\n"
            "    x += 1\n",
            encoding="utf-8",
        )

        writes, _ = _facts(path, "Python")
        shaped = [(w.target_kind, w.target_chain, w.attribute_name, w.key,
                   w.is_augmented) for w in writes]

        assert shaped == [
            ("name", "x", None, None, False),
            ("attribute", "self.foo.bar", "bar", None, False),
            ("subscript", "payload", None, "k", False),
            # The key exists and is not provably a literal.
            ("subscript", "d", None, None, False),
            ("name", "x", None, None, True),
        ]

    def test_a_mutating_call_is_not_a_write(self, tmp_path: Path) -> None:
        """items.append mutates; no syntactic write fact proves that."""
        path = tmp_path / "m.py"
        path.write_text("def go(items):\n    items.append(1)\n", encoding="utf-8")

        writes, _ = _facts(path, "Python")

        assert writes == []

    def test_a_closure_is_outside_the_boundary(self, tmp_path: Path) -> None:
        path = tmp_path / "c.py"
        path.write_text(
            "def outer():\n    def inner():\n        y = 1\n    z = 2\n", "utf-8"
        )

        writes, _ = _facts(path, "Python")

        # The closure's own body is walked as part of its enclosing
        # declaration, so its write is attributed there rather than lost.
        assert {w.owner_qualified_name for w in writes} == {"outer"}


class TestNoExpressionTextIsStored:
    """The line C1 drew when it refused to store a default's value."""

    def test_an_impure_chain_stores_null_not_its_text(self, tmp_path: Path) -> None:
        path = tmp_path / "p.py"
        path.write_text(
            "def go(items, i):\n"
            "    factory().state = 1\n"
            "    items[i].metadata = 2\n",
            encoding="utf-8",
        )

        writes, _ = _facts(path, "Python")

        assert [(w.target_chain, w.attribute_name) for w in writes] == [
            (None, "state"),
            (None, "metadata"),
        ]

    def test_a_pure_chain_is_a_proven_dotted_name(self, tmp_path: Path) -> None:
        path = tmp_path / "q.py"
        path.write_text("def go():\n    request.context.payload = 1\n", "utf-8")

        writes, _ = _facts(path, "Python")

        assert writes[0].target_chain == "request.context.payload"


class TestAccessSaysSyntaxNotMeaning:
    """A .get row is a call that was written. Nothing more."""

    def test_the_four_get_forms_are_distinguishable(self, tmp_path: Path) -> None:
        """One nullable literal would collapse three proven states into one."""
        path = tmp_path / "g.py"
        path.write_text(
            "def go(end, name):\n"
            "    a = end.get('k')\n"
            "    b = end.get(name)\n"
            "    c = end.get(123)\n"
            "    d = end.get()\n",
            encoding="utf-8",
        )

        _, accesses = _facts(path, "Python")
        calls = [a for a in accesses if a.access_kind == "method_call"]

        assert [(a.argument_present, a.argument_kind, a.literal_argument)
                for a in calls] == [
            (True, "string_literal", "k"),
            (True, "expression", None),
            (True, "other_literal", None),
            (False, None, None),
        ]

    def test_no_row_claims_a_key_was_read(self, tmp_path: Path) -> None:
        """The claim would live in a field name, so the field set is asserted."""
        path = tmp_path / "n.py"
        path.write_text("def go(end):\n    a = end.get('k')\n", encoding="utf-8")

        _, accesses = _facts(path, "Python")
        recorded = accesses[0].to_dict()

        assert recorded["method_name"] == "get"
        assert not any("read" in field or "key" in field for field in recorded)

    def test_a_user_defined_get_is_recorded_the_same_way(
        self, tmp_path: Path
    ) -> None:
        """Because the parser cannot tell it apart, and must not pretend to."""
        path = tmp_path / "u.py"
        path.write_text(
            "def go(widget):\n    return widget.get('colour')\n", encoding="utf-8"
        )

        _, accesses = _facts(path, "Python")

        assert accesses[0].literal_argument == "colour"
        assert accesses[0].object_chain == "widget"


class TestNonLiteralSubscriptsSurvive:
    """A read exists even when its key does not resolve."""

    def test_a_dynamic_subscript_is_recorded_with_a_null_literal(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "s.py"
        path.write_text("def go(d, name):\n    return d[name]\n", encoding="utf-8")

        _, accesses = _facts(path, "Python")

        assert accesses[0].access_kind == "subscript"
        assert accesses[0].argument_present is True
        assert accesses[0].literal_argument is None

    def test_environment_lookup_needs_no_category(self, tmp_path: Path) -> None:
        path = tmp_path / "e.py"
        path.write_text("def go():\n    return os.environ['TOKEN']\n", "utf-8")

        _, accesses = _facts(path, "Python")

        assert accesses[0].object_chain == "os.environ"
        assert accesses[0].literal_argument == "TOKEN"


class TestDirectionalSplit:
    """A subscript write is an assignment and never also an access."""

    def test_a_subscript_write_is_not_also_an_access(self, tmp_path: Path) -> None:
        path = tmp_path / "d.py"
        path.write_text("def go(payload):\n    payload['k'] = 1\n", encoding="utf-8")

        writes, accesses = _facts(path, "Python")

        assert len(writes) == 1
        assert accesses == []

    def test_typescript_keeps_the_same_split(self, tmp_path: Path) -> None:
        path = tmp_path / "d.ts"
        path.write_text("export function go(payload) { payload['k'] = 1; }\n", "utf-8")

        writes, accesses = _facts(path, "TypeScript")

        assert [w.target_kind for w in writes] == ["subscript"]
        assert accesses == []


class TestTypeScriptMatchesPython:
    """One vocabulary, two grammars."""

    def test_a_declaration_without_a_value_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "b.ts"
        path.write_text("export function go() { let bare; let x = 1; }\n", "utf-8")

        writes, _ = _facts(path, "TypeScript")

        assert [w.target_chain for w in writes] == ["x"]

    def test_the_same_access_vocabulary(self, tmp_path: Path) -> None:
        path = tmp_path / "a.ts"
        path.write_text(
            "export function go(end, name) {\n"
            "  const a = end.get('k');\n"
            "  const b = end.get(name);\n"
            "  const g = process.env['TOKEN'];\n"
            "}\n",
            encoding="utf-8",
        )

        _, accesses = _facts(path, "TypeScript")

        assert [(a.access_kind, a.argument_kind, a.literal_argument)
                for a in accesses] == [
            ("method_call", "string_literal", "k"),
            ("method_call", "expression", None),
            ("subscript", "string_literal", "TOKEN"),
        ]


class TestOwnershipStopsAtNestedDeclarations:
    """Owner means the indexed declaration *containing* the occurrence.

    Before this, ``ast.walk`` and ``_walk`` descended into nested functions,
    lambdas and arrows, so a closure's statements were filed under whatever
    enclosed them -- roughly 1,746 rows across the estate's Python. A closure
    is not an addressable symbol, so those facts are left out rather than
    attributed to a declaration that does not contain them.

    Each test uses syntax the language can actually represent. A Python lambda
    body is a single expression: it can hold neither an assignment nor a
    return, so none is invented here.
    """

    def test_a_nested_function_keeps_its_own_writes_and_accesses(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "nested.py"
        path.write_text(
            "def outer(d):\n"
            "    kept = 1\n"
            "    outer_read = d['own']\n"
            "    def inner():\n"
            "        leaked = 2\n"
            "        return d['closure']\n"
            "    return inner\n",
            encoding="utf-8",
        )

        writes, accesses = _facts(path, "Python")

        assert [w.target_chain for w in writes] == ["kept", "outer_read"]
        assert [a.literal_argument for a in accesses] == ["own"]

    async_source = (
        "async def outer(d):\n"
        "    kept = 1\n"
        "    async def inner():\n"
        "        leaked = 2\n"
        "    return inner\n"
    )

    def test_a_nested_async_function_is_the_same_boundary(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "nested_async.py"
        path.write_text(self.async_source, encoding="utf-8")

        writes, _ = _facts(path, "Python")

        assert [w.target_chain for w in writes] == ["kept"]

    def test_a_lambda_keeps_its_own_accesses(self, tmp_path: Path) -> None:
        """Accesses are the facts a lambda can hold.

        Its body is one expression, so there is no assignment or return to
        test, and C2 records only Assign/AugAssign/AnnAssign -- never
        NamedExpr -- so a walrus is not a write fact either.
        """
        path = tmp_path / "lam.py"
        path.write_text(
            "def outer(d):\n"
            "    outer_read = d['own']\n"
            "    pick = lambda k: d[k]\n"
            "    fetch = lambda k: d.get('closure')\n"
            "    return pick, fetch\n",
            encoding="utf-8",
        )

        _, accesses = _facts(path, "Python")

        assert [a.literal_argument for a in accesses] == ["own"]

    def test_a_nested_arrow_keeps_its_own_writes_and_accesses(
        self, tmp_path: Path
    ) -> None:
        """TypeScript arrows can hold both, so both are asserted here."""
        path = tmp_path / "nested.ts"
        path.write_text(
            "export function outer(d) {\n"
            "  const kept = 1;\n"
            "  const outerRead = d['own'];\n"
            "  const inner = () => {\n"
            "    const leaked = 2;\n"
            "    return d['closure'];\n"
            "  };\n"
            "  return inner;\n"
            "}\n",
            encoding="utf-8",
        )

        writes, accesses = _facts(path, "TypeScript")

        # `inner` itself is a binding the outer body performs; only the
        # closure's insides are pruned. `leaked` is gone, which is the point.
        assert [w.target_chain for w in writes] == ["kept", "outerRead", "inner"]
        assert [a.literal_argument for a in accesses] == ["own"]

    def test_a_nested_function_expression_is_the_same_boundary(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "expr.ts"
        path.write_text(
            "export function outer() {\n"
            "  const kept = 1;\n"
            "  const inner = function () { const leaked = 2; };\n"
            "  return inner;\n"
            "}\n",
            encoding="utf-8",
        )

        writes, _ = _facts(path, "TypeScript")

        assert [w.target_chain for w in writes] == ["kept", "inner"]

    def test_the_nested_declaration_statement_itself_still_belongs_outward(
        self, tmp_path: Path
    ) -> None:
        """``const inner = () => {...}`` is a write the outer body performs.

        Only the closure's *insides* are pruned. Losing the binding itself
        would trade one false fact for a missing true one.
        """
        path = tmp_path / "binding.ts"
        path.write_text(
            "export function outer() {\n"
            "  const inner = () => 1;\n"
            "  return inner;\n"
            "}\n",
            encoding="utf-8",
        )

        writes, _ = _facts(path, "TypeScript")

        assert [w.target_chain for w in writes] == ["inner"]
