"""The Transform: one canonical object for what is done to a load's records.

What is asserted is the contract every entry point relies on -- which kinds
exist, which fields are loader parameters, what a selection keeps, what
validation refuses, that the declarative form round-trips, and that resolution
goes through the one existing builder, with its own call shape, and leaves
nothing live behind.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from rey_lib.data.column_transform import (
    ColumnTransform,
    TransformPersistence,
    authorable_starters,
)
from rey_lib.data.data_transform import IdentityTransform
from rey_lib.errors.error_utils import ConfigError
from rey_lib.load import Transform
from rey_lib.load import load_operation
from rey_lib.load.transform import (
    TRANSFORM_FIELDS,
    TRANSFORM_KINDS,
    TRANSFORM_PARAMETERS,
)
from rey_lib.errors.error_utils import StateError
from rey_lib.load.execution import load_arguments
from rey_lib.load.source import Source
from rey_lib.load.target import Target
from rey_lib.web_utils.operation import HttpOperation

_DECLARED = {"columns": [{"source": "A", "name": "a"}]}
_STORED = {"transform_id": 7, "file_type_id": 4, "column_ids": [31]}


@pytest.fixture()
def builds(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    """Wrap the real builder, recording each call exactly as it was made."""
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    real = load_operation._build_transform

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        built = real(*args, **kwargs)
        calls[-1] = (args, {**kwargs, "_returned": built})
        return built

    monkeypatch.setattr(load_operation, "_build_transform", spy)
    return calls


class TestTheVocabulary:

    def test_the_kinds_are_the_five(self) -> None:
        # http offered since backlog 668.
        assert Transform.kinds() == ("identity", "declaration", "yaml", "http", "manifest")

    def test_the_fields_are_everything_the_transform_holds(self) -> None:
        assert TRANSFORM_FIELDS == (
            "transform", "transform-file", "http-adapter", "http-transform", "http-request",
            "declaration", "persistence",
        )

    def test_the_loader_parameters_are_the_typed_subset(self) -> None:
        # declaration and persistence are the Transform's fields, and not
        # parameters any loader invocation types.
        assert TRANSFORM_PARAMETERS == (
            "transform", "transform-file", "http-adapter", "http-transform", "http-request",
        )
        assert set(TRANSFORM_PARAMETERS) <= set(TRANSFORM_FIELDS)

    def test_persistence_is_never_required_to_execute(self) -> None:
        manifest = next(kind for kind in TRANSFORM_KINDS if kind.id == "manifest")
        assert manifest.required == ("declaration",)


class TestTheSelection:

    def test_nothing_said_is_identity(self) -> None:
        assert Transform().selected_kind() == "identity"

    def test_a_choice_wins(self) -> None:
        assert Transform({"transform": "x"}, selected="yaml").selected_kind() == "yaml"

    @pytest.mark.parametrize(("values", "kind"), [
        ({"transform": "columns: []"}, "declaration"),
        ({"transform-file": "/t.yaml"}, "yaml"),
        ({"declaration": _DECLARED}, "manifest"),
    ])
    def test_unchosen_it_is_the_kind_whose_own_fields_carry_values(
        self, values: dict[str, Any], kind: str,
    ) -> None:
        assert Transform(values).selected_kind() == kind

    def test_switching_kinds_keeps_what_was_said_under_the_other(self) -> None:
        transform = Transform({"transform": "inline", "transform-file": "/t.yaml"})

        transform.select("yaml")
        transform.select("declaration")

        assert transform.configuration() == {"transform": "inline"}
        assert transform.value("transform-file") == "/t.yaml"

    def test_an_unknown_kind_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="no transform kind"):
            Transform().select("sql")

    def test_a_field_that_is_not_a_transform_field_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="not a transform field"):
            Transform().update("table", "public.orders")


class TestValidation:

    @pytest.mark.parametrize(("kind", "needs"), [
        ("identity", []),
        ("declaration", ["transform"]),
        ("yaml", ["transform-file"]),
        ("manifest", ["declaration"]),
    ])
    def test_each_kind_names_what_it_still_needs(self, kind: str, needs: list[str]) -> None:
        assert Transform(selected=kind).validate() == needs

    def test_a_declaration_without_persistence_is_complete(self) -> None:
        assert Transform({"declaration": _DECLARED}).validate() == []


class TestTheDeclarativeForm:

    def test_it_round_trips(self) -> None:
        transform = Transform(
            {"declaration": _DECLARED, "persistence": _STORED, "transform": "x"},
            selected="manifest",
        )

        again = Transform.from_declaration(transform.declaration())

        assert again.declaration() == transform.declaration()
        assert again.selected_kind() == "manifest"

    def test_it_holds_nothing_live(self) -> None:
        declared = Transform({"declaration": _DECLARED, "persistence": _STORED}).declaration()

        # Plain data all the way down: it survives JSON unchanged.
        assert json.loads(json.dumps(declared)) == declared


class TestResolutionThroughTheOneBuilder:

    def test_identity_is_built_with_the_builders_own_call_shape(
        self, builds: list[tuple[tuple[Any, ...], dict[str, Any]]],
    ) -> None:
        ctx = SimpleNamespace()

        resolved = Transform().resolve(ctx)

        (args, kwargs), = builds
        assert args == (ctx, None, None)
        assert kwargs["_returned"] is resolved
        assert isinstance(resolved, IdentityTransform)

    def test_a_manifest_with_persistence_passes_it_through(
        self, builds: list[tuple[tuple[Any, ...], dict[str, Any]]],
    ) -> None:
        ctx = SimpleNamespace()

        resolved = Transform(
            {"declaration": _DECLARED, "persistence": _STORED},
        ).resolve(ctx)

        (args, kwargs), = builds
        assert args == (ctx, None, _DECLARED)
        assert kwargs["persistence"] == TransformPersistence(7, 4, (31,))
        assert kwargs["_returned"] is resolved
        assert isinstance(resolved, ColumnTransform)
        assert resolved.is_persistable

    def test_a_manifest_without_persistence_resolves_and_is_not_persistable(
        self, builds: list[tuple[tuple[Any, ...], dict[str, Any]]],
    ) -> None:
        ctx = SimpleNamespace()

        resolved = Transform({"declaration": _DECLARED}).resolve(ctx)

        (args, kwargs), = builds
        assert args == (ctx, None, _DECLARED)
        assert set(kwargs) == {"_returned"}
        assert isinstance(resolved, ColumnTransform)
        assert not resolved.is_persistable

    def test_a_manifest_set_by_hand_resolves_as_a_hydrated_one(self) -> None:
        by_hand = Transform()
        by_hand.update("declaration", _DECLARED)
        by_hand.update("persistence", _STORED)
        hydrated = Transform.from_declaration(
            {"values": {"declaration": _DECLARED, "persistence": _STORED}},
        )

        one = by_hand.resolve(SimpleNamespace())
        other = hydrated.resolve(SimpleNamespace())

        assert (one.persistence, one.columns) == (other.persistence, other.columns)


class TestResolution:

    @pytest.mark.parametrize("given", [
        "columns:\n  - {source: A, name: a}\n",
        json.dumps(_DECLARED),
        _DECLARED,
    ])
    def test_an_inline_declaration_resolves_to_a_column_transform(self, given: Any) -> None:
        resolved = Transform({"transform": given}).resolve(SimpleNamespace())

        assert isinstance(resolved, ColumnTransform)
        assert resolved.columns == ["a"]

    def test_a_file_is_read_into_a_column_transform(self, tmp_path: Path) -> None:
        held = tmp_path / "t.yaml"
        held.write_text("columns:\n  - {source: A, name: a}\n", encoding="utf-8")

        resolved = Transform({"transform-file": str(held)}).resolve(SimpleNamespace())

        assert isinstance(resolved, ColumnTransform)
        assert resolved.columns == ["a"]

    @pytest.mark.parametrize(("content", "refused"), [
        (None, "no such file"),
        ("   ", "holds no declaration"),
        ("row_filter: x\n", "declares no columns"),
        ("columns: [unclosed\n", "could not be read"),
    ])
    def test_a_file_that_is_not_a_declaration_is_refused(
        self, tmp_path: Path, content: Optional[str], refused: str,
    ) -> None:
        held = tmp_path / "t.yaml"
        if content is not None:
            held.write_text(content, encoding="utf-8")

        with pytest.raises(ConfigError, match=refused):
            Transform({"transform-file": str(held)}).resolve(SimpleNamespace())

    def test_an_incomplete_selection_is_refused_before_anything_is_built(
        self, builds: list[tuple[tuple[Any, ...], dict[str, Any]]],
    ) -> None:
        with pytest.raises(ConfigError, match="transform-file"):
            Transform(selected="yaml").resolve(SimpleNamespace())
        assert builds == []

    def test_secrets_are_resolved_at_resolution_and_never_held(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("REY_TEST_TRANSFORM_KEY", "k" * 32)
        declared = {"columns": [{
            "source": "A", "name": "a",
            "transform": {"type": "encrypt", "key_env": "REY_TEST_TRANSFORM_KEY"},
        }]}
        transform = Transform({"transform": declared})
        before = transform.declaration()

        resolved = transform.resolve(SimpleNamespace())

        assert resolved.secrets == {"REY_TEST_TRANSFORM_KEY": "k" * 32}
        assert transform.declaration() == before
        assert "k" * 32 not in json.dumps(transform.declaration())


class TestTheBuilderCarriesPersistence:

    def test_a_call_with_persistence_passes_it_on(self) -> None:
        stored = TransformPersistence(7, 4, (31,))

        built = load_operation._build_transform(
            SimpleNamespace(), None, _DECLARED, persistence=stored,
        )

        assert built.persistence == stored

    def test_a_call_without_it_is_unchanged(self) -> None:
        built = load_operation._build_transform(SimpleNamespace(), None, _DECLARED)

        assert isinstance(built, ColumnTransform)
        assert built.persistence is None


def _declared(transform: Transform) -> dict[str, Any]:
    """The authored declaration, as the selected kind holds it."""
    held = transform.in_force_declaration()
    assert held is not None
    return held


def _authoring(*source: str, declared: Optional[dict[str, Any]] = None) -> Transform:
    """A Declaration transform that has been told what the source carries."""
    transform = Transform(
        {"transform": json.dumps(declared) if declared else ""}, selected="declaration",
    )
    transform.observe_source_columns(list(source))
    return transform


class TestTheCompletedView:

    def test_it_lists_the_source_columns_before_anything_is_declared(self) -> None:
        transform = _authoring("feature_id", "feature_name")

        assert transform.columns() == [
            {"source": "feature_id", "name": "feature_id"},
            {"source": "feature_name", "name": "feature_name"},
        ]

    def test_looking_is_not_authoring(self) -> None:
        transform = _authoring("a", "b")

        transform.columns()

        assert transform.value("transform") == ""

    def test_the_source_columns_are_context_never_configuration(self) -> None:
        transform = _authoring("observed_col_one", "observed_col_two")

        assert "observed_col" not in json.dumps(transform.declaration())


class TestEditingOneField:

    def test_the_first_edit_names_every_source_column(self) -> None:
        transform = _authoring("a", "b", "c")

        transform.edit_column(1, "name", "b_out")

        assert _declared(transform)["columns"] == [
            {"source": "a", "name": "a"},
            {"source": "b", "name": "b_out"},
            {"source": "c", "name": "c"},
        ]

    def test_completing_a_partial_declaration_reorders_no_existing_entry(self) -> None:
        transform = _authoring("A", "B", "C", declared={"columns": [
            {"source": "A", "name": "a"},
            {"name": "x", "transform": {"type": "constant", "value": "1"}},
            {"source": "B", "name": "b"},
            {"name": "digest", "transform": {"type": "hash", "columns": ["a", "b"]}},
        ]})

        transform.edit_column(0, "name", "a_out")

        assert _declared(transform)["columns"] == [
            {"source": "A", "name": "a_out"},
            {"name": "x", "transform": {"type": "constant", "value": "1"}},
            {"source": "B", "name": "b"},
            {"source": "C", "name": "C"},
            {"name": "digest", "transform": {"type": "hash", "columns": ["a", "b"]}},
        ]

    def test_a_missing_column_before_every_named_one_goes_before_the_first(self) -> None:
        transform = _authoring("A", "B", declared={"columns": [{"source": "B", "name": "b"}]})

        transform.edit_column(1, "name", "b2")

        assert [one["source"] for one in _declared(transform)["columns"]] == ["A", "B"]

    def test_with_nothing_source_backed_it_goes_at_the_end(self) -> None:
        transform = _authoring("A", declared={"columns": [{"name": "k"}]})

        transform.edit_column(0, "name", "k2")

        assert _declared(transform)["columns"] == [
            {"name": "k2"}, {"source": "A", "name": "A"},
        ]

    def test_repointing_the_input_changes_source_and_nothing_else(self) -> None:
        transform = _authoring("a", "b")
        transform.edit_column(0, "name", "keep_me")
        transform.edit_column(0, "type", "numeric")
        starter = _declared(transform)["columns"][0]["transform"]

        transform.edit_column(0, "source", "b")

        assert _declared(transform)["columns"][0] == {
            "source": "b", "name": "keep_me", "transform": starter,
        }

    def test_clearing_the_input_removes_the_source(self) -> None:
        transform = _authoring("a")

        transform.edit_column(0, "source", "")

        assert _declared(transform)["columns"][0] == {"name": "a"}

    def test_export_is_written_only_to_say_false(self) -> None:
        transform = _authoring("a")

        transform.edit_column(0, "export", "false")
        assert _declared(transform)["columns"][0] == {"source": "a", "name": "a", "export": False}

        transform.edit_column(0, "export", "true")
        assert _declared(transform)["columns"][0] == {"source": "a", "name": "a"}

    def test_a_datatype_is_written_when_stated_and_removed_when_cleared(self) -> None:
        transform = _authoring("a")

        transform.edit_column(0, "datatype", "decimal")
        assert _declared(transform)["columns"][0]["datatype"] == "decimal"

        transform.edit_column(0, "datatype", "")
        assert _declared(transform)["columns"][0] == {"source": "a", "name": "a"}

    def test_choosing_a_type_copies_the_starter(self) -> None:
        transform = _authoring("a", "b")

        transform.edit_column(0, "type", "date")
        transform.edit_column(1, "type", "date")
        first, second = _declared(transform)["columns"]
        first["transform"]["format"] = "changed"

        assert second["transform"]["type"] == "date"
        assert second["transform"].get("format") != "changed"
        # ...and the library's own starter is untouched too.
        assert authorable_starters()["date"].get("format") != "changed"

    def test_a_type_with_no_starter_leaves_the_entry_alone(self) -> None:
        transform = _authoring("a", "b", declared={"columns": [
            {"source": "a", "name": "a", "transform": {"type": "encrypt", "key_env": "K"}},
            {"source": "b", "name": "b"},
        ]})

        transform.edit_column(1, "type", "encrypt")

        assert _declared(transform)["columns"] == [
            {"source": "a", "name": "a", "transform": {"type": "encrypt", "key_env": "K"}},
            {"source": "b", "name": "b"},
        ]

    def test_the_transform_field_sets_the_rule_or_removes_it(self) -> None:
        transform = _authoring("a")

        transform.edit_column(0, "transform", {"type": "date", "format": "yyyy-MM-dd"})
        assert _declared(transform)["columns"][0]["transform"] == {
            "type": "date", "format": "yyyy-MM-dd",
        }

        transform.edit_column(0, "transform", None)
        assert _declared(transform)["columns"][0] == {"source": "a", "name": "a"}

    def test_the_declaration_is_written_back_as_the_parameters_json_text(self) -> None:
        transform = _authoring("a")

        transform.edit_column(0, "name", "a_out")

        assert json.loads(transform.value("transform")) == {
            "columns": [{"source": "a", "name": "a_out"}],
        }

    def test_the_rest_of_the_declaration_is_kept(self) -> None:
        transform = _authoring("a", declared={
            "columns": [{"source": "a", "name": "a"}], "row_filter": "a is not null",
        })

        transform.edit_column(0, "name", "b")

        assert _declared(transform)["row_filter"] == "a is not null"

    @pytest.mark.parametrize(("at", "field"), [(5, "name"), (0, "colour")])
    def test_a_missing_position_or_an_unknown_field_is_refused(
        self, at: int, field: str,
    ) -> None:
        with pytest.raises(ConfigError):
            _authoring("a").edit_column(at, field, "x")


class TestAddingAndMoving:

    def test_add_inserts_after_a_column_from_the_same_source(self) -> None:
        transform = _authoring("amount", "other")

        transform.add_column(0)

        assert _declared(transform)["columns"] == [
            {"source": "amount", "name": "amount"},
            {"source": "amount", "name": "amount"},
            {"source": "other", "name": "other"},
        ]

    def test_add_appends_when_no_column_is_given(self) -> None:
        transform = _authoring("a")

        transform.add_column()

        assert _declared(transform)["columns"] == [{"source": "a", "name": "a"}, {"name": ""}]

    def test_move_puts_one_column_at_another_position(self) -> None:
        transform = _authoring("a", "b", "c")

        transform.move_column(2, 0)

        assert [one["source"] for one in _declared(transform)["columns"]] == ["c", "a", "b"]

    def test_a_move_to_where_it_is_authors_nothing(self) -> None:
        transform = _authoring("a", "b")

        transform.move_column(0, 0)

        assert transform.value("transform") == ""


class TestWhereAuthoringLands:

    def test_identity_authors_nothing_and_says_to_choose_declaration(self) -> None:
        transform = Transform(selected="identity")
        transform.observe_source_columns(["a"])

        with pytest.raises(StateError, match="Declaration"):
            transform.edit_column(0, "name", "b")

    def test_a_declaration_file_is_not_edited_here(self) -> None:
        transform = Transform({"transform-file": "/t.yaml"}, selected="yaml")
        transform.observe_source_columns(["a"])

        with pytest.raises(StateError, match="where it lives"):
            transform.add_column()

    def test_hand_written_yaml_is_left_as_it_was_written(self) -> None:
        text = "columns:\n  - {source: a, name: a}\n"
        transform = Transform({"transform": text}, selected="declaration")

        with pytest.raises(ConfigError, match="not JSON"):
            transform.add_column()
        assert transform.value("transform") == text
        assert transform.in_force_declaration() is None

    def test_a_manifest_keeps_its_stored_ids_aligned(self) -> None:
        transform = Transform({
            "declaration": {"columns": [{"source": "b", "name": "b"}]},
            "persistence": {"transform_id": 7, "file_type_id": 4, "column_ids": [31]},
        }, selected="manifest")
        transform.observe_source_columns(["a", "b"])

        # Completion puts `a` before `b`, with no stored id of its own.
        transform.add_column(1)
        assert transform.value("persistence")["column_ids"] == [None, 31, None]

        transform.move_column(1, 0)
        assert transform.value("persistence")["column_ids"] == [31, None, None]
        assert [one.get("source") for one in _declared(transform)["columns"]] == ["b", "a", "b"]
        assert transform.value("persistence")["transform_id"] == 7

    def test_a_manifest_without_persistence_authors_and_manufactures_none(self) -> None:
        transform = Transform(
            {"declaration": {"columns": [{"source": "a", "name": "a"}]}}, selected="manifest",
        )
        transform.observe_source_columns(["a", "b"])

        transform.edit_column(0, "name", "a_out")
        transform.add_column(0)
        transform.move_column(2, 0)

        assert [one["name"] for one in _declared(transform)["columns"]] == ["b", "a_out", "a"]
        assert transform.value("persistence") is None
        assert "persistence" not in transform.declaration()["values"]


class TestTheInForceDeclaration:

    @pytest.mark.parametrize(("values", "kind", "expected"), [
        ({}, "identity", None),
        ({"transform": json.dumps(_DECLARED)}, "declaration", _DECLARED),
        ({"transform": "columns: []"}, "declaration", None),
        ({"transform-file": "/t.yaml"}, "yaml", None),
        ({"declaration": _DECLARED}, "manifest", _DECLARED),
    ])
    def test_for_each_kind(
        self, values: dict[str, Any], kind: str, expected: Optional[dict[str, Any]],
    ) -> None:
        assert Transform(values, selected=kind).in_force_declaration() == expected


class TestFromSource:
    """The Transform reads its Source to create itself (backlog 677)."""

    @staticmethod
    def _csv(tmp_path: Any) -> str:
        held = tmp_path / "in.csv"
        held.write_text("a,b\n1,x\n", encoding="utf-8")
        return str(held)

    def test_it_starts_from_the_columns_the_source_carries(self, tmp_path: Any) -> None:
        from types import SimpleNamespace

        from rey_lib.load import Source

        built = Transform.from_source(
            SimpleNamespace(), Source({"file": self._csv(tmp_path)}),
            {"selected": "declaration"},
        )

        assert built.source_columns() == ["a", "b"]
        assert [one.get("source") for one in built.columns()] == ["a", "b"]

    def test_a_given_declaration_is_kept_as_it_is(self, tmp_path: Any) -> None:
        from types import SimpleNamespace

        from rey_lib.load import Source

        declared = {"columns": [{"source": "a", "name": "renamed"}]}
        built = Transform.from_source(
            SimpleNamespace(), Source({"file": self._csv(tmp_path)}),
            {"values": {"transform": json.dumps(declared)}, "selected": "declaration"},
        )

        assert built.executed_declaration()["columns"][0]["name"] == "renamed"
        assert built.source_columns() == ["a", "b"]

    def test_an_incomplete_source_starts_with_no_columns(self) -> None:
        from types import SimpleNamespace

        from rey_lib.load import Source

        built = Transform.from_source(SimpleNamespace(), Source(selected="database"))

        assert built.source_columns() == []

    def test_an_unreadable_source_starts_with_no_columns(self, tmp_path: Any) -> None:
        from types import SimpleNamespace

        from rey_lib.load import Source

        built = Transform.from_source(
            SimpleNamespace(), Source({"file": str(tmp_path / "absent.csv")}),
        )

        assert built.source_columns() == []

    def test_it_references_its_source_and_target_never_the_context(
        self, tmp_path: Any,
    ) -> None:
        from types import SimpleNamespace

        from rey_lib.load import Source, Target

        source = Source({"file": self._csv(tmp_path)})
        target = Target()
        ctx = SimpleNamespace()
        built = Transform.from_source(ctx, source, target=target)

        assert built.source() is source
        assert any(value is target for value in vars(built).values())
        assert all(value is not ctx for value in vars(built).values())
        assert built.declaration() == Transform().declaration()


class TestItsSourceAndTarget:
    """The Transform reads and previews the Source it references (backlog 831).

    Moved from LinkedLoad (backlog 681), which held the same two objects and is
    gone. Observing the Source tells the Transform its columns; a preview runs
    the REFERENCED Source and this Transform, so an edit to either is what the
    next preview executes; an incomplete Transform is refused; nothing about a
    preview is kept; and a released Source or Target is no longer referenced.
    """

    @staticmethod
    def _csv(tmp_path: Path) -> Path:
        held = tmp_path / "in.csv"
        held.write_text("a,b\n1,x\n2,y\n3,z\n", encoding="utf-8")
        return held

    @staticmethod
    def _from(source: Any, declaration: Optional[dict[str, Any]] = None, **given: Any) -> Transform:
        return Transform.from_source(SimpleNamespace(), source, declaration, **given)

    @classmethod
    def _declared(cls, tmp_path: Path) -> Transform:
        from rey_lib.load import Source

        return cls._from(Source({"file": str(cls._csv(tmp_path))}), {"selected": "declaration"})

    def test_the_transform_reads_its_source_columns_again(self, tmp_path: Path) -> None:
        other = tmp_path / "other.csv"
        other.write_text("c,d\n1,2\n", encoding="utf-8")
        transform = self._declared(tmp_path)

        transform.source().update("file", str(other))

        assert transform.observe_source(SimpleNamespace()) is True
        assert transform.source_columns() == ["c", "d"]

    def test_a_source_that_cannot_be_read_tells_it_nothing(self, tmp_path: Path) -> None:
        from rey_lib.load import Source

        transform = self._from(Source({"file": str(tmp_path / "no.unknown")}))
        transform.observe_source_columns(["kept"])

        assert transform.observe_source(SimpleNamespace()) is False
        assert transform.source_columns() == ["kept"]

    def test_it_previews_the_mapping_it_shows(self, tmp_path: Path) -> None:
        found = self._declared(tmp_path).preview(SimpleNamespace(), 10)

        assert list(found.columns) == ["a", "b"]
        assert [row["a"] for row in found.rows] == ["1", "2", "3"]

    def test_an_edit_to_the_transform_is_what_the_next_preview_runs(
        self, tmp_path: Path,
    ) -> None:
        transform = self._declared(tmp_path)
        transform.preview(SimpleNamespace(), 10)

        transform.edit_column(0, "name", "a_out")

        assert list(transform.preview(SimpleNamespace(), 10).columns) == ["a_out", "b"]

    def test_an_edit_to_the_referenced_source_is_what_the_next_preview_runs(
        self, tmp_path: Path,
    ) -> None:
        shorter = tmp_path / "shorter.csv"
        shorter.write_text("a,b\n9,8\n", encoding="utf-8")
        transform = self._declared(tmp_path)

        transform.source().update("file", str(shorter))

        assert transform.preview(SimpleNamespace(), 10).rows == ({"a": "9", "b": "8"},)

    def test_an_incomplete_transform_is_refused(self, tmp_path: Path) -> None:
        from rey_lib.load import Source

        transform = self._from(Source({"file": str(self._csv(tmp_path))}), {"selected": "declaration"})
        transform.observe_source_columns([])

        with pytest.raises(ConfigError, match="is missing: transform"):
            transform.preview(SimpleNamespace(), 10)

    def test_nothing_about_a_preview_is_kept(self, tmp_path: Path) -> None:
        from rey_lib.load import Source

        transform = self._from(
            Source({"file": str(self._csv(tmp_path))}),
            {"values": {"transform": json.dumps({"columns": [{"source": "a", "name": "a"}]})}},
        )
        before = (dict(vars(transform)), transform.declaration())

        transform.preview(SimpleNamespace(), 10)

        assert (dict(vars(transform)), transform.declaration()) == before

    def test_a_released_source_cannot_be_previewed(self, tmp_path: Path) -> None:
        transform = self._declared(tmp_path)

        transform.release(transform.source())

        assert transform.source() is None
        assert transform.observe_source(SimpleNamespace()) is False
        with pytest.raises(StateError, match="no Source to preview"):
            transform.preview(SimpleNamespace(), 10)

    def test_a_released_target_drops_only_the_target(self, tmp_path: Path) -> None:
        from rey_lib.load import Source, Target

        source, target = Source({"file": str(self._csv(tmp_path))}), Target()
        transform = self._from(source, target=target)

        transform.release(target)

        assert transform.source() is source
        assert all(value is not target for value in vars(transform).values())

    def test_releasing_anything_else_changes_nothing(self, tmp_path: Path) -> None:
        from rey_lib.load import Source, Target

        source, target = Source({"file": str(self._csv(tmp_path))}), Target()
        transform = self._from(source, target=target)
        before = dict(vars(transform))

        transform.release(Source({"file": str(self._csv(tmp_path))}))
        transform.release(None)

        assert dict(vars(transform)) == before


class TestFormFields:
    """The fields drawn beside each grid: never the field the grid authors (backlog 679)."""

    def test_each_kind_less_its_authored_field(self) -> None:
        assert Transform.form_fields() == {
            "identity": [],
            "declaration": [],
            "yaml": ["transform-file"],
            "http": [],
            "manifest": ["persistence"],
        }

    def test_the_authored_fields_are_still_fields_of_their_kinds(self) -> None:
        assert {"transform", "http-transform", "declaration"} <= set(TRANSFORM_FIELDS)
        held = Transform({"transform": "{}", "http-transform": "{}"})
        assert held.value("transform") == "{}"
        assert held.value("http-transform") == "{}"


class TestWhatIsShownIsWhatExecutes:
    """The mapping the grid shows is the mapping that executes (backlog 681)."""

    def test_an_untouched_grid_is_a_complete_declaration(self) -> None:
        shown = _authoring("a", "b")

        assert shown.validate() == []
        assert shown.executed_declaration() == {
            "columns": [{"source": "a", "name": "a"}, {"source": "b", "name": "b"}],
        }

    def test_a_partial_declaration_executes_every_column_it_shows(self) -> None:
        shown = _authoring("a", "b", declared={"columns": [{"source": "a", "name": "a_out"}]})

        assert shown.executed_declaration()["columns"] == shown.columns()
        assert [one["name"] for one in shown.executed_declaration()["columns"]] == ["a_out", "b"]

    def test_hand_written_text_executes_as_it_stands(self) -> None:
        written = Transform(
            {"transform": "columns:\n  - source: a\n    name: a_out\n"}, selected="declaration",
        )
        written.observe_source_columns(["a", "b"])

        assert written.executed_declaration() == {"columns": [{"source": "a", "name": "a_out"}]}

    def test_without_source_columns_the_declaration_is_unchanged(self) -> None:
        # THE CLI SHAPE: nothing observed, so only what was declared executes.
        declared = {"columns": [{"source": "a", "name": "a_out"}]}
        told = Transform({"transform": json.dumps(declared)}, selected="declaration")

        assert told.executed_declaration() == declared

    def test_nothing_shown_still_needs_its_declaration(self) -> None:
        assert Transform(selected="declaration").validate() == ["transform"]

# -- the HTTP selection (moved from test_http_transform_source.py, backlog 837) --

_MAPPING = '{"columns": [{"name": "code", "source": "code"}]}'


def _job(key: str, **overrides: Any) -> dict[str, Any]:
    """A neutral job row, as f_http_operation_get shapes it."""
    job: dict[str, Any] = {
        "adapter_key": key, "connection_name": "site",
        "base_url": "https://api.example.test/v1/", "auth_header": "X-Api-Key",
        "auth_prefix": None, "auth_secret_ref": "env.example_api_key", "auth_required": True,
        "timeout_seconds": 5, "verify": True,
        "default_headers": {"User-Agent": "rey test", "Accept": "application/json"},
        "http_method": "POST", "path": "find", "request_format": "json",
        "response_format": "json", "is_enabled": True,
        "request_body_fields": ["idType", "idValue"],
        "request_body_kinds": ["literal", "reference"],
        "request_body_values": ["TICKER", "symbol"],
        "request_query_fields": ["limit"], "request_query_kinds": ["literal"],
        "request_query_values": ["10"],
        "response_field_names": ["found"], "response_field_paths": ["$.name"],
        "batch_size": 2, "correlation_mode": "position", "response_records_path": "$.data",
    }
    job.update(overrides)
    return job


class _Control:
    """Answers http_operation from fixed rows, recording every call."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[str] = []

    def http_operation(self, installation: str, adapter_key: Any = None) -> list[dict[str, Any]]:
        self.calls.append(installation)
        return [
            dict(row) for row in self.rows
            if adapter_key is None or row["adapter_key"] == adapter_key
        ]


