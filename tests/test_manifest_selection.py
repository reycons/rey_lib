"""The common configured manifest-selection contract.

These prove the declared YAML shape selects the right records, that matching is
performed by the existing shared JSONL reader and search rather than by a second
matcher, and that selection stays narrow — no path resolution, no lineage, no
filesystem.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from rey_lib.logs import (
    ManifestSelectionError,
    select_manifest_records,
)


class _Paths:
    def __init__(self, manifest: Path) -> None:
        self._manifest = manifest

    def resolve(self, name: str) -> Path:
        from rey_lib.errors.error_utils import ConfigError

        if name != "file_manifest":
            raise ConfigError(f"Unknown path name: {name!r}")
        return self._manifest


def _ctx(manifest: Path) -> SimpleNamespace:
    return SimpleNamespace(paths=_Paths(manifest))


def _selection(manifest: Path, *record_sets: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_manifest": str(manifest),
        "record_sets": list(record_sets),
    }


def _write(manifest: Path, *records: dict[str, Any]) -> Path:
    manifest.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return manifest


def _classification(record_id: int, **overrides: Any) -> dict[str, Any]:
    record = {
        "record_id": record_id,
        "record_type": "source_file_classification",
        "application_name": "file_operator",
        "classification_type": "file_name_regex",
        "file_extension": "csv",
        "status": "classified",
        "values": {"record_type": "Hold", "feed": "bmo"},
    }
    record.update(overrides)
    return record


def _mutation(record_id: int, **overrides: Any) -> dict[str, Any]:
    record = {
        "record_id": record_id,
        "record_type": "source_file_mutation",
        "application_name": "file_operator",
        "action": "create",
        "status": "success",
        "mutation_kind": "excel_to_csv",
        "created_file_extension": "csv",
        "destination_path": "/data/out.csv",
    }
    record.update(overrides)
    return record


@pytest.fixture()
def manifest(tmp_path: Path) -> Path:
    return tmp_path / "file_manifest.jsonl"


# ---------------------------------------------------------------------------
# Record-set semantics
# ---------------------------------------------------------------------------


def test_one_record_set_selects_its_declared_record_type(manifest: Path) -> None:
    _write(manifest, _classification(1), _mutation(2))

    found = select_manifest_records(
        _ctx(manifest),
        _selection(
            manifest,
            {"record_type": "source_file_classification", "match": {}},
        ),
    )

    assert [item.record["record_id"] for item in found] == [1]
    assert found.records_read == 2
    assert found.records_selected == 1
    assert found.manifest_path == str(manifest)


def test_multiple_record_sets_use_or_semantics(manifest: Path) -> None:
    _write(manifest, _classification(1), _mutation(2), _classification(3))

    found = select_manifest_records(
        _ctx(manifest),
        _selection(
            manifest,
            {
                "record_type": "source_file_classification",
                "match": {"status": "classified"},
            },
            {
                "record_type": "source_file_mutation",
                "match": {"mutation_kind": "excel_to_csv", "status": "success"},
            },
        ),
    )

    assert [item.record["record_id"] for item in found] == [1, 2, 3]
    assert [item.record_set_index for item in found] == [0, 1, 0]


def test_fields_inside_one_match_use_and_semantics(manifest: Path) -> None:
    _write(
        manifest,
        _classification(1),
        _classification(2, file_extension="xls"),
    )

    found = select_manifest_records(
        _ctx(manifest),
        _selection(
            manifest,
            {
                "record_type": "source_file_classification",
                "match": {"file_extension": "csv", "status": "classified"},
            },
        ),
    )

    assert [item.record["record_id"] for item in found] == [1]


def test_empty_match_selects_every_record_of_that_type(manifest: Path) -> None:
    _write(manifest, _classification(1), _classification(2, status="rejected"))

    found = select_manifest_records(
        _ctx(manifest),
        _selection(
            manifest,
            {"record_type": "source_file_classification", "match": {}},
        ),
    )

    assert [item.record["record_id"] for item in found] == [1, 2]


def test_an_absent_match_key_selects_every_record_of_that_type(
    manifest: Path,
) -> None:
    _write(manifest, _classification(1), _mutation(2))

    found = select_manifest_records(
        _ctx(manifest),
        _selection(manifest, {"record_type": "source_file_mutation"}),
    )

    assert [item.record["record_id"] for item in found] == [2]


# ---------------------------------------------------------------------------
# Match forms
# ---------------------------------------------------------------------------


def test_scalar_equality_matches_exactly(manifest: Path) -> None:
    _write(manifest, _classification(1), _classification(2, status="rejected"))

    found = select_manifest_records(
        _ctx(manifest),
        _selection(
            manifest,
            {
                "record_type": "source_file_classification",
                "match": {"status": "rejected"},
            },
        ),
    )

    assert [item.record["record_id"] for item in found] == [2]


def test_list_membership_matches_any_listed_value(manifest: Path) -> None:
    _write(
        manifest,
        _classification(1, values={"record_type": "Hold"}),
        _classification(2, values={"record_type": "Tran"}),
        _classification(3, values={"record_type": "Other"}),
    )

    found = select_manifest_records(
        _ctx(manifest),
        _selection(
            manifest,
            {
                "record_type": "source_file_classification",
                "match": {"values.record_type": ["Hold", "Tran"]},
            },
        ),
    )

    assert [item.record["record_id"] for item in found] == [1, 2]


def test_dotted_fields_resolve_nested_values(manifest: Path) -> None:
    _write(
        manifest,
        _classification(1, values={"feed": "bmo"}),
        _classification(2, values={"feed": "moody"}),
    )

    found = select_manifest_records(
        _ctx(manifest),
        _selection(
            manifest,
            {
                "record_type": "source_file_classification",
                "match": {"values.feed": "moody"},
            },
        ),
    )

    assert [item.record["record_id"] for item in found] == [2]


@pytest.mark.parametrize(
    "match",
    [
        {"absent_field": "anything"},
        {"values.absent_field": "anything"},
        {"values.feed": "bmo", "absent_field": "anything"},
    ],
)
def test_a_missing_field_does_not_match(manifest: Path, match: dict) -> None:
    _write(manifest, _classification(1))

    found = select_manifest_records(
        _ctx(manifest),
        _selection(
            manifest,
            {"record_type": "source_file_classification", "match": match},
        ),
    )

    assert len(found) == 0


def test_booleans_stay_distinct_from_numbers(manifest: Path) -> None:
    _write(manifest, _classification(1, reprocessed=True))

    matched = select_manifest_records(
        _ctx(manifest),
        _selection(
            manifest,
            {
                "record_type": "source_file_classification",
                "match": {"reprocessed": True},
            },
        ),
    )
    mismatched = select_manifest_records(
        _ctx(manifest),
        _selection(
            manifest,
            {
                "record_type": "source_file_classification",
                "match": {"reprocessed": 1},
            },
        ),
    )

    assert len(matched) == 1
    assert len(mismatched) == 0


def test_values_containing_expression_syntax_are_compared_literally(
    manifest: Path,
) -> None:
    """A configured value is data, never part of the routed expression."""
    _write(
        manifest,
        _classification(1, values={"feed": "a`b'c\"d"}),
        _classification(2, values={"feed": "other"}),
    )

    found = select_manifest_records(
        _ctx(manifest),
        _selection(
            manifest,
            {
                "record_type": "source_file_classification",
                "match": {"values.feed": "a`b'c\"d"},
            },
        ),
    )

    assert [item.record["record_id"] for item in found] == [1]


# ---------------------------------------------------------------------------
# Ordering, identity, and narrowness
# ---------------------------------------------------------------------------


def test_manifest_order_is_preserved(manifest: Path) -> None:
    _write(
        manifest,
        _mutation(1),
        _classification(2),
        _mutation(3),
        _classification(4),
    )

    found = select_manifest_records(
        _ctx(manifest),
        _selection(
            manifest,
            {"record_type": "source_file_mutation", "match": {}},
            {"record_type": "source_file_classification", "match": {}},
        ),
    )

    assert [item.record["record_id"] for item in found] == [1, 2, 3, 4]
    assert [item.line_number for item in found] == [1, 2, 3, 4]


def test_records_sharing_content_are_not_deduplicated(manifest: Path) -> None:
    _write(manifest, _classification(1), _classification(1))

    found = select_manifest_records(
        _ctx(manifest),
        _selection(
            manifest,
            {"record_type": "source_file_classification", "match": {}},
        ),
    )

    assert len(found) == 2


def test_a_record_matching_two_sets_is_selected_once(manifest: Path) -> None:
    """Overlapping sets are one logical selection, attributed to the first."""
    _write(manifest, _classification(1))

    found = select_manifest_records(
        _ctx(manifest),
        _selection(
            manifest,
            {
                "record_type": "source_file_classification",
                "match": {"status": "classified"},
            },
            {
                "record_type": "source_file_classification",
                "match": {"file_extension": "csv"},
            },
        ),
    )

    assert len(found) == 1
    assert found.records[0].record_set_index == 0


def test_selection_resolves_no_paths_and_touches_no_filesystem(
    manifest: Path,
) -> None:
    """Selection returns records only; it never stats or resolves their paths."""
    _write(manifest, _mutation(1, destination_path="/nowhere/absent.csv"))

    with patch("os.scandir", side_effect=AssertionError("directory enumerated")):
        found = select_manifest_records(
            _ctx(manifest),
            _selection(manifest, {"record_type": "source_file_mutation"}),
        )

    assert found.records[0].record["destination_path"] == "/nowhere/absent.csv"
    assert not Path("/nowhere/absent.csv").exists()


def test_an_empty_selection_returns_a_valid_empty_result(manifest: Path) -> None:
    _write(manifest, _classification(1))

    found = select_manifest_records(
        _ctx(manifest),
        _selection(
            manifest,
            {"record_type": "source_file_mutation", "match": {}},
        ),
    )

    assert found.records == ()
    assert found.records_selected == 0
    assert found.records_read == 1


# ---------------------------------------------------------------------------
# Validation and failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("record_sets", "message"),
    [
        ([], "non-empty 'record_sets'"),
        (["not-a-mapping"], "must be a mapping"),
        ([{"match": {"status": "classified"}}], "requires a non-empty 'record_type'"),
        ([{"record_type": "", "match": {}}], "requires a non-empty 'record_type'"),
        ([{"record_type": "x", "match": "not-a-mapping"}], "'match' must be a mapping"),
        ([{"record_type": "x", "match": {"field": []}}], "empty value list"),
    ],
)
def test_malformed_record_sets_fail_validation(
    manifest: Path,
    record_sets: list,
    message: str,
) -> None:
    _write(manifest, _classification(1))
    selection = {"file_manifest": str(manifest), "record_sets": record_sets}

    with pytest.raises(ManifestSelectionError, match=message):
        select_manifest_records(_ctx(manifest), selection)


def test_a_missing_record_sets_key_fails_validation(manifest: Path) -> None:
    _write(manifest, _classification(1))

    with pytest.raises(ManifestSelectionError, match="non-empty 'record_sets'"):
        select_manifest_records(_ctx(manifest), {"file_manifest": str(manifest)})


def test_configuration_is_validated_before_the_manifest_is_read(
    tmp_path: Path,
) -> None:
    """A malformed selection fails even when no manifest exists to read."""
    absent = tmp_path / "absent.jsonl"

    with pytest.raises(ManifestSelectionError, match="non-empty 'record_sets'"):
        select_manifest_records(
            _ctx(absent),
            {"file_manifest": str(absent), "record_sets": []},
        )


def test_a_foreign_manifest_path_is_rejected(
    manifest: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    _write(manifest, _classification(1))
    foreign = tmp_path_factory.mktemp("other") / "file_manifest.jsonl"

    selection = {
        "file_manifest": str(foreign),
        "record_sets": [{"record_type": "source_file_classification"}],
    }

    with pytest.raises(ManifestSelectionError, match="governed manifest"):
        select_manifest_records(_ctx(manifest), selection)


def test_a_missing_file_manifest_value_is_rejected(manifest: Path) -> None:
    _write(manifest, _classification(1))

    with pytest.raises(ManifestSelectionError, match="requires a 'file_manifest'"):
        select_manifest_records(
            _ctx(manifest),
            {"record_sets": [{"record_type": "source_file_classification"}]},
        )


def test_malformed_jsonl_fails_through_the_strict_reader(manifest: Path) -> None:
    manifest.write_text("{not json}\n", encoding="utf-8")

    with pytest.raises(ManifestSelectionError, match="cannot read"):
        select_manifest_records(
            _ctx(manifest),
            _selection(manifest, {"record_type": "source_file_classification"}),
        )
