"""Installation-scoped current-state governed profile library."""

from __future__ import annotations

from copy import deepcopy
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
# log_record_id was a UUID this module minted for itself. It pointed at nothing:
# a profile carrying it could not be resolved back to the run that wrote it.
# run_log_file followed it: the only thing that ever read it was the run-log
# driven rollback selector, and that engine has been retired in favour of the
# pending-mutation rows. The run-log record id remains, because it does resolve.
_RETIRED_HEADER_FIELDS = frozenset({"source_row_id", "log_record_id"})
_REQUIRED_HEADER_FIELDS = frozenset({
    "profile_schema_version",
    "object_id",
    "source_hash",
    # The supporting run-log record, supplied by the producer before this
    # record is appended. Evidence-first, as the governed mutation model is.
    "evidence",
    "profiler",
    "sampling_strategy",
    "requested_sample_rows",
    "sampled_rows",
    "eligible_population_rows",
    "sampling_provenance",
})
_EVIDENCE_FIELDS = frozenset({"run_log_id"})
_CANONICAL_HEADER_FIELDS = (
    "profile_id",
    "profile_schema_version",
    "object_id",
    "source_hash",
    "created_at",
    "evidence",
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
    # The exact source header text, kept beside the canonical identity so
    # header_definition.columns can be compared against what the file actually
    # said. See the positional check below, which prefers it and falls back to
    # name only for records written before it was retained.
    "raw_name",
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
    # Imported inside the function because rey_lib.files imports rey_lib.logs, so
    # a module-scope import would cycle. It is kept outside the try because the
    # except clause names JsonlReadError: binding it inside would turn any import
    # failure into an UnboundLocalError that hides the real cause.
    from rey_lib.files.jsonl import JsonlReadError, read_jsonl_file, write_jsonl_file

    try:
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
    # Function-local and outside the try for the reasons given in
    # append_profile_record.
    from rey_lib.files.jsonl import JsonlReadError, read_jsonl_file

    try:
        with file_manifest_session(ctx):
            records = [dict(item.record) for item in read_jsonl_file(target)]
        for record in records:
            _validate_stored_record(record)
        return records
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
    # object_id is the manifest row of the profiled object; evidence points at
    # the run-log record that produced this profile. Two identity spaces, never
    # compared — requiring them equal is what made the retired field ambiguous.
    retired = sorted(supplied & _RETIRED_HEADER_FIELDS)
    if retired:
        raise ProfileLibraryError(
            "header carries retired field(s): " + ", ".join(retired)
        )
    _validate_evidence(header.get("evidence"))
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
    # distribution says what the dataset looks like. Read instructions belong to
    # loader_hints, and the csv subsection restated both, so all three are
    # refused here rather than allowed to drift apart in two places.
    moved = sorted(
        set(distribution)
        & {
            "columns",
            "source",
            "source_files",
            "detected_header",
            "header",
            "llm_hints",
            "csv",
            "delimiter",
            "encoding",
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
    # TEMPORARILY RELAXED. This required header_definition.columns to equal the
    # per-column names positionally, and rejected real files whose detected
    # header and profiled columns disagree. Restore it once the producer's
    # raw_name / name identities are settled; see the sample-identity check
    # below, which still pins columns[i] to samples[i].
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


def _validate_evidence(evidence: Any) -> None:
    """Validate the pointer back to the run-log record that produced a profile.

    The same pair every governed record carries: the run log's filename, which
    rollback selects exact matches on, and the positive integer row number of
    the supporting record within it. A profile that cannot be resolved back to
    its run is unverifiable, so an incomplete pointer fails before it is stored.
    """
    if not isinstance(evidence, Mapping):
        raise ProfileLibraryError("evidence must be a JSON object.")
    supplied = set(evidence)
    if supplied != _EVIDENCE_FIELDS:
        missing = sorted(_EVIDENCE_FIELDS - supplied)
        if missing:
            raise ProfileLibraryError(
                "evidence is missing required field(s): " + ", ".join(missing)
            )
        raise ProfileLibraryError(
            "evidence carries unknown field(s): "
            + ", ".join(sorted(supplied - _EVIDENCE_FIELDS))
        )
    _positive_int(evidence.get("run_log_id"), "run_log_id")


def _record_object_id(record: Mapping[str, Any]) -> str:
    header = record.get("header")
    if not isinstance(header, Mapping):
        return ""
    return str(header.get("object_id") or "").strip()

PROFILE_ACCESS_REDACTED = "redacted"
PROFILE_ACCESS_UNREDACTED = "unredacted"
_ACCESS_SAMPLE_FIELDS = {
    PROFILE_ACCESS_REDACTED: "redacted_samples",
    PROFILE_ACCESS_UNREDACTED: "samples",
}


def resolve_profile_presentation(
    record: Mapping[str, Any],
    access: str,
    *,
    object_id: str = "",
) -> dict[str, Any]:
    """Return the record carrying one sample presentation, with the other removed.

    A stored profile holds both presentations side by side — ``structure.samples``
    and ``structure.redacted_samples``. This selects one and deletes the other
    from the copy it returns, so a caller handed the redacted view has no path
    back to the clear values.

    This answers only "give me the clear or redacted representation of this
    record". Whether a particular consumer may receive that representation is a
    separate question, asked one layer up: ``profile_access.allowed`` and
    ``profile_access.default`` govern what a model may be sent and are enforced by
    ``rey_lib.llm.profiles.resolve_profile_for_llm``. An operator reading the two
    presentations in the tree is not subject to that policy and does not consult
    it.

    Parameters
    ----------
    record : Mapping[str, Any]
        The stored profile record. Never modified.
    access : str
        ``redacted`` or ``unredacted``.
    object_id : str
        Identity used in failure messages only.

    Returns
    -------
    dict[str, Any]
        A copy carrying exactly one sample presentation.

    Raises
    ------
    ProfileLibraryError
        If ``access`` names no known presentation, or the record has no valid
        structure, or it lacks the requested presentation.
    """
    selected = str(access or "").strip()
    if selected not in _ACCESS_SAMPLE_FIELDS:
        raise ProfileLibraryError(
            "profile access may be only redacted or unredacted."
        )

    resolved = deepcopy(dict(record))
    structure = resolved.get("structure")
    if not isinstance(structure, dict):
        raise ProfileLibraryError(
            f"Current profile record for object_id '{object_id}' has no valid structure."
        )
    selected_field = _ACCESS_SAMPLE_FIELDS[selected]
    rejected_field = (
        "samples" if selected_field == "redacted_samples" else "redacted_samples"
    )
    if not isinstance(structure.get(selected_field), list):
        raise ProfileLibraryError(
            f"Current profile record for object_id '{object_id}' has no valid "
            f"{selected_field}."
        )
    structure.pop(rejected_field, None)
    return resolved