def _control() -> _Control:
    return _Control([_job("alpha"), _job("beta", is_enabled=False), _job("gamma")])


def _runtime(control: Any) -> SimpleNamespace:
    """A runtime carrying its shared Control, as open_shared_control leaves one."""
    return SimpleNamespace(shared_control=control, installation=SimpleNamespace(name="local"))


def _established(control: Any, declaration: dict[str, Any] | None = None) -> Transform:
    """A Transform established as the Loader establishes one: from its Source."""
    return Transform.from_source(_runtime(control), Source.from_declaration({}), declaration)


_REQUEST_ALPHA = {
    "method": "POST",
    "fields": [
        {"part": "body", "name": "idType", "kind": "literal", "value": "TICKER", "label": None},
        {"part": "body", "name": "idValue", "kind": "reference", "value": "symbol", "label": None},
        {"part": "query", "name": "limit", "kind": "literal", "value": "10", "label": None},
    ],
    "headers": [
        {"part": "header", "name": "Accept", "kind": "configured", "value": "application/json"},
        {"part": "header", "name": "User-Agent", "kind": "configured", "value": "rey test"},
    ],
}


class TestTheOneRead:
    """Moved from HttpTransformSource (backlog 837): the one read, on the Transform."""

    def test_one_read_and_the_enabled_choices_in_read_order(self) -> None:
        control = _control()

        transform = _established(control, {"selected": "http"})

        assert control.calls == ["local"]
        assert transform.choices() == {"http-adapter": [
            {"value": "alpha", "label": ""}, {"value": "gamma", "label": ""},
        ]}
        assert transform.http_request() is None

    @pytest.mark.parametrize("key", ["beta", "missing", ""])
    def test_refuses_what_is_not_an_enabled_operation(self, key: str) -> None:
        transform = _established(_control(), {"selected": "http"})

        with pytest.raises(ConfigError, match="not an enabled operation"):
            transform.select_http_operation("http-adapter", key)
        assert transform.http_request() is None

    def test_no_credential_is_in_the_request(self) -> None:
        transform = _established(_control(), {"selected": "http", "values": {"http-adapter": "alpha"}})

        shown = repr(transform.http_request())

        assert "X-Api-Key" not in shown
        assert "env.example_api_key" not in shown


