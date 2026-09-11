"""What a class body binds, directly.

Syntax, never meaning. Python has no field construct -- a dataclass field, a
ClassVar, a plain class attribute and ``__slots__`` are the same statement --
so these assert what the body binds and never what a decorator later makes of
it.
"""

from __future__ import annotations

from pathlib import Path

from rey_lib.repository_map.extractors import extract_class_attributes


def _attributes(path: Path, language: str) -> list[dict[str, object]]:
    """Return each recorded attribute's facts, in written order."""
    return [
        {
            "name": record.name,
            "ordinal": record.ordinal,
            "owner": record.owner_qualified_name,
            "form": record.declaration_form,
            "is_annotated": record.is_annotated,
            "has_default": record.has_default,
            "is_optional": record.is_optional,
            "annotation": record.annotation,
            "modifiers": list(record.modifiers),
        }
        for record in extract_class_attributes(path, language, path.name)
    ]


def _names(path: Path, language: str) -> list[str]:
    """Return the recorded names, in written order."""
    return [record["name"] for record in _attributes(path, language)]


class TestPython:
    """What the immediate class body binds, and what it refuses."""

    def test_the_three_reachable_states_are_distinguished(self, tmp_path: Path) -> None:
        """``x: int`` and ``x = 1`` are different declarations.

        Which is why is_annotated is a fact of its own rather than
        ``annotation IS NOT NULL``: collapsing them would lose the difference
        between declaring a type and binding a value.
        """
        path = tmp_path / "states.py"
        path.write_text(
            "class C:\n    a: int\n    b = 1\n    c: int = 1\n", encoding="utf-8"
        )

        found = {record["name"]: record for record in _attributes(path, "Python")}

        assert [(found[n]["is_annotated"], found[n]["has_default"])
                for n in ("a", "b", "c")] == [(True, False), (False, True), (True, True)]
        assert found["b"]["annotation"] is None

    def test_slots_is_an_ordinary_row(self, tmp_path: Path) -> None:
        """It is a class-body assignment, so it needs no exception.

        Deciding __slots__ is "not really an attribute" would be
        interpretation, and a reader filtering dunders makes that choice for
        themselves.
        """
        path = tmp_path / "slots.py"
        path.write_text('class C:\n    __slots__ = ("a",)\n', encoding="utf-8")

        assert _names(path, "Python") == ["__slots__"]

    def test_a_chained_assignment_binds_each_name(self, tmp_path: Path) -> None:
        """``a = b = 1`` binds both, so both are rows with distinct ordinals."""
        path = tmp_path / "chained.py"
        path.write_text("class C:\n    a = b = 1\n", encoding="utf-8")

        assert [(r["name"], r["ordinal"]) for r in _attributes(path, "Python")] == [
            ("a", 0), ("b", 1),
        ]

    def test_a_tuple_target_is_refused(self, tmp_path: Path) -> None:
        """Unpacking does not prove which name receives which value.

        Recording these would make has_default a claim about unpacking rather
        than about syntax.
        """
        path = tmp_path / "tuple.py"
        path.write_text("class C:\n    a, b = 1, 2\n", encoding="utf-8")

        assert _names(path, "Python") == []

    def test_a_conditional_binding_is_outside_the_surface(self, tmp_path: Path) -> None:
        """It is bound conditionally, so "declares" is not proven.

        The table and view say so in their own comments, so a consumer does not
        read this absence as proof the class has no such name.
        """
        path = tmp_path / "conditional.py"
        path.write_text(
            "TYPE_CHECKING = False\n\n"
            "class C:\n    kept = 1\n    if TYPE_CHECKING:\n        hidden = 2\n",
            encoding="utf-8",
        )

        assert _names(path, "Python") == ["kept"]

    def test_a_nested_class_is_not_walked(self, tmp_path: Path) -> None:
        """The symbol inventory records top-level classes only.

        An attribute owned by a nested class would name an owner the index does
        not hold, so the boundary is the inventory's, not a new one.
        """
        path = tmp_path / "nested.py"
        path.write_text(
            "class Outer:\n    kept = 1\n\n    class Inner:\n        nested = 2\n",
            encoding="utf-8",
        )

        assert _names(path, "Python") == ["kept"]

    def test_an_attribute_target_is_not_a_class_attribute(self, tmp_path: Path) -> None:
        """``obj.attr = 1`` in a class body binds something else entirely."""
        path = tmp_path / "attribute.py"
        path.write_text("class C:\n    obj = object()\n    obj.attr = 1\n",
                        encoding="utf-8")

        assert _names(path, "Python") == ["obj"]

    def test_self_assignments_in_methods_are_not_here(self, tmp_path: Path) -> None:
        """Those are code.assignment's, from C2, and the two compose."""
        path = tmp_path / "method.py"
        path.write_text(
            "class C:\n    declared = 1\n\n"
            "    def __init__(self):\n        self.assigned = 2\n",
            encoding="utf-8",
        )

        assert _names(path, "Python") == ["declared"]

    def test_classvar_stays_annotation_text(self, tmp_path: Path) -> None:
        """It is a type as written, not a modifier, and is not decomposed."""
        path = tmp_path / "classvar.py"
        path.write_text("class C:\n    a: ClassVar[int] = 1\n", encoding="utf-8")

        found = _attributes(path, "Python")[0]

        assert found["annotation"] == "ClassVar[int]"
        assert found["modifiers"] == []

    def test_python_carries_no_modifiers_and_no_optionality(
        self, tmp_path: Path
    ) -> None:
        """Empty rather than a row of falses.

        Python has no readonly, so is_readonly = False would assert a
        distinction the language does not make.
        """
        path = tmp_path / "plain.py"
        path.write_text("class C:\n    a: int = 1\n", encoding="utf-8")

        found = _attributes(path, "Python")[0]

        assert found["modifiers"] == []
        assert found["is_optional"] is False
        assert found["form"] == "class_attribute"


