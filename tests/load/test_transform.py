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
            "transform", "transform-file", "http-connection", "http-adapter",
            "http-options", "http-transform", "declaration", "persistence",
        )

    def test_the_loader_parameters_are_the_typed_subset(self) -> None:
        # declaration and persistence are the Transform's fields, and not
        # parameters any loader invocation types.
        assert TRANSFORM_PARAMETERS == (
            "transform", "transform-file", "http-connection", "http-adapter", "http-options",
            "http-transform",
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

    def test_it_keeps_neither_the_source_nor_the_context(self, tmp_path: Any) -> None:
        from types import SimpleNamespace

        from rey_lib.load import Source

        source = Source({"file": self._csv(tmp_path)})
        ctx = SimpleNamespace()
        built = Transform.from_source(ctx, source)

        assert all(value is not source and value is not ctx for value in vars(built).values())
        assert built.declaration() == Transform().declaration()


class TestFormFields:
    """The fields drawn beside each grid: never the field the grid authors (backlog 679)."""

    def test_each_kind_less_its_authored_field(self) -> None:
        assert Transform.form_fields() == {
            "identity": [],
            "declaration": [],
            "yaml": ["transform-file"],
            "http": ["http-connection", "http-adapter"],
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