class TestOneSelectedOperation:
    """The held Transform shows and executes ONE HttpOperation (backlog 837)."""

    def test_the_request_shown_is_read_off_the_held_operation(self) -> None:
        transform = _established(_control(), {"selected": "http", "values": {"http-adapter": "alpha"}})

        assert transform.http_request() == _REQUEST_ALPHA

    def test_execution_uses_the_held_operation_and_reads_nothing(self) -> None:
        control = _control()
        transform = _established(control, {"selected": "http", "values": {"http-adapter": "alpha"}})
        shown = transform.resolve(_runtime(control)).operation

        assert control.calls == ["local"]
        assert transform.resolve(_runtime(control)).operation is shown

    def test_choosing_another_operation_is_what_executes_next(self) -> None:
        control = _control()
        transform = _established(control, {"selected": "http", "values": {"http-adapter": "alpha"}})

        transform.select_http_operation("http-adapter", "gamma")

        assert transform.resolve(_runtime(control)).operation.adapter_key == "gamma"
        assert control.calls == ["local"]

    def test_a_declarative_rebuild_reads_its_own_job(self) -> None:
        control = _control()
        rebuilt = Transform.from_declaration({
            "selected": "http", "values": {"http-adapter": "gamma"},
        })

        built = rebuilt.resolve(_runtime(control))

        assert control.calls == ["local"]
        assert built.operation.adapter_key == "gamma"


