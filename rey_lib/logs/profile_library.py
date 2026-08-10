"""Installation-scoped current-state governed profile library."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Iterable
from typing import Any, Mapping
from uuid import uuid4

from rey_lib.logs.file_manifest import FileManifestError, file_manifest_session

__all__ = [
    "ProfileLibraryError",
    "append_profile_record",
    "lookup_profile_record",
    "read_profile_records",
    "remove_profile_records",
    "resolve_profile_library_path",
]

_PATH_NAME = "file_profiles"
_PROFILE_SCHEMA_VERSION = 1
_WRITER_FIELDS = frozenset({"profile_id", "log_record_id", "created_at"})
# Writer-owned fields a stored record must already carry. log_record_id is
# deliberately absent: it is assigned to everything written from now on, but a
# record stored before the field existed is still a valid record, and failing
# its validation would make every existing profile library unreadable.
_RETIRED_HEADER_FIELDS = frozenset({"source_row_id"})
_REQUIRED_HEADER_FIELDS = frozenset({
    "profile_schema_version",
    "object_id",
    "source_hash",
    "profiler",
    "sampling_strategy",
    "requested_sample_rows",
    "sampled_rows",
    "eligible_population_rows",
    "sampling_provenance",
})
_CANONICAL_HEADER_FIELDS = (
    "profile_id",
    "log_record_id",
    "profile_schema_version",
    "object_id",
    "source_hash",
    "created_at",
    "profiler",
    "sampling_strategy",
    "requested_sample_rows",
    "sampled_rows",
    "eligible_population_rows",
    "sampling_provenance",
)
_STRUCTURE_FIELDS = frozenset({
    "header_definition",
    "distribution",
    "columns",
    "redacted_samples",
    "samples",
})
_SAMPLE_FIELDS = frozenset({
    "sample_values",
    "null_like_values",
    "constant_value",
    "min_numeric",
    "max_numeric",
    "min_date",
    "max_date",
})
_COLUMN_FIELDS = frozenset({
    "name",
    "type",
    "blank_count",
    "min_length",
    "max_length",
    "min_decimal_places",
    "max_decimal_places",
    "has_leading_zero",
    "contains_commas",
    "contains_currency_symbol",
    "negative_format",
})


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
    """Atomically store the one current record for ``header.object_id``."""
    content = _validated_content(record)
    profile_id = str(uuid4())
    header = content["header"]
    complete_header = {
        "profile_id": profile_id,
        # The profile log's own identity for this record, and what a rollback
        # names to remove exactly it. A UUID rather than a position: the append
        # below rewrites the file without the records this one supersedes, so a
        # positional id would move under a later write.
        "log_record_id": str(uuid4()),
        **header,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }
    canonical = {
        "header": {
            field: complete_header[field]
            for field in _CANONICAL_HEADER_FIELDS
        },
        "structure": content["structure"],
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
            for item in current:
                _validate_stored_record(item)
            retained = [
                item for item in current
                if _record_object_id(item) != canonical["header"]["object_id"]
            ]
            write_jsonl_file(target, [*retained, canonical])
    except (OSError, TypeError, ValueError, JsonlReadError, FileManifestError) as exc:
        raise ProfileLibraryError(
            f"Profile record could not be stored in '{target}': {exc}"
        ) from exc
    return profile_id


def read_profile_records(ctx: Any) -> list[dict[str, Any]]:
    """Strictly read all current governed profile records."""
    target = resolve_profile_library_path(ctx)
    if not target.exists():
        return []
    try:
        from rey_lib.files.jsonl import JsonlReadError, read_jsonl_file

        with file_manifest_session(ctx):
            records = [dict(item.record) for item in read_jsonl_file(target)]
        for record in records:
            _validate_stored_record(record)
        return records
    except (OSError, JsonlReadError, FileManifestError) as exc:
        raise ProfileLibraryError(
            f"Profile records could not be read from '{target}': {exc}"
        ) from exc


def remove_profile_records(ctx: Any, *, log_record_ids: Iterable[str]) -> int:
    """Remove exactly the profile records named by ``log_record_ids``.

    The governed-rewrite counterpart of :func:`append_profile_record`, and the
    deletion primitive a rollback calls once it knows which profile-log record
    it owns. ``header.log_record_id`` is the only field consulted: the manifest
    identities carried on a record say what was profiled, not which profile-log
    row this is, so matching on them would delete by the wrong identity space.

    Removal is exact. Every other record survives unchanged, no superseded
    profile is restored — those were already dropped when they were superseded —
    and no compensating record is appended.

    Returns how many records were removed. An id matching nothing removes
    nothing and is not an error, following the manifest's removal helper.
    """
    targets = {_required_text(value, "log_record_id") for value in log_record_ids}
    if not targets:
        return 0
    target = resolve_profile_library_path(ctx)
    if not target.exists():
        return 0
    try:
        from rey_lib.files.jsonl import JsonlReadError, read_jsonl_file, write_jsonl_file

        with file_manifest_session(ctx):
            current = [dict(item.record) for item in read_jsonl_file(target)]
            for record in current:
                _validate_stored_record(record)
            retained = [
                record for record in current
                if _record_log_record_id(record) not in targets
            ]
            if len(retained) != len(current):
                write_jsonl_file(target, retained)
    except (OSError, TypeError, ValueError, JsonlReadError, FileManifestError) as exc:
        raise ProfileLibraryError(
            f"Profile records could not be removed from '{target}': {exc}"
        ) from exc
    return len(current) - len(retained)


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
    if record["header"]["source_hash"] != current_hash:
        return {"status": "profile_stale", "object_id": key, "record": None}
    return {"status": "profile_available", "object_id": key, "record": record}


def _validated_content(record: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise ProfileLibraryError("A profile record must be a JSON object.")
    supplied = set(record)
    if supplied != {"header", "structure"}:
        missing = sorted({"header", "structure"} - supplied)
        unknown = sorted(supplied - {"header", "structure"})
        if missing:
            raise ProfileLibraryError(
                "Profile record is missing required field(s): " + ", ".join(missing)
            )
        raise ProfileLibraryError(
            "Profile record carries unknown field(s): " + ", ".join(unknown)
        )
    header = record.get("header")
    structure = record.get("structure")
    if not isinstance(header, Mapping):
        raise ProfileLibraryError("header must be a JSON object.")
    if not isinstance(structure, Mapping):
        raise ProfileLibraryError("structure must be a JSON object.")
    _validate_header(header, stored=False)
    _validate_structure(structure)
    return {"header": dict(header), "structure": dict(structure)}


def _validate_stored_record(record: Mapping[str, Any]) -> None:
    if not isinstance(record, Mapping) or set(record) != {"header", "structure"}:
        raise ProfileLibraryError(
            "Stored profile record must contain only header and structure."
        )
    header = record.get("header")
    structure = record.get("structure")
    if not isinstance(header, Mapping) or not isinstance(structure, Mapping):
        raise ProfileLibraryError(
            "Stored profile header and structure must be JSON objects."
        )
    _validate_header(header, stored=True)
    _validate_structure(structure)


def _validate_header(header: Mapping[str, Any], *, stored: bool) -> None:
    supplied = set(header)
    writer_fields = sorted(supplied & _WRITER_FIELDS)
    if not stored and writer_fields:
        raise ProfileLibraryError(
            "Profile header must not supply writer-owned field(s): "
            + ", ".join(writer_fields)
        )
    required = _REQUIRED_HEADER_FIELDS | (_WRITER_FIELDS if stored else frozenset())
    missing = sorted(required - supplied)
    if missing:
        raise ProfileLibraryError(
            "Profile header is missing required field(s): " + ", ".join(missing)
        )
    unknown = sorted(supplied - required)
    if unknown:
        raise ProfileLibraryError(
            "Profile header carries unknown field(s): " + ", ".join(unknown)
        )
    if header.get("profile_schema_version") != _PROFILE_SCHEMA_VERSION:
        raise ProfileLibraryError("profile_schema_version must be 1.")
    _required_text(header.get("object_id"), "object_id")
    # object_id is the manifest row of the profiled object; log_record_id is the
    # profile log's own row. Two identity spaces, never compared — requiring
    # them equal is what made the retired field ambiguous.
    retired = sorted(supplied & _RETIRED_HEADER_FIELDS)
    if retired:
        raise ProfileLibraryError(
            "header carries retired field(s): " + ", ".join(retired)
        )
    for field in ("source_hash", "sampling_strategy"):
        _required_text(header.get(field), field)
    for field in ("profiler", "sampling_provenance"):
        if not isinstance(header.get(field), Mapping):
            raise ProfileLibraryError(f"{field} must be a JSON object.")
    if stored:
        _required_text(header.get("profile_id"), "profile_id")
        _required_text(header.get("created_at"), "created_at")
    requested = _positive_int(
        header.get("requested_sample_rows"), "requested_sample_rows"
    )
    sampled = _non_negative_int(header.get("sampled_rows"), "sampled_rows")
    eligible = _non_negative_int(
        header.get("eligible_population_rows"), "eligible_population_rows"
    )
    if sampled > requested or sampled > eligible:
        raise ProfileLibraryError(
            "sampled_rows cannot exceed requested_sample_rows or "
            "eligible_population_rows."
        )


def _validate_structure(structure: Mapping[str, Any]) -> None:
    supplied = set(structure)
    if supplied != _STRUCTURE_FIELDS:
        missing = sorted(_STRUCTURE_FIELDS - supplied)
        unknown = sorted(supplied - _STRUCTURE_FIELDS)
        if missing:
            raise ProfileLibraryError(
                "Profile structure is missing required field(s): "
                + ", ".join(missing)
            )
        raise ProfileLibraryError(
            "Profile structure carries unknown field(s): " + ", ".join(unknown)
        )
    distribution = structure.get("distribution")
    if not isinstance(distribution, Mapping):
        raise ProfileLibraryError("distribution must be a JSON object.")
    moved = sorted(
        set(distribution)
        & {
            "columns",
            "source",
            "source_files",
            "detected_header",
            "header",
            "llm_hints",
        }
    )
    if moved:
        raise ProfileLibraryError(
            "distribution carries moved or retired field(s): " + ", ".join(moved)
        )
    if "profile_version" in distribution:
        raise ProfileLibraryError(
            "distribution carries retired field(s): profile_version"
        )
    profile_header = structure.get("header_definition")
    if not isinstance(profile_header, Mapping) or set(profile_header) != {
        "row_number",
        "columns",
    }:
        raise ProfileLibraryError(
            "header_definition must contain only row_number and columns."
        )
    _positive_int(profile_header.get("row_number"), "header_definition.row_number")
    header_columns = profile_header.get("columns")
    if (
        not isinstance(header_columns, list)
        or not header_columns
        or not all(isinstance(value, str) and value for value in header_columns)
    ):
        raise ProfileLibraryError(
            "header_definition.columns must be a non-empty string array."
        )
    columns = structure.get("columns")
    samples = structure.get("samples")
    redacted = structure.get("redacted_samples")
    if not isinstance(columns, list):
        raise ProfileLibraryError("columns must be a JSON array.")
    if not isinstance(samples, list):
        raise ProfileLibraryError("samples must be a JSON array.")
    if not isinstance(redacted, list):
        raise ProfileLibraryError("redacted_samples must be a JSON array.")
    if len(columns) != len(samples) or len(samples) != len(redacted):
        raise ProfileLibraryError(
            "columns, samples, and redacted_samples must have equal lengths."
        )
    column_names = [
        column.get("raw_name", column.get("name"))
        if isinstance(column, Mapping)
        else None
        for column in columns
    ]
    if column_names != header_columns:
        raise ProfileLibraryError(
            "header_definition.columns must match structure.columns in order."
        )
    for position, (column, clear, safe) in enumerate(
        zip(columns, samples, redacted, strict=True), start=1
    ):
        if not isinstance(column, Mapping):
            raise ProfileLibraryError(f"columns[{position}] must be a JSON object.")
        value_fields = sorted(set(column) & _SAMPLE_FIELDS)
        if value_fields:
            raise ProfileLibraryError(
                f"columns[{position}] carries value-bearing field(s): "
                + ", ".join(value_fields)
            )
        unknown_column_fields = sorted(set(column) - _COLUMN_FIELDS)
        if unknown_column_fields:
            raise ProfileLibraryError(
                f"columns[{position}] carries non-canonical field(s): "
                + ", ".join(unknown_column_fields)
            )
        _validate_sample(clear, position, "samples")
        _validate_sample(safe, position, "redacted_samples")
        if set(clear) != set(safe) or clear["column"] != safe["column"]:
            raise ProfileLibraryError(
                f"samples[{position}] and redacted_samples[{position}] must "
                "have the same shape and column identity."
            )
        if "sample_values" in clear:
            clear_entries = clear["sample_values"]
            safe_entries = safe["sample_values"]
            if len(clear_entries) != len(safe_entries) or [
                entry["count"] for entry in clear_entries
            ] != [entry["count"] for entry in safe_entries]:
                raise ProfileLibraryError(
                    f"samples[{position}].sample_values and redacted_samples"
                    f"[{position}].sample_values must preserve count and order."
                )
        column_name = column.get("name")
        if not isinstance(column_name, str) or clear["column"] != column_name:
            raise ProfileLibraryError(
                f"columns[{position}] and samples[{position}] must identify "
                "the same real column name."
            )


def _validate_sample(value: Any, position: int, field: str) -> None:
    if not isinstance(value, Mapping):
        raise ProfileLibraryError(f"{field}[{position}] must be a JSON object.")
    unknown = sorted(set(value) - _SAMPLE_FIELDS - {"column"})
    if unknown:
        raise ProfileLibraryError(
            f"{field}[{position}] carries unknown field(s): " + ", ".join(unknown)
        )
    if not isinstance(value.get("column"), str):
        raise ProfileLibraryError(f"{field}[{position}].column must be a string.")
    if "sample_values" in value:
        entries = value["sample_values"]
        if not isinstance(entries, list):
            raise ProfileLibraryError(
                f"{field}[{position}].sample_values must be a JSON array."
            )
        for entry_position, entry in enumerate(entries, start=1):
            if not isinstance(entry, Mapping) or set(entry) != {"value", "count"}:
                raise ProfileLibraryError(
                    f"{field}[{position}].sample_values[{entry_position}] must "
                    "contain only value and count."
                )
            if not isinstance(entry.get("value"), str) or not entry["value"]:
                raise ProfileLibraryError(
                    f"{field}[{position}].sample_values[{entry_position}].value "
                    "must be a non-empty string."
                )
            _positive_int(
                entry.get("count"),
                f"{field}[{position}].sample_values[{entry_position}].count",
            )


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


def _record_log_record_id(record: Mapping[str, Any]) -> str:
    header = record.get("header")
    if not isinstance(header, Mapping):
        return ""
    return str(header.get("log_record_id") or "").strip()


def _record_object_id(record: Mapping[str, Any]) -> str:
    header = record.get("header")
    if not isinstance(header, Mapping):
        return ""
    return str(header.get("object_id") or "").strip()
