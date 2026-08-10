"""Focused tests for the installation-scoped governed profile library."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.logs import (
    ProfileLibraryError,
    append_profile_record,
    lookup_profile_record,
    read_profile_records,
    remove_profile_records,
    resolve_profile_library_path,
)


class _Paths:
    def __init__(self, manifest: Path, profiles: Path) -> None:
        self._paths = {"file_manifest": manifest, "file_profiles": profiles}

    def resolve(self, name: str) -> Path:
        return self._paths[name]


def _ctx(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        paths=_Paths(
            tmp_path / "file_manifest" / "manifest.jsonl",
            tmp_path / "file_manifest" / "profiles.jsonl",
        )
    )


def _record(**header_overrides: object) -> dict:
    header = {
        "profile_schema_version": 1,
        "object_id": "1",
        "source_hash": "abc123",
        "profiler": {
            "application": "file_operator",
        },
        "sampling_strategy": "random_without_replacement_v1",
        "requested_sample_rows": 500,
        "sampled_rows": 42,
        "eligible_population_rows": 42,
        "sampling_provenance": {
            "implementation": "rey_lib.files.csv.sample_indices",
            "strategy": "random_without_replacement_v1",
            "inputs": ["eligible_population_rows", "requested_sample_rows"],
        },
    }
    header.update(header_overrides)
    return {
        "header": header,
        "structure": {
            "header_definition": {
                "row_number": 1,
                "columns": ["Account Name"],
            },
            "distribution": {
                "row_count": 42,
            },
            "columns": [{"name": "Account Name", "type": "text"}],
            "samples": [
                {
                    "column": "Account Name",
                    "sample_values": [{"value": "ACME", "count": 8}],
                }
            ],
            "redacted_samples": [
                {
                    "column": "Account Name",
                    "sample_values": [{"value": "RANDOM", "count": 8}],
                }
            ],
        },
    }


def test_profile_path_comes_only_from_installation_configuration(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    assert resolve_profile_library_path(ctx) == (
        tmp_path / "file_manifest" / "profiles.jsonl"
    )


def test_different_source_rows_coexist(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    first_id = append_profile_record(ctx, _record(object_id="1"))
    second_id = append_profile_record(ctx, _record(object_id="2"))

    records = read_profile_records(ctx)
    assert [record["header"]["profile_id"] for record in records] == [
        first_id,
        second_id,
    ]
    assert [record["header"]["object_id"] for record in records] == ["1", "2"]
    # The profile log assigns each record its own identity, distinct from the
    # manifest object it describes and from the profile artifact's UUID.
    log_ids = [record["header"]["log_record_id"] for record in records]
    assert len(set(log_ids)) == 2
    assert all(log_id and log_id not in {"1", "2"} for log_id in log_ids)
    assert all(
        record["header"]["created_at"].endswith("+00:00") for record in records
    )
    assert not Path(str(resolve_profile_library_path(ctx)) + ".hstate.json").exists()


def test_same_object_id_is_atomically_replaced(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    first_id = append_profile_record(ctx, _record(source_hash="old"))
    second_id = append_profile_record(ctx, _record(source_hash="new"))

    records = read_profile_records(ctx)
    assert len(records) == 1
    assert records[0]["header"]["profile_id"] == second_id
    assert records[0]["header"]["profile_id"] != first_id
    assert records[0]["header"]["source_hash"] == "new"


def test_lookup_distinguishes_missing_stale_and_available(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    assert lookup_profile_record(ctx, "1", "hash-a")["status"] == "profile_missing"

    append_profile_record(ctx, _record(source_hash="hash-a"))

    stale = lookup_profile_record(ctx, "1", "hash-b")
    available = lookup_profile_record(ctx, "1", "hash-a")
    assert stale == {"status": "profile_stale", "object_id": "1", "record": None}
    assert available["status"] == "profile_available"
    assert available["record"]["header"]["source_hash"] == "hash-a"


def test_profile_append_does_not_modify_manifest_content(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    manifest = ctx.paths.resolve("file_manifest")
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(b"governed manifest bytes\n")

    append_profile_record(ctx, _record())

    assert manifest.read_bytes() == b"governed manifest bytes\n"


def test_writer_owns_profile_id_and_created_at(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(ProfileLibraryError, match="writer-owned"):
        append_profile_record(ctx, _record(profile_id="caller-value"))
    with pytest.raises(ProfileLibraryError, match="writer-owned"):
        append_profile_record(ctx, _record(created_at="caller-value"))


def test_invalid_sampling_counts_are_rejected_before_append(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(ProfileLibraryError, match="sampled_rows cannot exceed"):
        append_profile_record(
            ctx,
            _record(
                requested_sample_rows=10,
                sampled_rows=11,
                eligible_population_rows=20,
            ),
        )
    assert read_profile_records(ctx) == []


def test_the_retired_source_row_id_is_rejected(tmp_path: Path) -> None:
    """object_id and log_record_id are two identity spaces, never one value.

    source_row_id straddled them, which is why it had to be equal to object_id
    and why nothing could own a rollback. A header still carrying it is refused
    rather than silently accepted alongside the fields that replaced it.
    """
    ctx = _ctx(tmp_path)
    with pytest.raises(ProfileLibraryError, match="source_row_id"):
        append_profile_record(ctx, _record(object_id="2", source_row_id=1))
    assert read_profile_records(ctx) == []


def test_structure_rejects_value_fields_in_columns_and_shape_drift(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    value_in_column = _record()
    value_in_column["structure"]["columns"][0]["sample_values"] = ["ACME"]
    with pytest.raises(ProfileLibraryError, match="value-bearing"):
        append_profile_record(ctx, value_in_column)

    shape_drift = _record()
    shape_drift["structure"]["redacted_samples"][0].pop("sample_values")
    with pytest.raises(ProfileLibraryError, match="same shape"):
        append_profile_record(ctx, shape_drift)


def test_structure_rejects_invalid_counted_samples_and_count_drift(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    invalid_entry = _record()
    invalid_entry["structure"]["samples"][0]["sample_values"] = ["ACME"]
    with pytest.raises(ProfileLibraryError, match="value and count"):
        append_profile_record(ctx, invalid_entry)

    count_drift = _record()
    count_drift["structure"]["redacted_samples"][0]["sample_values"][0][
        "count"
    ] = 7
    with pytest.raises(ProfileLibraryError, match="count and order"):
        append_profile_record(ctx, count_drift)


def test_structure_rejects_retired_distinct_sample(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    retired = _record()
    retired["structure"]["samples"][0]["distinct_sample"] = [
        {"value": "ACME", "count": 8}
    ]
    retired["structure"]["redacted_samples"][0]["distinct_sample"] = [
        {"value": "RANDOM", "count": 8}
    ]
    with pytest.raises(ProfileLibraryError, match="unknown field.*distinct_sample"):
        append_profile_record(ctx, retired)


def test_retired_metadata_is_rejected(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    retired = _record()
    retired["structure"]["structural_profile"] = {"schema_version": 6}
    with pytest.raises(ProfileLibraryError, match="unknown field.*structural_profile"):
        append_profile_record(ctx, retired)

    retired_distribution = _record()
    retired_distribution["structure"]["distribution"]["llm_hints"] = {}
    with pytest.raises(ProfileLibraryError, match="llm_hints"):
        append_profile_record(ctx, retired_distribution)


def test_canonical_header_must_match_columns(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    mismatch = _record()
    mismatch["structure"]["header_definition"]["columns"] = ["Other"]
    with pytest.raises(ProfileLibraryError, match="match structure.columns"):
        append_profile_record(ctx, mismatch)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("type_hint", "text"),
        ("ordinal", 1),
        ("row_count", 42),
        ("integer_digit_counts", {"7": 42}),
        ("has_negative", False),
    ],
)
def test_columns_reject_derived_and_duplicate_fields(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    ctx = _ctx(tmp_path)
    duplicate = _record()
    duplicate["structure"]["columns"][0][field] = value
    with pytest.raises(ProfileLibraryError, match=f"non-canonical.*{field}"):
        append_profile_record(ctx, duplicate)


def _log_record_id(record: dict) -> str:
    return record["header"]["log_record_id"]


def test_removal_deletes_exactly_the_named_profile_record(tmp_path: Path) -> None:
    """The dangerous case: one record goes, the other is untouched.

    log_record_id is the only field consulted. Matching on object_id or
    profile_id would delete by the wrong identity space, and with two objects
    present that mistake is visible rather than silent.
    """
    ctx = _ctx(tmp_path)
    append_profile_record(ctx, _record(object_id="1"))
    append_profile_record(ctx, _record(object_id="2"))
    before = read_profile_records(ctx)
    doomed, survivor = before[0], before[1]

    removed = remove_profile_records(ctx, log_record_ids=[_log_record_id(doomed)])

    assert removed == 1
    assert read_profile_records(ctx) == [survivor]


def test_removal_ignores_an_id_that_matches_nothing(tmp_path: Path) -> None:
    """No match removes nothing and is not an error, as the manifest helper."""
    ctx = _ctx(tmp_path)
    append_profile_record(ctx, _record(object_id="1"))
    before = read_profile_records(ctx)

    assert remove_profile_records(ctx, log_record_ids=["not-a-real-id"]) == 0
    assert remove_profile_records(ctx, log_record_ids=[]) == 0
    assert read_profile_records(ctx) == before


def test_removing_a_superseded_object_resurrects_nothing(tmp_path: Path) -> None:
    """Removing the current profile leaves the object with no profile at all.

    append_profile_record physically drops the records it supersedes, so there
    is no earlier profile left to come back. This pins that consequence: an
    empty result is the retention model working, not a rollback bug, and
    resurrecting history would require retaining it in the first place.
    """
    ctx = _ctx(tmp_path)
    append_profile_record(ctx, _record(object_id="1", source_hash="first"))
    append_profile_record(ctx, _record(object_id="1", source_hash="second"))
    current = read_profile_records(ctx)
    assert len(current) == 1
    assert current[0]["header"]["source_hash"] == "second"

    removed = remove_profile_records(ctx, log_record_ids=[_log_record_id(current[0])])

    assert removed == 1
    assert read_profile_records(ctx) == []


def test_removal_leaves_supersession_behaviour_unchanged(tmp_path: Path) -> None:
    """Appending after a removal still replaces by object_id, as before."""
    ctx = _ctx(tmp_path)
    append_profile_record(ctx, _record(object_id="1"))
    append_profile_record(ctx, _record(object_id="2"))
    first = read_profile_records(ctx)[0]
    remove_profile_records(ctx, log_record_ids=[_log_record_id(first)])

    append_profile_record(ctx, _record(object_id="2", source_hash="replacement"))

    records = read_profile_records(ctx)
    assert [record["header"]["object_id"] for record in records] == ["2"]
    assert records[0]["header"]["source_hash"] == "replacement"