class TestChoosingTheKind:
    def test_selecting_http_reads_once_and_offers_the_operations(self) -> None:
        control = _control()
        transform = _established(control)

        transform.select("http")
        transform.select("http")

        assert control.calls == ["local"]
        assert transform.selected_kind() == "http"
        assert transform.choices() == {"http-adapter": [
            {"value": "alpha", "label": ""}, {"value": "gamma", "label": ""},
        ]}
        assert transform.field_operations() == {"http-adapter": "select_http_operation"}
        assert transform.http_request() is None

    def test_without_a_control_database_http_is_refused_and_nothing_changes(self) -> None:
        transform = _established(None, {"selected": "declaration"})

        with pytest.raises(ConfigError, match="control database"):
            transform.select("http")

        assert transform.selected_kind() == "declaration"
        assert transform.choices() == {} and transform.field_operations() == {}

    def test_a_refused_named_operation_leaves_the_kind_unchanged(self) -> None:
        transform = _established(_control(), {
            "selected": "declaration", "values": {"http-adapter": "beta"},
        })

        with pytest.raises(ConfigError, match="not an enabled operation"):
            transform.select("http")

        assert transform.selected_kind() == "declaration"
        assert transform.choices() == {}

    def test_leaving_http_lets_the_source_and_request_go(self) -> None:
        transform = _established(_control(), {
            "selected": "http", "values": {"http-adapter": "alpha"},
        })

        # As ManifestSource.populate does: no Console, no special path.
        transform.select("declaration")

        assert transform.http_request() is None
        assert transform.choices() == {} and transform.field_operations() == {}
        assert transform.value("http-adapter") == "alpha"

    def test_returning_to_http_reads_nothing_and_repopulates(self) -> None:
        control = _control()
        transform = _established(control, {"selected": "http", "values": {"http-adapter": "alpha"}})
        transform.select("declaration")

        transform.select("http")

        assert control.calls == ["local"]
        assert transform.http_request() == _REQUEST_ALPHA


