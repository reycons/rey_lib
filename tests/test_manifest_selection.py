"""The common configured manifest-selection contract.

These prove the declared YAML shape selects the right records, that matching is
evaluated over the governed records themselves, and that selection stays
narrow -- no path resolution, no lineage, no filesystem.

Records come from the control database through the real writers, so a match is
declared against the shape a consumer actually sees. Selections are keyed by
``record_id``: the manifest is a table and there is no line to key one on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from rey_lib.files.manifest import FileManifest
from rey_lib.logs import (
    ManifestSelectionError,
    select_manifest_records,
)

from tests.support.control_double import control_backed_ctx


class _Paths:
    def __init__(self, manifest: Path) -> None:
        self._manifest = manifest

    def resolve(self, name: str) -> Path:
        from rey_lib.errors.error_utils import ConfigError

        if name != "file_manifest":
            raise ConfigError(f"Unknown path name: {name!r}")
        return self._manifest


class _Store:
    """A governed manifest seeded through the real FileManifest."""

    def __init__(self, manifest: Path) -> None:
        self.ctx = control_backed_ctx(paths=_Paths(manifest))
        self.manifest = FileManifest(self.ctx.shared_control)
        self.path = manifest

    def file(self, name: str, *, extension: str = "csv",
             source: str = "bmo", size: int = 10,
             classification: dict[str, Any] | None = None) -> int:
        """Record one governed file, returning the identity it was given."""
        file_id = self.manifest.inventory(
            path=f"/in/{name}.{extension}", file_name=f"{name}.{extension}",
            base_name=name, file_extension=extension,
            checksum_sha256=f"sha-{name}", size_bytes=size, source_name=source,
            producer={"application": "file_operator"},
        )
        if classification is not None:
            self.manifest.update(file_id, classification=classification)
        return file_id

    def mutation(self, file_id: int, *, action: str = "create",
                 status: str = "success", path: str = "/data/out.csv",
                 **payload: Any) -> int:
        """Append one lifecycle event beneath a governed file."""
        return self.manifest.append_mutation(
            file_id, record_type="source_file_mutation", action=action,
            status=status, path=path,
            producer={"application": "file_operator"}, **payload,
        )


@pytest.fixture()
def store(tmp_path: Path) -> _Store:
    return _Store(tmp_path / "file_manifest.jsonl")


def _selection(manifest: Path, *record_sets: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_manifest": str(manifest),
        "record_sets": list(record_sets),
    }


# ---------------------------------------------------------------------------
# Record-set semantics
# ---------------------------------------------------------------------------

_CLASSIFIED = {"type": "file_name_regex", "source_field": "file.file_name",
               "values": {"record_type": "Hold", "feed": "bmo"}}


def test_one_record_set_selects_its_declared_record_type(store) -> None:
    file_id = store.file("a", classification=_CLASSIFIED)
    store.mutation(file_id)

    found = select_manifest_records(
        store.ctx,
        _selection(store.path,
                   {"record_type": "source_file_inventory", "match": {}}),
    )

    assert [item.record["record_id"] for item in found] == [file_id]
    assert found.records_selected == 1
    assert found.manifest_path == str(store.path)


def test_multiple_record_sets_use_or_semantics(store) -> None:
    first = store.file("a", classification=_CLASSIFIED)
    second = store.file("b", classification=_CLASSIFIED)
    mutation = store.mutation(first, action="create")

    found = select_manifest_records(
        store.ctx,
        _selection(
            store.path,
            {"record_type": "source_file_inventory", "match": {}},
            {"record_type": "source_file_mutation",
             "match": {"action": "create", "status": "success"}},
        ),
    )

    assert sorted(item.record["record_id"] for item in found) == sorted(
        [first, second, mutation])
    assert {item.record_set_index for item in found} == {0, 1}


def test_fields_inside_one_match_use_and_semantics(store) -> None:
    """Every declared field must hold, not any of them."""
    wanted = store.mutation(store.file("a"), action="create", status="success")
    store.mutation(store.file("b"), action="create", status="failed")

    found = select_manifest_records(
        store.ctx,
        _selection(store.path,
                   {"record_type": "source_file_mutation",
                    "match": {"action": "create", "status": "success"}}),
    )

    assert [item.record["record_id"] for item in found] == [wanted]


def test_empty_match_selects_every_record_of_that_type(store) -> None:
    first, second = store.file("a"), store.file("b")

    found = select_manifest_records(
        store.ctx,
        _selection(store.path,
                   {"record_type": "source_file_inventory", "match": {}}),
    )

    assert [item.record["record_id"] for item in found] == [first, second]


def test_an_absent_match_key_selects_every_record_of_that_type(store) -> None:
    """A match that is not declared is not a filter."""
    first, second = store.file("a"), store.file("b")

    found = select_manifest_records(
        store.ctx,
        _selection(store.path, {"record_type": "source_file_inventory"}),
    )

    assert [item.record["record_id"] for item in found] == [first, second]


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_scalar_equality_matches_exactly(store) -> None:
    exact = store.mutation(store.file("a"), status="success")
    store.mutation(store.file("b"), status="succeeded")

    found = select_manifest_records(
        store.ctx,
        _selection(store.path, {"record_type": "source_file_mutation",
                                "match": {"status": "success"}}),
    )

    assert [item.record["record_id"] for item in found] == [exact]


def test_list_membership_matches_any_listed_value(store) -> None:
    created = store.mutation(store.file("a"), action="create")
    moved = store.mutation(store.file("b"), action="move")
    store.mutation(store.file("c"), action="delete")

    found = select_manifest_records(
        store.ctx,
        _selection(store.path,
                   {"record_type": "source_file_mutation",
                    "match": {"action": ["create", "move"]}}),
    )

    assert sorted(item.record["record_id"] for item in found) == sorted(
        [created, moved])


def test_dotted_fields_resolve_nested_values(store) -> None:
    """A nested field is addressed by path, not flattened into the root."""
    wanted = store.file("a", extension="csv")
    store.file("b", extension="xls")

    found = select_manifest_records(
        store.ctx,
        _selection(store.path,
                   {"record_type": "source_file_inventory",
                    "match": {"file.file_extension": "csv"}}),
    )

    assert [item.record["record_id"] for item in found] == [wanted]


@pytest.mark.parametrize("match", [
    {"nonexistent": "value"},
    {"file.nonexistent": "value"},
    {"nonexistent.nested": "value"},
])
def test_a_missing_field_does_not_match(store, match: dict) -> None:
    store.file("a")

    found = select_manifest_records(
        store.ctx,
        _selection(store.path,
                   {"record_type": "source_file_inventory", "match": match}),
    )

    assert list(found) == []


def test_booleans_stay_distinct_from_numbers(store) -> None:
    """1 is not True. A comparison that conflates them selects the wrong row."""
    numeric = store.file("a", size=1)

    found = select_manifest_records(
        store.ctx,
        _selection(store.path,
                   {"record_type": "source_file_inventory",
                    "match": {"file.size_bytes": True}}),
    )
    assert list(found) == []

    found = select_manifest_records(
        store.ctx,
        _selection(store.path,
                   {"record_type": "source_file_inventory",
                    "match": {"file.size_bytes": 1}}),
    )
    assert [item.record["record_id"] for item in found] == [numeric]


def test_values_containing_expression_syntax_are_compared_literally(
    store,
) -> None:
    """A declared value is a value, never an expression to evaluate."""
    literal = store.mutation(store.file("a"), path="/data/`weird`.csv")
    store.mutation(store.file("b"), path="/data/plain.csv")

    found = select_manifest_records(
        store.ctx,
        _selection(store.path,
                   {"record_type": "source_file_mutation",
                    "match": {"file.path": "/data/`weird`.csv"}}),
    )

    assert [item.record["record_id"] for item in found] == [literal]


# ---------------------------------------------------------------------------
# Ordering and identity
# ---------------------------------------------------------------------------


def test_generated_identity_order_is_preserved(store) -> None:
    """Selections come back in identity order, which is the order of events."""
    first, second, third = store.file("a"), store.file("b"), store.file("c")

    found = select_manifest_records(
        store.ctx,
        _selection(store.path,
                   {"record_type": "source_file_inventory", "match": {}}),
    )

    assert [item.record["record_id"] for item in found] == [
        first, second, third,
    ]
    assert [item.record_id for item in found] == [first, second, third]


def test_records_sharing_content_are_not_deduplicated(store) -> None:
    """Two identical events are two events."""
    file_id = store.file("a")
    first = store.mutation(file_id, action="create", path="/data/out.csv")
    second = store.mutation(file_id, action="create", path="/data/out.csv")

    found = select_manifest_records(
        store.ctx,
        _selection(store.path,
                   {"record_type": "source_file_mutation",
                    "match": {"action": "create"}}),
    )

    assert [item.record["record_id"] for item in found] == [first, second]


def test_a_record_matching_two_sets_is_selected_once(store) -> None:
    """One record, attributed to the first set that claimed it."""
    mutation = store.mutation(store.file("a"), action="create",
                              status="success")

    found = select_manifest_records(
        store.ctx,
        _selection(
            store.path,
            {"record_type": "source_file_mutation",
             "match": {"action": "create"}},
            {"record_type": "source_file_mutation",
             "match": {"status": "success"}},
        ),
    )

    assert [item.record["record_id"] for item in found] == [mutation]
    assert [item.record_set_index for item in found] == [0]


def test_selection_resolves_no_paths_and_touches_no_filesystem(store) -> None:
    """Selection reads records. It resolves nothing and inspects nothing."""
    store.file("a")

    with patch("pathlib.Path.exists") as exists:
        found = select_manifest_records(
            store.ctx,
            _selection(store.path,
                       {"record_type": "source_file_inventory", "match": {}}),
        )

    assert len(list(found)) == 1
    exists.assert_not_called()


def test_an_empty_selection_returns_a_valid_empty_result(store) -> None:
    store.file("a")

    found = select_manifest_records(
        store.ctx,
        _selection(store.path,
                   {"record_type": "source_file_mutation",
                    "match": {"action": "nothing_matches_this"}}),
    )

    assert list(found) == []
    assert found.records_selected == 0
    assert found.manifest_path == str(store.path)


# ---------------------------------------------------------------------------
# Configuration validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("record_sets, expected", [
    ([{"match": {}}], "record_type"),
    ([{"record_type": "", "match": {}}], "record_type"),
    ([{"record_type": "source_file_inventory", "match": []}], "match"),
    ("not-a-list", "record_sets"),
])
def test_malformed_record_sets_fail_validation(store, record_sets,
                                               expected: str) -> None:
    with pytest.raises(ManifestSelectionError, match=expected):
        select_manifest_records(
            store.ctx,
            {"file_manifest": str(store.path), "record_sets": record_sets},
        )


def test_a_missing_record_sets_key_fails_validation(store) -> None:
    with pytest.raises(ManifestSelectionError, match="record_sets"):
        select_manifest_records(store.ctx,
                                {"file_manifest": str(store.path)})


def test_configuration_is_validated_before_the_manifest_is_read(store) -> None:
    """A malformed declaration is refused without reading anything."""
    with pytest.raises(ManifestSelectionError, match="record_type"):
        select_manifest_records(
            store.ctx,
            _selection(store.path, {"match": {}}),
        )


def test_a_foreign_manifest_path_is_rejected(store, tmp_path: Path) -> None:
    """A selection names the installation's governed manifest, or nothing."""
    with pytest.raises(ManifestSelectionError, match="file_manifest"):
        select_manifest_records(
            store.ctx,
            _selection(tmp_path / "somewhere_else.jsonl",
                       {"record_type": "source_file_inventory", "match": {}}),
        )


def test_a_missing_file_manifest_value_is_rejected(store) -> None:
    with pytest.raises(ManifestSelectionError, match="file_manifest"):
        select_manifest_records(
            store.ctx,
            {"record_sets": [{"record_type": "source_file_inventory",
                              "match": {}}]},
        )