class TestTypeScript:
    """Modifiers preserved as written, and the class boundary held."""

    def test_modifiers_are_kept_in_source_order(self, tmp_path: Path) -> None:
        path = tmp_path / "mods.ts"
        path.write_text(
            "export class C {\n"
            "  protected static readonly e: number = 5;\n"
            "}\n",
            encoding="utf-8",
        )

        found = _attributes(path, "TypeScript")[0]

        assert found["modifiers"] == ["protected", "static", "readonly"]
        assert found["is_annotated"] is True
        assert found["has_default"] is True
        assert found["annotation"] == "number"

    def test_declare_and_abstract_are_modifiers_not_storage_claims(
        self, tmp_path: Path
    ) -> None:
        """Both allocate nothing, and no column here says so.

        The parser proves the keyword; what it implies about storage is the
        reader's inference, which is why declaration_form stays grammar
        provenance.
        """
        path = tmp_path / "nostorage.ts"
        path.write_text(
            "export abstract class C {\n"
            "  declare f: string;\n"
            "  abstract g: number;\n"
            "}\n",
            encoding="utf-8",
        )

        found = _attributes(path, "TypeScript")

        assert [r["modifiers"] for r in found] == [["declare"], ["abstract"]]
        assert {r["form"] for r in found} == {"field_definition"}

    def test_a_default_and_an_optional_marker_are_independent(
        self, tmp_path: Path
    ) -> None:
        """tree-sitter reports ``b = 2`` as a field carrying a value.

        So a default is detected by that value and never by the node type,
        exactly as C1 found for parameters.
        """
        path = tmp_path / "independent.ts"
        path.write_text(
            "export class C {\n  b = 2;\n  c?: number;\n}\n", encoding="utf-8"
        )

        found = {r["name"]: r for r in _attributes(path, "TypeScript")}

        assert (found["b"]["has_default"], found["b"]["is_optional"]) == (True, False)
        assert (found["c"]["has_default"], found["c"]["is_optional"]) == (False, True)

    def test_a_private_name_keeps_its_hash(self, tmp_path: Path) -> None:
        """The # is inside the identifier token, not a modifier.

        So ECMAScript-private needs no flag, and stays distinct from
        TypeScript's ``private``, which is a modifier.
        """
        path = tmp_path / "private.ts"
        path.write_text(
            "export class C {\n  #priv = 9;\n  private b = 2;\n}\n", encoding="utf-8"
        )

        found = _attributes(path, "TypeScript")

        assert [(r["name"], r["modifiers"]) for r in found] == [
            ("#priv", []), ("b", ["private"]),
        ]

    def test_methods_and_index_signatures_are_not_attributes(
        self, tmp_path: Path
    ) -> None:
        """A method is already a symbol; an index signature declares no name."""
        path = tmp_path / "other.ts"
        path.write_text(
            "export class C {\n"
            "  a = 1;\n"
            "  [key: string]: unknown;\n"
            "  run() {}\n"
            "}\n",
            encoding="utf-8",
        )

        assert _names(path, "TypeScript") == ["a"]

    def test_an_interface_produces_nothing(self, tmp_path: Path) -> None:
        """Class-scoped on purpose.

        An interface property is adjacent syntax the grammar exposes cheaply,
        and that is not a reason to enlarge the concept. It gets its own fact
        when it has its own measured reason to exist.
        """
        path = tmp_path / "iface.ts"
        path.write_text(
            "export interface I { readonly x: string; y?: number; }\n", encoding="utf-8"
        )

        assert _names(path, "TypeScript") == []

    def test_a_default_expression_is_never_stored(self, tmp_path: Path) -> None:
        """Presence, never value. The expression is source."""
        path = tmp_path / "expr.ts"
        path.write_text(
            'export class C {\n  a = secretFactory("token");\n}\n', encoding="utf-8"
        )

        found = _attributes(path, "TypeScript")[0]

        assert found["has_default"] is True
        assert "secretFactory" not in str(found)
        assert "token" not in str(found)


class TestWhyTheBenchmarkSubjectMoved:
    """Recorded rather than quietly erased.

    The benchmark asked this question of ``PhaseTimeline``. It declares only
    ``__slots__`` at class-body level -- its actual state is bound in
    ``__init__`` -- so C3 would have returned one row, scored *stored*, and
    answered nothing anyone wanted. The subject moved to one whose true answer
    is non-empty, and this asserts why.
    """

    def test_phase_timeline_declares_only_slots(self) -> None:
        path = Path(__file__).resolve().parents[1] / "rey_lib/logs/phase_timeline.py"

        found = [
            record for record in _attributes(path, "Python")
            if record["owner"] == "PhaseTimeline"
        ]

        assert [record["name"] for record in found] == ["__slots__"]

    def test_parameter_record_declares_its_attributes(self) -> None:
        """The replacement subject's answer is non-empty and non-trivial."""
        path = Path(__file__).resolve().parents[1] / "rey_lib/repository_map/records.py"

        found = [
            record for record in _attributes(path, "Python")
            if record["owner"] == "ParameterRecord"
        ]

        assert [record["name"] for record in found] == [
            "source_path", "owner_qualified_name", "owner_line", "owner_column",
            "name", "ordinal", "parameter_kind", "has_default", "is_optional",
            "annotation",
        ]
        assert all(record["is_annotated"] for record in found)
        assert [record["name"] for record in found if record["has_default"]] == [
            "has_default", "is_optional", "annotation",
        ]