class TestTheInitialHydration:
    """HTTP operations are hydrated once, at initialization; selection is in memory (841)."""

    def test_initialization_reads_once_whatever_kind_is_declared(self) -> None:
        control = _control()

        _established(control, {"selected": "declaration"})

        assert control.calls == ["local"]

    def test_selecting_http_and_an_operation_reads_nothing(self) -> None:
        control = _control()
        transform = _established(control, {"selected": "declaration"})

        transform.select("http")
        transform.select_http_operation("http-adapter", "gamma")

        assert control.calls == ["local"]

    def test_selecting_an_operation_hydrates_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from rey_lib.web_utils.operation import HttpOperation

        transform = _established(_control(), {"selected": "http"})
        monkeypatch.setattr(
            HttpOperation, "hydrate",
            classmethod(lambda cls, job: pytest.fail("selection hydrated an operation")),
        )

        transform.select_http_operation("http-adapter", "alpha")
        transform.select("declaration")
        transform.select("http")

        assert transform.value("http-adapter") == "alpha"

    def test_a_job_that_cannot_hydrate_is_not_offered_and_initialization_succeeds(self) -> None:
        broken = _Control([_job("alpha"), _job("broken", correlation_mode=None)])

        transform = _established(broken, {"selected": "declaration"})
        transform.select("http")

        assert [one["value"] for one in transform.choices()["http-adapter"]] == ["alpha"]


