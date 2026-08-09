"""Installation-scoped append-only file profile library."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from rey_lib.logs.file_manifest import FileManifestError, file_manifest_session

__all__ = [
    "ProfileLibraryError",
    "append_profile_record",
    "lookup_profile_record",
    "read_profile_records",
    "resolve_profile_library_path",
]

_PATH_NAME = "file_profiles"
_PROFILE_SCHEMA_VERSION = 1
_WRITER_FIELDS = frozenset({"profile_id", "created_at"})
_REQUIRED_FIELDS = frozenset({
    "profile_schema_version",
    "object_id",
    "file_id",
    "source_path",
    "source_hash",
    "source_size",
    "dataset_id",
    "profiler",
    "sampling_strategy",
    "requested_sample_rows",
    "sampled_rows",
    "eligible_population_rows",
    "sampling_provenance",
    "structural_profile",
    "unredacted_profile",
    "redacted_profile",
})
_OPTIONAL_FIELDS = frozenset({"source_modified_time", "table_name"})
_CANONICAL_FIELDS = (
    "profile_id",
    "profile_schema_version",
    "object_id",
    "file_id",
    "source_path",
    "source_hash",
    "source_size",
    "source_modified_time",
    "dataset_id",
    "table_name",
    "created_at",
    "profiler",
    "sampling_strategy",
    "requested_sample_rows",
    "sampled_rows",
    "eligible_population_rows",
    "sampling_provenance",
    "structural_profile",
    "unredacted_profile",
    "redacted_profile",
)


class ProfileLibraryError(Exception):
    """Raised when a profile-library record cannot be resolved or persisted."""


def resolve_profile_library_path(ctx: Any) -> Path:
    """Return the installation-configured ``file_profiles`` path."""
    resolver = getattr(ctx, "paths", None)
    resolve = getattr(resolver, "resolve", None)
    if not callable(resolve):
        raise ProfileLibraryError(
            "Resolved context carries no path resolver; the installation-owned "
            "'file_profiles' path cannot be resolved."
        )
    try:
        return Path(resolve(_PATH_NAME))
    except Exception as exc:
        raise ProfileLibraryError(
            "Installation configuration does not define a resolvable "
            f"'{_PATH_NAME}' path: {exc}"
        ) from exc


def append_profile_record(ctx: Any, record: Mapping[str, Any]) -> str:
    """Atomically store the one current record for ``object_id``.

    The historical name is retained for callers introduced with schema v1, but
    storage is current-state, not history: a write replaces the prior record
    carrying the same object key and preserves every other object's record.
    """
    content = _validated_content(record)
    profile_id = str(uuid4())
    complete = {
        "profile_id": profile_id,
        **content,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }
    canonical = {
        field: complete[field]
        for field in _CANONICAL_FIELDS
        if field in complete
    }
    target = resolve_profile_library_path(ctx)
    try:
        from rey_lib.files.jsonl import JsonlReadError, read_jsonl_file, write_jsonl_file

        target.parent.mkdir(parents=True, exist_ok=True)
        with file_manifest_session(ctx):
            current = (
                [dict(item.record) for item in read_jsonl_file(target)]
                if target.exists()
                else []
            )
            retained = [
                item for item in current
                if _record_object_id(item) != canonical["object_id"]
            ]
            write_jsonl_file(target, [*retained, canonical])
    except (OSError, TypeError, ValueError, JsonlReadError, FileManifestError) as exc:
        raise ProfileLibraryError(
            f"Profile record could not be stored in '{target}': {exc}"
        ) from exc
    return profile_id


def read_profile_records(ctx: Any) -> list[dict[str, Any]]:
    """Strictly read all profile records in physical append order."""
    target = resolve_profile_library_path(ctx)
    if not target.exists():
        return []
    try:
        from rey_lib.files.jsonl import JsonlReadError, read_jsonl_file

        with file_manifest_session(ctx):
            return [dict(item.record) for item in read_jsonl_file(target)]
    except (OSError, JsonlReadError, FileManifestError) as exc:
        raise ProfileLibraryError(
            f"Profile records could not be read from '{target}': {exc}"
        ) from exc


def lookup_profile_record(
    ctx: Any,
    object_id: str,
    source_hash: str,
) -> dict[str, Any]:
    """Return available, missing, or stale state for one file object."""
    key = _required_text(object_id, "object_id")
    current_hash = _required_text(source_hash, "source_hash")
    matches = [
        record for record in read_profile_records(ctx)
        if _record_object_id(record) == key
    ]
    if not matches:
        return {"status": "profile_missing", "object_id": key, "record": None}
    record = matches[-1]
    if record.get("source_hash") != current_hash:
        return {"status": "profile_stale", "object_id": key, "record": None}
    return {"status": "profile_available", "object_id": key, "record": record}


def _validated_content(record: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise ProfileLibraryError("A profile record must be a JSON object.")
    supplied = set(record)
    writer_fields = sorted(supplied & _WRITER_FIELDS)
    if writer_fields:
        raise ProfileLibraryError(
            "Profile record must not supply writer-owned field(s): "
            + ", ".join(writer_fields)
        )
    missing = sorted(_REQUIRED_FIELDS - supplied)
    if missing:
        raise ProfileLibraryError(
            "Profile record is missing required field(s): " + ", ".join(missing)
        )
    unknown = sorted(supplied - _REQUIRED_FIELDS - _OPTIONAL_FIELDS)
    if unknown:
        raise ProfileLibraryError(
            "Profile record carries unknown field(s): " + ", ".join(unknown)
        )
    if record.get("profile_schema_version") != _PROFILE_SCHEMA_VERSION:
        raise ProfileLibraryError("profile_schema_version must be 1.")
    for field in ("object_id", "file_id", "source_path", "source_hash", "dataset_id"):
        _required_text(record.get(field), field)
    if record["object_id"].strip() != record["file_id"].strip():
        raise ProfileLibraryError("For file profiles, object_id must equal file_id.")
    source_size = record.get("source_size")
    if not isinstance(source_size, int) or isinstance(source_size, bool) or source_size < 0:
        raise ProfileLibraryError("source_size must be a non-negative integer.")
    for field in ("profiler", "sampling_provenance", "structural_profile",
                  "unredacted_profile", "redacted_profile"):
        if not isinstance(record.get(field), Mapping):
            raise ProfileLibraryError(f"{field} must be a JSON object.")
    requested = _positive_int(record.get("requested_sample_rows"), "requested_sample_rows")
    sampled = _non_negative_int(record.get("sampled_rows"), "sampled_rows")
    eligible = _non_negative_int(
        record.get("eligible_population_rows"), "eligible_population_rows"
    )
    if sampled > requested or sampled > eligible:
        raise ProfileLibraryError(
            "sampled_rows cannot exceed requested_sample_rows or "
            "eligible_population_rows."
        )
    return dict(record)


def _positive_int(value: Any, field: str) -> int:
    number = _non_negative_int(value, field)
    if number == 0:
        raise ProfileLibraryError(f"{field} must be positive.")
    return number


def _non_negative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProfileLibraryError(f"{field} must be a non-negative integer.")
    return value


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileLibraryError(f"{field} must be a non-empty string.")
    return value.strip()


def _record_object_id(record: Mapping[str, Any]) -> str:
    """Return the current key, accepting Phase 3 file_id records on first read."""
    value = record.get("object_id", record.get("file_id"))
    return str(value or "").strip()
