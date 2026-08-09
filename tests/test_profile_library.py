"""Focused tests for the installation-scoped profile library."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.logs import (
    ProfileLibraryError,
    append_profile_record,
    lookup_profile_record,
    read_profile_records,
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


def _record(**overrides: object) -> dict:
    record = {
        "profile_schema_version": 1,
        "object_id": "file-1",
        "file_id": "file-1",
        "source_path": "/governed/source.csv",
        "source_hash": "abc123",
        "source_size": 42,
        "dataset_id": "source",
        "table_name": "source",
        "profiler": {
            "application": "file_operator",
            "structural_schema_version": 6,
            "distribution_profile_version": "csv_v1",
        },
        "sampling_strategy": "beginning_middle_end_v1",
        "requested_sample_rows": 500,
        "sampled_rows": 42,
        "eligible_population_rows": 42,
        "sampling_provenance": {
            "implementation": "rey_lib.files.csv.sample_indices",
            "strategy": "beginning_middle_end_v1",
            "inputs": ["eligible_population_rows", "requested_sample_rows"],
        },
        "structural_profile": {"schema_version": 6},
        "unredacted_profile": {"profile_version": "csv_v1"},
        "redacted_profile": {"profile_version": "csv_v1"},
    }
    record.update(overrides)
    return record


def test_profile_path_comes_only_from_installation_configuration(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    assert resolve_profile_library_path(ctx) == (
        tmp_path / "file_manifest" / "profiles.jsonl"
    )


def test_different_object_ids_coexist(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    first_id = append_profile_record(
        ctx, _record(object_id="first", file_id="first")
    )
    second_id = append_profile_record(
        ctx, _record(object_id="second", file_id="second")
    )

    records = read_profile_records(ctx)
    assert [record["profile_id"] for record in records] == [first_id, second_id]
    assert [record["file_id"] for record in records] == ["first", "second"]
    assert all(record["created_at"].endswith("+00:00") for record in records)
    assert not Path(str(resolve_profile_library_path(ctx)) + ".hstate.json").exists()


def test_same_object_id_is_atomically_replaced(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    first_id = append_profile_record(ctx, _record(source_hash="old"))
    second_id = append_profile_record(ctx, _record(source_hash="new"))

    records = read_profile_records(ctx)
    assert len(records) == 1
    assert records[0]["profile_id"] == second_id
    assert records[0]["profile_id"] != first_id
    assert records[0]["source_hash"] == "new"


def test_lookup_distinguishes_missing_stale_and_available(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    assert lookup_profile_record(ctx, "file-1", "hash-a")["status"] == "profile_missing"

    append_profile_record(ctx, _record(source_hash="hash-a"))

    stale = lookup_profile_record(ctx, "file-1", "hash-b")
    available = lookup_profile_record(ctx, "file-1", "hash-a")
    assert stale == {"status": "profile_stale", "object_id": "file-1", "record": None}
    assert available["status"] == "profile_available"
    assert available["record"]["source_hash"] == "hash-a"


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