class TestEstablishment:
    def test_declared_in_http_it_is_established_and_reconciled(self) -> None:
        control = _control()

        transform = _established(control, {
            "selected": "http", "values": {"http-adapter": "gamma", "http-transform": _MAPPING},
        })

        assert control.calls == ["local"]
        assert transform.http_request()["method"] == "POST"
        assert transform.value("http-transform") == _MAPPING

    def test_declared_in_http_without_control_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="control database"):
            _established(None, {"selected": "http"})

    def test_a_declarative_rebuild_establishes_nothing(self) -> None:
        # What execution rebuilds from: no Control, no read, no refusal.
        transform = Transform.from_declaration({
            "selected": "http", "values": {"http-adapter": "alpha"},
        })

        assert transform.selected_kind() == "http"
        assert transform.choices() == {} and transform.http_request() is None


class TestChoosingTheOperation:
    def test_chosen_in_memory_with_its_request(self) -> None:
        control = _control()
        transform = _established(control, {"selected": "http"})

        transform.select_http_operation("http-adapter", "alpha")

        assert control.calls == ["local"]
        assert transform.value("http-adapter") == "alpha"
        assert transform.http_request() == _REQUEST_ALPHA

    def test_a_refused_operation_leaves_the_choice_as_it_was(self) -> None:
        transform = _established(_control(), {"selected": "http", "values": {"http-adapter": "alpha"}})

        with pytest.raises(ConfigError, match="not an enabled operation"):
            transform.select_http_operation("http-adapter", "beta")

        assert transform.value("http-adapter") == "alpha"
        assert transform.http_request() == _REQUEST_ALPHA

    def test_an_update_of_the_field_is_choosing_an_operation(self) -> None:
        transform = _established(_control(), {"selected": "http"})

        transform.update("http-adapter", "gamma")

        assert transform.http_request()["method"] == "POST"
        with pytest.raises(ConfigError, match="not an enabled operation"):
            transform.update("http-adapter", "beta")

    def test_needs_an_http_transform(self) -> None:
        with pytest.raises(StateError, match="not in kind http"):
            _established(_control(), {"selected": "declaration"}).select_http_operation(
                "http-adapter", "alpha",
            )

    def test_only_through_the_selection_field(self) -> None:
        with pytest.raises(ConfigError, match="is chosen through"):
            _established(_control(), {"selected": "http"}).select_http_operation(
                "http-transform", "alpha",
            )


