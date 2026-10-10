"""The Source and Transform own their governed transitions (backlog 830).

Driven with no Console: a Source established from a governed file holds its
one read; a Transform built from it takes its half of that read; choosing,
creating and deleting a saved setting go through the Transform, the one object
referencing both; a save re-reads through the Source; and every refusal is the
objects' own.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

import pytest

from rey_lib.errors.error_utils import ConfigError, StateError
from rey_lib.load import Source, Transform


def _rows(default: Optional[int] = 20) -> list[dict[str, Any]]:
    """Two transforms of one file type, each with its own query and one column."""
    def of(transform_id: int, column: str, sql: str) -> dict[str, Any]:
        return {
            "file_manifest_id": 10, "file_mutation_id": 25, "file_type_id": 4,
            "installation_id": 1, "path": "/data/in.csv", "layout": "DELIMITED_HEADER",
            "file_name": "in.csv", "data_profile_id": 7, "data_profile_field_id": 101,
            "profile_header_definition": "a", "field_name": "a", "field_ordinal": 1,
            "field_detected_type": "integer", "transform_id": transform_id,
            "transform_name": f"t{transform_id}",
            "transform_is_default": transform_id == default,
            "transform_row_filter": None, "transform_column_id": transform_id * 10,
            "column_name": column, "source_column": "a", "column_ordinal": 1,
            "column_datatype": None, "transform_type": "passthrough",
            "transform_config": None, "column_is_exported": True,
            "transform_query_id": transform_id * 100, "resolved_query_sql": sql,
            "mutation_choices": [],
        }
    return [of(20, "from_default", "select 'default'"), of(21, "from_other", "select 'other'")]


class _Control:
    """The source-context contract and the saved-setting writes, counting each call."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.reads = 0
        self.maintained: list[tuple[Any, dict[str, Any]]] = []
        self.queries: list[tuple[Any, Any]] = []

    def get_file_manifests(self, **_filters: Any) -> list[dict[str, Any]]:
        self.reads += 1
        return list(self.rows)

    def maintain_transform(self, file_type_id: Any, **given: Any) -> None:
        self.maintained.append((file_type_id, given))

    def update_transform_query_sql(self, transform_query_id: Any, query_sql: Any) -> None:
        self.queries.append((transform_query_id, query_sql))

    def update_transform_columns(self, _transform_id: Any, entries: list[Any]) -> list[int]:
        return [900 + at for at, _ in enumerate(entries)]


def _runtime(control: Any) -> SimpleNamespace:
    return SimpleNamespace(shared_control=control, installation=SimpleNamespace(name="local"))


def _opened(rows: Optional[list[dict[str, Any]]] = None) -> tuple[Any, Source, Transform, _Control]:
    control = _Control(rows if rows is not None else _rows())
    runtime = _runtime(control)
    source = Source.from_governed(runtime, file_manifest_id=10)
    return runtime, source, Transform.from_source(runtime, source), control


def _statement(transform: Transform) -> list[Any]:
    return [one.get("name") for one in transform.columns()]


class TestEstablishingFromTheGovernedFile:

    def test_the_source_is_established_and_populated_with_one_read(self) -> None:
        control = _Control(_rows())

        source = Source.from_governed(_runtime(control), file_manifest_id=10)

        assert control.reads == 1
        assert source.selected_kind() == "database"
        assert source.value("statement") == "select 'default'"
        assert source.governed_context()["selected_transform_id"] == 20
        assert source.manifest() is not None

    def test_the_held_read_is_not_configuration(self) -> None:
        _, source, _, _ = _opened()

        assert source.manifest() not in source.declaration()["values"].values()
        assert "manifest" not in source.declaration()

    def test_a_transform_built_on_it_takes_its_half_of_the_same_read(self) -> None:
        _, _, transform, control = _opened()

        assert control.reads == 1
        assert transform.selected_kind() == "declaration"
        assert _statement(transform) == ["from_default"]

    def test_no_control_database_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="no control database to read a governed file"):
            Source.from_governed(SimpleNamespace(shared_control=None), file_manifest_id=10)


class TestChoosingASavedSetting:

    def test_both_objects_are_rehydrated_and_nothing_is_read(self) -> None:
        runtime, source, transform, control = _opened()

        transform.select_saved_setting(runtime, 21)

        assert control.reads == 1
        assert source.value("statement") == "select 'other'"
        assert source.governed_context()["selected_transform_id"] == 21
        assert _statement(transform) == ["from_other"]

    def test_a_whole_number_is_required(self) -> None:
        runtime, _, transform, _ = _opened()

        with pytest.raises(ConfigError, match="a whole number"):
            transform.select_saved_setting(runtime, "twenty")

    def test_a_transform_on_a_plain_source_is_refused(self) -> None:
        transform = Transform.from_source(SimpleNamespace(), Source({"file": "/no/such.csv"}))

        with pytest.raises(StateError, match="not opened on a governed file"):
            transform.select_saved_setting(SimpleNamespace(), 21)


class TestNewAndDelete:

    def test_new_is_maintained_from_the_selected_setting(self) -> None:
        runtime, _, transform, control = _opened()

        transform.new_saved_setting(runtime)

        assert control.maintained == [(4, {"action": "new", "transform_id": 20})]
        assert control.reads == 2

    def test_delete_drops_the_selected_and_rehydrates_without_a_read(self) -> None:
        runtime, source, transform, control = _opened()
        transform.select_saved_setting(runtime, 21)

        transform.delete_saved_setting(runtime)

        assert [given["action"] for _, given in control.maintained] == ["delete"]
        assert control.reads == 1
        assert source.governed_context()["selected_transform_id"] == 20

    def test_a_released_source_has_no_saved_settings(self) -> None:
        runtime, source, transform, _ = _opened()

        transform.release(source)

        with pytest.raises(StateError, match="not opened on a governed file"):
            transform.new_saved_setting(runtime)


class TestSavingReReadsThroughTheSource:

    def test_saving_the_query_rereads_the_governed_file(self) -> None:
        runtime, source, _, control = _opened()

        source.save_query(runtime)

        assert control.queries == [(2000, "select 'default'")]
        assert control.reads == 2
        assert source.governed_context()["selected_transform_id"] == 20

    def test_saving_the_mapping_rereads_its_sources_governed_file(self) -> None:
        runtime, _, transform, control = _opened()

        transform.save(runtime)

        assert control.reads == 2

    def test_a_plain_source_reads_nothing(self) -> None:
        assert Source().reread_governed(SimpleNamespace()) is False


class TestSavingTheSavedSettings:

    def test_the_source_saves_and_is_told_the_saved_names(self) -> None:
        runtime, source, _, control = _opened()

        source.save_saved_settings(runtime, [
            {"transform_id": 20, "transform_name": "renamed", "is_default": True},
            {"transform_id": 21, "transform_name": "t21", "is_default": False},
        ])

        assert [given["action"] for _, given in control.maintained] == ["name_change"]
        assert control.reads == 1
        names = [one["transform_name"] for one in source.governed_context()["transform_choices"]]
        assert "renamed" in names

    def test_a_plain_source_is_refused(self) -> None:
        with pytest.raises(StateError, match="not opened on a governed file"):
            Source().save_saved_settings(SimpleNamespace(), [])
