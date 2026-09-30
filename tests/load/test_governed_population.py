"""A governed file populates the canonical Source and Transform, and nothing else.

    ManifestSource.populate(source, transform)

Asserted: the columns come in their STORED order, stated rather than inherited
from the join; the Source is told the file's governed facts as context, never
configuration, and loses them when it stops being that file; the Transform is
configured only from a switched-on definition under an active scope; and each
column's stored id and ordinal are held BESIDE the declaration -- aligned to
its entries through every edit -- and never inside it.

THE FIXTURES ARE THE CONTRACT'S SHAPE: one row per profile field x transform
column, parent values repeating.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

import pytest

from rey_lib.load import Source, Transform
from rey_lib.load.manifest_source import ManifestSource


class _Reader:
    """The source-context contract, answered from rows."""

    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self.rows = list(rows)

    def file_source_context(
        self,
        file_manifest_id: Optional[int] = None,
        file_mutation_id: Optional[int] = None,
        required: bool = True,
    ) -> Sequence[Mapping[str, Any]]:
        return self.rows


def _row(**overrides: Any) -> dict[str, Any]:
    """One context row: a typed, profiled, transformed file."""
    row: dict[str, Any] = {
        "file_manifest_id": 10, "file_mutation_id": 25, "file_type_id": 4,
        "data_profile_id": 7, "data_profile_field_id": 101, "transform_id": 20,
        "transform_column_id": 301, "installation_id": 1,
        "path": "/data/incoming/asset.txt", "file_name": "asset.txt",
        "layout": "DELIMITED_HEADER", "profile_header_definition": "asset_id",
        "field_name": "asset_id", "field_ordinal": 1, "field_detected_type": "integer",
        "transform_is_enabled": True, "transform_row_filter": None,
        "column_name": "asset_id", "source_column": "asset_id", "column_ordinal": 1,
        "column_datatype": "integer", "transform_type": "passthrough",
        "transform_config": None, "column_is_exported": True,
    }
    row.update(overrides)
    return row


def _column(column_id: int, name: str, ordinal: Optional[int], **more: Any) -> dict[str, Any]:
    return {"transform_column_id": column_id, "column_name": name,
            "source_column": name, "column_ordinal": ordinal, **more}


def _joined(columns: list[dict[str, Any]], fields: int = 1) -> list[dict[str, Any]]:
    """Every field crossed with every column, as the contract returns them."""
    return [
        _row(data_profile_field_id=100 + field, field_name=f"f{field}",
             field_ordinal=field, **column)
        for field in range(1, fields + 1) for column in columns
    ]


def _manifest(rows: list[dict[str, Any]], **create: Any) -> ManifestSource:
    return ManifestSource.create(
        _Reader(rows), file_manifest_id=10, adopt_persisted_type=True, **create,
    )


def _populated(rows: list[dict[str, Any]]) -> tuple[Source, Transform]:
    source = Source({"file-manifest-id": "10"}, selected="manifest")
    transform = Transform()
    _manifest(rows).populate(source, transform)
    return source, transform


class TestTheColumnsComeInTheirStoredOrder:

    def test_rows_arriving_out_of_order_are_put_in_ordinal_order(self) -> None:
        _, transform = _populated(_joined([
            _column(303, "c", 70), _column(301, "a", 10), _column(302, "b", 20),
        ], fields=2))

        assert [one["name"] for one in transform.columns()] == ["a", "b", "c"]
        assert transform.column_ids() == [301, 302, 303]
        assert transform.column_ordinals() == [10, 20, 70]

    def test_equal_and_absent_ordinals_keep_the_readers_order_absent_last(self) -> None:
        _, transform = _populated(_joined([
            _column(304, "none_first", None), _column(302, "tie_first", 5),
            _column(303, "tie_second", 5), _column(305, "none_second", None),
            _column(301, "lowest", 1),
        ]))

        assert [one["name"] for one in transform.columns()] == [
            "lowest", "tie_first", "tie_second", "none_first", "none_second",
        ]
        assert transform.column_ordinals() == [1, 5, 5, None, None]


class TestTheSourceIsToldTheGovernedFacts:

    def test_the_four_identities_and_the_file_name(self) -> None:
        source, _ = _populated(_joined([_column(301, "a", 1)]))

        assert source.governed_context() == {
            "file_manifest_id": 10, "file_mutation_id": 25, "installation_id": 1,
            "data_profile_id": 7, "file_name": "asset.txt",
        }

    def test_a_file_with_no_profile_still_has_its_context(self) -> None:
        source, _ = _populated([_row(
            file_type_id=None, data_profile_id=None, data_profile_field_id=None,
            transform_id=None, transform_column_id=None,
        )])

        assert source.governed_context()["data_profile_id"] is None
        assert source.governed_context()["file_manifest_id"] == 10

    def test_the_context_is_not_configuration(self) -> None:
        source, _ = _populated(_joined([_column(301, "a", 1)]))

        # The resolved IDENTITIES are configuration (row 435: the source object
        # is populated from the resolved values); the file's facts are not.
        assert source.declaration() == {
            "selected": "manifest",
            "values": {"file-manifest-id": "10", "file-mutation-id": "25",
                       "file-type-id": "4"},
        }
        assert "file_name" not in source.configuration()
        assert source.validate() == []
        assert Source.from_declaration(source.declaration()).governed_context() is None


class TestTheSourceIsPopulatedFromTheResolvedContext:
    """Row 435: ManifestSource resolves; the source object is populated from it."""

    def test_the_resolved_identities_fill_its_manifest_fields(self) -> None:
        source = Source({"file-manifest-id": "10"}, selected="manifest")
        _manifest(_joined([_column(301, "a", 1)])).populate(source, Transform())

        assert source.selected_kind() == "manifest"
        assert source.configuration() == {
            "file-manifest-id": "10", "file-mutation-id": "25", "file-type-id": "4",
        }

    def test_opened_by_its_mutation_it_gains_its_manifest(self) -> None:
        source = Source({"file-mutation-id": "25"})
        ManifestSource.create(
            _Reader(_joined([_column(301, "a", 1)])), file_mutation_id=25,
            adopt_persisted_type=True,
        ).populate(source, Transform())

        assert source.configuration()["file-manifest-id"] == "10"
        assert source.selected_kind() == "manifest"

    def test_its_context_survives_its_own_population(self) -> None:
        source, _ = _populated(_joined([_column(301, "a", 1)]))

        assert source.governed_context() is not None

    def test_no_governing_type_leaves_the_type_field_as_it_was(self) -> None:
        source, _ = _populated([_row(file_type_id=None)])

        assert source.value("file-type-id") is None
        assert source.value("file-mutation-id") == "25"


class TestTheContextBelongsToOneIdentity:

    @pytest.fixture()
    def source(self) -> Source:
        held, _ = _populated(_joined([_column(301, "a", 1)]))
        return held

    @pytest.mark.parametrize("field", ["file-manifest-id", "file-mutation-id", "file-type-id"])
    def test_a_different_identity_clears_it(self, source: Source, field: str) -> None:
        source.update(field, "99")

        assert source.governed_context() is None

    def test_the_same_identity_said_again_keeps_it(self, source: Source) -> None:
        source.update("file-manifest-id", 10)
        source.update("file-manifest-id", " 10 ")

        assert source.governed_context() is not None

    def test_another_kinds_field_keeps_it(self, source: Source) -> None:
        source.update("file", "/elsewhere.csv")

        assert source.governed_context() is not None

    def test_selecting_another_kind_and_back_keeps_it(self, source: Source) -> None:
        # The selected kind is the configuration in force; it destroys nothing.
        before = source.governed_context()

        source.select("file")
        assert source.governed_context() == before
        source.select("manifest")

        assert source.governed_context() == before
        assert source.value("file-mutation-id") == "25"


class TestTheTransformIsConfiguredFromTheStoredDefinition:

    def test_it_becomes_the_manifest_kind_with_the_stored_mapping(self) -> None:
        _, transform = _populated(_joined([
            _column(301, "a", 1, column_datatype="integer",
                    transform_type="upper", transform_config={"type": "ignored", "x": 1}),
            _column(302, "b", 2, column_datatype=None, column_is_exported=False),
        ]))

        assert transform.selected_kind() == "manifest"
        assert transform.validate() == []
        assert transform.columns() == [
            {"name": "a", "source": "a", "datatype": "integer",
             "transform": {"x": 1, "type": "upper"}},
            {"name": "b", "source": "b", "transform": {"type": "passthrough"},
             "export": False},
        ]
        assert transform.value("persistence") == {
            "transform_id": 20, "file_type_id": 4,
            "column_ids": [301, 302], "column_ordinals": [1, 2],
        }

    def test_the_stored_facts_never_enter_the_declaration(self) -> None:
        _, transform = _populated(_joined([_column(301, "a", 1), _column(302, "b", 2)]))

        for entry in transform.value("declaration")["columns"]:
            assert not {"transform_column_id", "column_ordinal", "id", "ordinal"} & set(entry)

    def test_repeated_parent_rows_are_one_definition(self) -> None:
        _, transform = _populated(_joined(
            [_column(301 + at, f"c{at}", at + 1) for at in range(4)], fields=3,
        ))

        assert len(transform.columns()) == 4
        assert transform.column_ids() == [301, 302, 303, 304]

    def test_a_switched_off_definition_leaves_the_transform_alone(self) -> None:
        source, transform = _populated([_row(transform_is_enabled=False)])

        assert transform.declaration() == Transform().declaration()
        assert source.governed_context() is not None

    def test_a_file_with_no_type_leaves_the_transform_alone(self) -> None:
        _, transform = _populated([_row(file_type_id=None)])

        assert transform.declaration() == Transform().declaration()

    def test_a_scope_nobody_asked_for_is_not_adopted(self) -> None:
        source, transform = Source(), Transform()
        ManifestSource.create(
            _Reader(_joined([_column(301, "a", 1)])), file_manifest_id=10,
        ).populate(source, transform)

        assert transform.declaration() == Transform().declaration()
        assert source.governed_context() is not None


class TestTheStoredFactsStayAligned:

    @pytest.fixture()
    def transform(self) -> Transform:
        _, held = _populated(_joined([
            _column(301, "a", 10), _column(302, "b", 20), _column(303, "c", 30),
        ]))
        return held

    def test_a_moved_entry_takes_its_facts_with_it(self, transform: Transform) -> None:
        transform.move_column(2, 0)

        assert [one["name"] for one in transform.columns()] == ["c", "a", "b"]
        assert transform.column_ids() == [303, 301, 302]
        assert transform.column_ordinals() == [30, 10, 20]

    def test_an_added_entry_has_none(self, transform: Transform) -> None:
        transform.add_column(0)

        assert transform.column_ids() == [301, None, 302, 303]
        assert transform.column_ordinals() == [10, None, 20, 30]

    def test_a_spliced_source_column_has_none(self, transform: Transform) -> None:
        transform.observe_source_columns(["a", "b", "c", "d"])

        assert [one["name"] for one in transform.columns()] == ["a", "b", "c", "d"]
        assert transform.column_ids() == [301, 302, 303, None]
        assert transform.column_ordinals() == [10, 20, 30, None]

    def test_ordinals_are_not_manufactured_for_a_persistence_without_them(self) -> None:
        transform = Transform({
            "declaration": {"columns": [{"source": "a", "name": "a"}]},
            "persistence": {"transform_id": 1, "file_type_id": 2, "column_ids": [9]},
        }, selected="manifest")
        transform.add_column()

        assert transform.column_ordinals() == []
        assert "column_ordinals" not in transform.value("persistence")
        assert transform.column_ids() == [9, None]

    def test_a_kind_that_stores_nothing_answers_nothing(self) -> None:
        transform = Transform({"transform": '{"columns": [{"source": "a", "name": "a"}]}'})

        assert transform.column_ids() == [] and transform.column_ordinals() == []