class TestTheHeldStateIsNeverConfiguration:
    def test_it_is_neither_declared_nor_an_execution_argument(self) -> None:
        transform = _established(_control(), {"selected": "http", "values": {"http-adapter": "alpha"}})

        assert transform.declaration() == {
            "selected": "http", "values": {"http-adapter": "alpha"},
        }
        assert load_arguments(
            Source.from_declaration({}), transform, Target.from_declaration({}),
        ) == {"http-adapter": "alpha"}


class TestTheRequestSettingsAreChosenFromTheHydration:
    """Only what was hydrated can be chosen; the choice is declared state (backlog 841)."""

    @staticmethod
    def _mapped() -> _Control:
        # Synthetic governed rows: idType carries a value map; its inputs are the choices.
        return _Control([_job(
            "alpha",
            value_map_parts=["body", "body"], value_map_fields=["idType", "idType"],
            value_map_inputs=["ONE", "TWO"], value_map_outputs=["SENT_ONE", "SENT_TWO"],
        )])

    def _held(self, control: Any = None) -> Transform:
        transform = _established(control or self._mapped(), {
            "selected": "http", "values": {"http-adapter": "alpha"},
        })
        transform.observe_source_columns(["symbol", "identifier_value"])
        return transform

    @staticmethod
    def _field(transform: Transform, name: str) -> dict[str, Any]:
        return next(one for one in transform.http_request()["fields"] if one["name"] == name)

    def test_a_value_mapped_field_offers_its_map_inputs_in_order(self) -> None:
        assert self._field(self._held(), "idType")["choices"] == ["ONE", "TWO"]

    def test_a_field_without_a_value_map_offers_no_choices(self) -> None:
        assert "choices" not in self._field(self._held(), "idValue")

    def test_choosing_one_is_declared_state_and_shown(self) -> None:
        transform = self._held()

        transform.edit_request_field("body", "idType", "TWO")

        assert json.loads(transform.declaration()["values"]["http-request"]) == {"body": {"idType": "TWO"}}
        assert self._field(transform, "idType")["value"] == "TWO"

    def test_a_reference_is_never_chosen_from_the_source_columns(self) -> None:
        transform = self._held()

        with pytest.raises(ConfigError, match="is not a choice"):
            transform.edit_request_field("body", "idValue", "identifier_value")
        assert self._field(transform, "idValue")["value"] == "symbol"

    @pytest.mark.parametrize(("name", "value"), [("idType", "THREE"), ("idValue", "not_a_column")])
    def test_a_value_that_was_not_hydrated_is_refused(self, name: str, value: str) -> None:
        with pytest.raises(ConfigError, match="is not a choice"):
            self._held().edit_request_field("body", name, value)

    def test_a_setting_the_operation_does_not_have_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="no body setting 'missing'"):
            self._held().edit_request_field("body", "missing", "ONE")

    def test_without_an_operation_nothing_can_be_set(self) -> None:
        transform = _established(self._mapped(), {"selected": "http"})

        with pytest.raises(StateError, match="no HTTP operation is held"):
            transform.edit_request_field("body", "idType", "ONE")

    def test_execution_sends_the_choice_through_the_value_map(self) -> None:
        control = self._mapped()
        transform = self._held(control)
        transform.edit_request_field("body", "idType", "TWO")

        sent = transform.resolve(_runtime(control)).operation
        filled, refusal = sent._filled({"symbol": "AAPL"})

        assert refusal is None and filled["body"]["idType"] == "SENT_TWO"

    def test_a_rebuilt_transform_sends_the_same_choice(self) -> None:
        held = self._held()
        held.edit_request_field("body", "idType", "TWO")
        rebuilt = Transform.from_declaration(held.declaration())

        sent = rebuilt.resolve(_runtime(self._mapped())).operation
        filled, _ = sent._filled({"symbol": "AAPL"})

        assert filled["body"]["idType"] == "SENT_TWO"

    def test_it_is_not_drawn_as_a_form_field(self) -> None:
        assert "http-request" not in Transform.form_fields()["http"]

    def test_the_operation_is_chosen_in_the_request_not_the_kind_form(self) -> None:
        assert "http-adapter" not in Transform.form_fields()["http"]
        assert "http-adapter" in TRANSFORM_FIELDS

    def test_a_governed_label_is_shown_and_an_ungoverned_one_is_none(self) -> None:
        transform = self._held(_Control([_job(
            "alpha", request_body_labels=["Identifier type", None],
        )]))

        assert self._field(transform, "idType")["label"] == "Identifier type"
        assert self._field(transform, "idValue")["label"] is None
        assert self._field(transform, "limit")["label"] is None

    def test_two_references_keep_their_own_hydrated_defaults(self) -> None:
        # The live shape: two reference fields with different governed values,
        # and a Source whose FIRST column is neither of them.
        transform = self._held(_Control([_job(
            "alpha",
            request_body_fields=["idValue", "micCode"],
            request_body_kinds=["reference", "reference"],
            request_body_values=["symbol", "market_code"],
        )]))
        transform.observe_source_columns(["instrument_name", "symbol", "market_code"])

        assert self._field(transform, "idValue")["value"] == "symbol"
        assert self._field(transform, "micCode")["value"] == "market_code"
        assert "http-request" not in transform.declaration()["values"]

    def test_neither_reference_can_be_edited_and_both_keep_their_defaults(self) -> None:
        transform = self._held(_Control([_job(
            "alpha",
            request_body_fields=["idValue", "micCode"],
            request_body_kinds=["reference", "reference"],
            request_body_values=["symbol", "market_code"],
        )]))
        transform.observe_source_columns(["instrument_name", "symbol", "market_code"])

        for name in ("idValue", "micCode"):
            with pytest.raises(ConfigError, match="is not a choice"):
                transform.edit_request_field("body", name, "instrument_name")

        assert self._field(transform, "idValue")["value"] == "symbol"
        assert self._field(transform, "micCode")["value"] == "market_code"

    @staticmethod
    def _figi() -> _Control:
        # The governed shape (load.http_request_selected_reference).
        return _Control([_job(
            "alpha",
            request_body_fields=["idType", "idValue", "micCode"],
            request_body_kinds=["literal", "selected_reference", "reference"],
            request_body_values=["TICKER", "idType", "market_code"],
            value_map_parts=["body"] * 4,
            value_map_fields=["idType", "idType", "idValue", "idValue"],
            value_map_inputs=["ISIN", "TICKER", "ISIN", "TICKER"],
            value_map_outputs=["ID_ISIN", "TICKER", "isin", "ticker"],
        )])

    def test_only_the_identifier_type_is_chosen_and_idvalue_shows_its_column(self) -> None:
        transform = self._held(self._figi())

        assert self._field(transform, "idType")["choices"] == ["ISIN", "TICKER"]
        assert self._field(transform, "idValue")["value"] == "ticker"
        assert "choices" not in self._field(transform, "idValue")
        assert "choices" not in self._field(transform, "micCode")

    def test_choosing_isin_moves_idvalue_to_the_isin_column(self) -> None:
        transform = self._held(self._figi())

        transform.edit_request_field("body", "idType", "ISIN")

        assert self._field(transform, "idType")["value"] == "ISIN"
        assert self._field(transform, "idValue")["value"] == "isin"

    def test_the_selected_reference_is_never_edited(self) -> None:
        with pytest.raises(ConfigError, match="is not a choice"):
            self._held(self._figi()).edit_request_field("body", "idValue", "isin")

    def test_a_value_held_for_a_reference_is_refused_by_name(self) -> None:
        operation = HttpOperation.hydrate(self._figi().rows[0])

        with pytest.raises(ConfigError, match="'instrument_name' is not a choice for body setting 'idValue'"):
            operation.with_request_values({"body": {
                "idValue": "instrument_name", "micCode": "instrument_name",
            }})

    def test_the_operations_are_labelled_by_their_governed_display_name(self) -> None:
        transform = _established(_Control([
            _job("alpha", display_name="Alpha service"), _job("gamma"),
        ]), {"selected": "http"})

        assert transform.choices() == {"http-adapter": [
            {"value": "alpha", "label": "Alpha service"}, {"value": "gamma", "label": ""},
        ]}


