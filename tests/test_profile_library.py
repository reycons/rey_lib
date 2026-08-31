"""A profile is a mutation, and this is how one is found and presented.

The rows are seeded through ``log_file_manifest_record`` -- the writer profiling
itself uses -- so what these read is what the real writer produces.
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.logs import log_file_manifest_record
from rey_lib.logs.profile_library import (
    PROFILE_ACCESS_REDACTED,
    PROFILE_ACCESS_UNREDACTED,
    ProfileLibraryError,
    lookup_profile_record,
    read_profile_records,
    resolve_profile_presentation,
)

from tests.support.control_double import control_backed_ctx


def _ctx() -> Any:
    """A context holding one governed file for profiles to attach to."""
    ctx = control_backed_ctx()
    ctx.shared_control.inventory_file(
        path="/source/report.csv", file_name="report.csv", base_name="report",
        file_extension="csv", checksum_sha256="abc", size_bytes=1,
        evidence={"run_log_id": 1},
    )
    return ctx


def _profile(
    ctx: Any,
    *,
    source_record_id: int,
    source_hash: str = "hash-a",
    clear_samples: Any = None,
    redacted_samples: Any = None,
) -> int:
    """Append one profiling mutation the way the profiler appends it."""
    shared = {"profile_schema_version": 1, "source_hash": source_hash,
              "header_definition": {"columns": ["a"]}, "distribution": {},
              "columns": []}
    return log_file_manifest_record(ctx, {
        "record_type": "source_file_profile",
        "action": "record_only",
        "status": "success",
        "file_id": 1,
        "evidence": {"run_log_id": 1},
        "file": {"path": "/source/report.csv"},
        "lineage": {"source_record_id": source_record_id},
        "clear_profile": {
            **shared,
            "samples": [{"value": "Alice"}] if clear_samples is None else clear_samples,
        },
        "redacted_profile": {
            **shared,
            "samples": [{"value": "X"}] if redacted_samples is None else redacted_samples,
        },
    })


def _mutation(ctx: Any, record_type: str) -> int:
    """One non-profiling mutation, so narrowing has something to exclude."""
    return log_file_manifest_record(ctx, {
        "record_type": record_type,
        "action": "move",
        "status": "success",
        "file_id": 1,
        "evidence": {"run_log_id": 1},
        "file": {"path": "/source/report.csv"},
    })


def test_only_profiling_mutations_are_profiles() -> None:
    """Reading profiles narrows the file's mutations to the profiling ones."""
    ctx = _ctx()
    _mutation(ctx, "source_file_mutation")
    _profile(ctx, source_record_id=2)

    records = read_profile_records(ctx)

    assert [record["record_type"] for record in records] == ["source_file_profile"]


def test_lookup_distinguishes_missing_stale_and_available() -> None:
    """The three states, keyed on the mutation the profile consumed."""
    ctx = _ctx()
    assert lookup_profile_record(ctx, 2, "hash-a")["status"] == "profile_missing"

    _profile(ctx, source_record_id=2, source_hash="hash-a")

    assert lookup_profile_record(ctx, 2, "hash-b")["status"] == "profile_stale"
    available = lookup_profile_record(ctx, 2, "hash-a")
    assert available["status"] == "profile_available"
    assert available["record"]["source_record_id"] == 2


def test_a_profile_of_another_mutation_is_not_this_ones() -> None:
    """The key is the consumed mutation, not the file they share."""
    ctx = _ctx()
    _profile(ctx, source_record_id=2)

    assert lookup_profile_record(ctx, 3, "hash-a")["status"] == "profile_missing"


def test_reprofiling_appends_and_the_last_is_current() -> None:
    """Re-profiling never replaces; the newest reading is the current one."""
    ctx = _ctx()
    _profile(ctx, source_record_id=2, source_hash="hash-a")
    _profile(ctx, source_record_id=2, source_hash="hash-b")

    assert len(read_profile_records(ctx)) == 2
    assert lookup_profile_record(ctx, 2, "hash-b")["status"] == "profile_available"
    assert lookup_profile_record(ctx, 2, "hash-a")["status"] == "profile_stale"


@pytest.mark.parametrize("supplied", [0, -1, "2", None, True])
def test_a_consumed_record_is_a_positive_mutation_id(supplied: Any) -> None:
    """No manifest id, no string, no truthy stand-in resolves to a lookup."""
    with pytest.raises(ProfileLibraryError):
        lookup_profile_record(_ctx(), supplied, "hash-a")


def test_each_access_returns_its_own_representation() -> None:
    """Clear and redacted are separate columns, so one is read not derived."""
    ctx = _ctx()
    _profile(ctx, source_record_id=2, clear_samples=[{"value": "Alice"}],
             redacted_samples=[{"value": "X"}])
    record = lookup_profile_record(ctx, 2, "hash-a")["record"]

    clear = resolve_profile_presentation(record, PROFILE_ACCESS_UNREDACTED)
    redacted = resolve_profile_presentation(record, PROFILE_ACCESS_REDACTED)

    assert clear["samples"] == [{"value": "Alice"}]
    assert redacted["samples"] == [{"value": "X"}]


def test_the_redacted_representation_has_no_path_back_to_the_clear_values() -> None:
    """What a caller is handed holds one reading and never the other."""
    ctx = _ctx()
    _profile(ctx, source_record_id=2, clear_samples=[{"value": "Alice"}],
             redacted_samples=[{"value": "X"}])
    record = lookup_profile_record(ctx, 2, "hash-a")["record"]

    redacted = resolve_profile_presentation(record, PROFILE_ACCESS_REDACTED)

    assert "clear_profile" not in redacted
    assert "Alice" not in repr(redacted)


def test_an_unknown_access_names_no_representation() -> None:
    """Only the two readings exist, so a third is refused rather than guessed."""
    ctx = _ctx()
    _profile(ctx, source_record_id=2)
    record = lookup_profile_record(ctx, 2, "hash-a")["record"]

    with pytest.raises(ProfileLibraryError):
        resolve_profile_presentation(record, "partial")


def test_a_representation_the_mutation_lacks_is_refused() -> None:
    """A half-recorded profile fails rather than answering with the other half."""
    ctx = _ctx()
    ctx.shared_control.append_file_mutation(
        1, record_type="source_file_profile", action="record_only",
        source_record_id=2, clear_profile={"source_hash": "hash-a", "samples": []},
    )
    record = lookup_profile_record(ctx, 2, "hash-a")["record"]

    with pytest.raises(ProfileLibraryError):
        resolve_profile_presentation(record, PROFILE_ACCESS_REDACTED)