class TestItsTargetsColumns:
    """Its held Target, asked for its columns when they are wanted (load.mapping_output_from_target_columns)."""

    _MAPPING = {"columns": [{"source": "A", "name": "a"}, {"source": "B", "name": "old_name"}]}

    @staticmethod
    def _target(answer: Any) -> SimpleNamespace:
        """A Target answering its destination columns, recording each read."""
        reads: list[Any] = []

        def destination_columns(ctx: Any) -> Any:
            reads.append(ctx)
            if isinstance(answer, Exception):
                raise answer
            return answer

        return SimpleNamespace(destination_columns=destination_columns, reads=reads)

    def _established(self, target: Any, *, governed: bool = True) -> Transform:
        """Established as the governed read leaves it: kind declaration, its mapping filled."""
        source = Source.from_declaration({})
        if governed:
            # A Source established from a governed Manifest read answers it.
            source._manifest = SimpleNamespace(populate_transform=lambda transform: None)
        return Transform.from_source(
            _runtime(_Control([])), source,
            {"selected": "declaration", "values": {"transform": json.dumps(self._MAPPING)}},
            target=target,
        )

    def test_established_it_reads_nothing_and_holds_its_target(self) -> None:
        target = self._target(["a", "z"])

        transform = self._established(target)

        assert target.reads == []
        assert transform.target() is target

    def test_it_asks_its_held_target_each_time(self) -> None:
        target = self._target(["a", "z"])
        transform = self._established(target)
        ctx = SimpleNamespace()

        assert transform.target_columns(ctx) == ["a", "z"]
        target.destination_columns = self._target(["a", "b", "c"]).destination_columns

        assert transform.target_columns(ctx) == ["a", "b", "c"]
        assert target.reads == [ctx]

    def test_one_read_gives_the_columns_and_the_missing_outputs(self) -> None:
        target = self._target(["a", "z"])
        transform = self._established(target)

        state = transform.target_mapping_state(SimpleNamespace())

        assert state == (["a", "z"], [False, True])
        assert len(target.reads) == 1
        assert [entry["name"] for entry in transform.columns()] == ["a", "old_name"]

    def test_without_a_governed_manifest_there_are_no_choices_and_nothing_missing(self) -> None:
        target = self._target(["a", "z"])
        transform = self._established(target, governed=False)

        assert transform.target_columns(SimpleNamespace()) is None
        assert not any(transform.target_mapping_state(SimpleNamespace())[1])
        assert target.reads == []

    @pytest.mark.parametrize("answer", [None, ConfigError("no such connection")])
    def test_no_table_or_a_refused_read_gives_no_choices(self, answer: Any) -> None:
        transform = self._established(self._target(answer))

        assert transform.target_columns(SimpleNamespace()) is None
        assert transform.target_mapping_state(SimpleNamespace()) == (None, [False, False])

    def test_a_released_target_is_not_asked(self) -> None:
        target = self._target(["a", "z"])
        transform = self._established(target)

        transform.release(target)

        assert transform.target() is None
        assert transform.target_columns(SimpleNamespace()) is None
        assert target.reads == []
