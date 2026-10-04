"""The Loader's record-type profiling (row 589, step 5).

Copied from the legacy file_operator workflow module's profiling functions,
which stay untouched as the reference: the selection, the profile library
record, the canonical DataProfile, its persistence. What changed, and only
because the boundary required it:

* each selected state is opened through ``ManifestSource`` at exactly the
  mutation the selector named, and profiled from that DataFile's path;
* a file that cannot be profiled is KICKED OUT (rule 75,
  ``a_failed_file_is_kicked_out``): the ORIGINAL at its processing state --
  never the sanitized copy that was profiled -- moves to the declared kickouts
  destination through ``Transform(move)``, and only then is the failure
  recorded and counted. The kickout never raises; a move that cannot be made
  is logged and the file still counts as failed;
* the error is ``ProfilingError``; the profiler's application is the runtime
  context's and its version is the one the application supplies.

    selected row -> ManifestSource(file_mutation_id) -> DataFile (sanitized)
      -> build_csv_parts / build_clean_single_file_profile -> DataProfile
      -> run-log evidence -> M16 profile record -> profile store
    on failure:
      FileManifest.history -> the original's latest live successful move
      (result moved_to_processing) -> ManifestSource -> DataFile
      -> kickouts.path resolved from that governed record
      -> Transform(move, role=kickouts, name=<resolved file name>)
      -> ERROR record, failure counted
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from rey_lib.data.data_profile import DataProfile, FieldProfile, ProfileField
from rey_lib.data.errors import DataStructureError
from rey_lib.encryption import sha256_file
from rey_lib.errors.error_utils import ConfigError, DatabaseError
from rey_lib.files.csv import normalized_header
from rey_lib.load.layouts.common import effective_max_sample_rows
from rey_lib.load.layouts.delimited import (
    build_clean_single_file_profile,
    build_csv_parts,
)
from rey_lib.load.manifest_source import ManifestSource
from rey_lib.load.profile_errors import ProfilingError
from rey_lib.files.data_file import DataFile, data_file_for
from rey_lib.load.file_transform import FileTransform, file_transform
from rey_lib.load.transform import Transform
from rey_lib.load.record_templates import record_field
from rey_lib.logs import (
    PROFILE_RECORD_TYPE,
    FileManifestError,
    bound_run_log,
    get_logger,
    log_file_manifest_record,
    log_run_record,
)
from rey_lib.profiling import redact_profile
from rey_lib.profiling.csv_profile import PROFILE_VERSION as CSV_PROFILE_VERSION

__all__ = [
    "ProfilingBatchResult",
    "ProfilingError",
    "run_record_type_profiling",
]

_log = get_logger(__name__)

#: The operation every profiling record and kickout move states.
_OPERATION = "record_type_profiling"

# The run-log record a governed profile points back at, named for what it
# records as SOURCE_FILE_INVENTORY and SOURCE_FILE_MUTATION are.
_PROFILE_RUN_RECORD_TYPE = "SOURCE_FILE_PROFILE"

_SAMPLE_FIELDS = (
    "sample_values",
    "null_like_values",
    "constant_value",
    "min_numeric",
    "max_numeric",
    "min_date",
    "max_date",
)
_CANONICAL_COLUMN_FIELDS = frozenset({
    # Two legitimate identities per column. raw_name is the exact source header
    # text, which header_definition.columns is compared against positionally;
    # name is the canonical identity after disambiguation or normalization.
    # Dropping raw_name here made that comparison fall back to name and fail on
    # any file whose header needed normalizing.
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

#: What a column entry calls each thing, against the name the profile object
#: gives it. The left side is the profiler's vocabulary and the right the
#: shared one; this is the one place the two meet, which is why nothing
#: downstream -- the profile object or the database -- ever learns the former.
_FIELD_FROM_COLUMN = {
    "type": "detected_type",
    "blank_count": "blank_count",
    "min_length": "min_length",
    "max_length": "max_length",
    "min_decimal_places": "min_decimal_places",
    "max_decimal_places": "max_decimal_places",
}

#: The same, for the value-bearing half a sample entry carries.
_FIELD_FROM_SAMPLE = {
    "min_numeric": "min_numeric",
    "max_numeric": "max_numeric",
    "min_date": "min_date",
    "max_date": "max_date",
    "sample_values": "sample_values",
    "null_like_values": "null_like_values",
    "constant_value": "constant_value",
}


def _value(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def _readings_of(
    columns: Sequence[Mapping[str, Any]],
    samples: Sequence[Mapping[str, Any]],
) -> tuple[FieldProfile, ...]:
    """One reading per column, in the profiler's vocabulary no longer.

    THE ADAPTER BOUNDARY, and the only one. Each reading is the column's own
    facts plus the value-bearing facts of its sample, translated once through
    the two mapping tables. After this nothing translates again -- the field
    names the object carries are the shared ones, so persisting it is reading
    attributes rather than crossing a second vocabulary.

    Samples are matched to their column BY NAME, never by position: two lists
    agreeing today is not a reason to depend on their order, and a column with
    no sample entry is a real case -- it keeps its column facts and carries no
    value-bearing ones.
    """
    by_name = {
        str(entry.get("column")): entry
        for entry in samples
        if isinstance(entry, Mapping)
    }
    readings: list[FieldProfile] = []
    for ordinal, column in enumerate(columns, start=1):
        name = column.get("name") or column.get("raw_name")
        if not name:
            continue
        sample = by_name.get(str(name)) or {}
        readings.append(FieldProfile(
            name=str(name),
            ordinal=ordinal,
            # THE HEADER PREPARE WILL WRITE, by the function prepare calls --
            # not a second normalization, and not the profiler's own names.
            prepared_name=normalized_header([str(name)])[0],
            **{field: column.get(source)
               for source, field in _FIELD_FROM_COLUMN.items()},
            **{field: sample.get(source)
               for source, field in _FIELD_FROM_SAMPLE.items()},
        ))
    return tuple(readings)


def _every_row_matched_the_header(
    distribution: Mapping[str, Any],
    header_columns: Sequence[str],
) -> bool:
    """Whether the whole source was checked against the header, and passed.

    THE EVIDENCE BEHIND ``structure_validated``, and the reason this takes the
    header rather than reading the count on its own.

    ``rey_lib.files.csv`` counts a row as ragged when its field count differs
    from the header's, over EVERY line rather than a sample. So a zero count
    looks like proof that the file is uniform -- but the check disables
    itself:

        is_ragged = in_data_region and bool(expected_width) ...

    ``expected_width`` is the located header's width, so a read that found no
    header reports zero because it NEVER LOOKED, and the empty-text early
    return reports zero the same way. Zero is three different situations and
    only one of them is evidence.

    A non-empty ``header_columns`` is what separates them. The caller proves
    it -- it raises unless the header was located with at least one named
    column -- so a header here means ``expected_width`` was non-zero and the
    comparison was live for that read.

    **WHAT A TRUE ANSWER CLAIMS, exactly:** every non-blank row in the data
    region carried the header's field count. That is a claim about WIDTH and
    nothing else. Not types, not nullability, not values, not that any row
    means what its column says. A consumer skipping work on the strength of
    it may skip re-counting fields, and nothing more.

    Anything unexpected is False. Absence of evidence is not evidence, which
    is the whole of what this flag is for.
    """
    if not header_columns:
        # The comparison was disabled, so the count describes nothing.
        return False
    counted = distribution.get("ragged_row_count")
    # `bool` first: True is an int and would otherwise read as a count of 1
    # -- and a count of 1 is not zero, so it would answer False by accident
    # rather than by rule.
    if isinstance(counted, bool) or not isinstance(counted, int):
        return False
    return counted == 0


def _canonical_profile_structure(
    distribution_profile: Mapping[str, Any],
) -> DataProfile:
    """Describe the profiled data as the shared profile object.

    THE ADAPTER from the profiler's enriched dict to ``DataProfile``. The dict
    it reads stays exactly as it is -- it is the ``.profile.json`` artifact
    contract and carries hints this object has no opinion about. What this
    removes is the SECOND representation that used to sit between them.

    The structural fields come from the HEADER, and the readings from the
    sampled columns. They are different lengths on purpose: a short or ragged
    first data row sets the sample's width, which is how a 19-field header was
    once recorded with a field_count of 2. ``fields`` describes the structure,
    so it is taken from the row that declares it.
    """
    distribution = deepcopy(dict(distribution_profile))
    distribution.pop("profile_version", None)
    supplied_columns = distribution.pop("columns", None)
    distribution.pop("source", None)
    distribution.pop("source_files", None)
    distribution.pop("llm_hints", None)
    header_line = distribution.pop("header", None)
    detected_header = distribution.pop("detected_header", None)
    if not isinstance(supplied_columns, list):
        raise ProfilingError("Distribution profile columns must be a list.")
    if not isinstance(detected_header, Mapping):
        raise ProfilingError("Distribution profile must declare its header location.")
    header_row_number = detected_header.get("row_number")
    header_columns = detected_header.get("ordered_columns")
    if (
        not isinstance(header_row_number, int)
        or isinstance(header_row_number, bool)
        or header_row_number <= 0
    ):
        raise ProfilingError("Distribution profile header row_number must be positive.")
    if (
        not isinstance(header_columns, list)
        or not header_columns
        or not all(isinstance(value, str) and value for value in header_columns)
    ):
        raise ProfilingError("Distribution profile header columns must be strings.")
    detected_line = detected_header.get("line")
    if not isinstance(header_line, str) or detected_line != header_line:
        raise ProfilingError("Distribution profile header representations disagree.")
    # The header TEXT, not a JSON wrapper around it. It is a member of the
    # profile's natural key, so it is profile identity and is stored as the
    # line the file actually carried.
    #
    # row_number is validated above and deliberately NOT carried: how many
    # preamble lines one delivery happened to have is a fact about that
    # delivery, not about the structure, and nothing downstream reads it.
    header_definition = header_line

    columns: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    for position, supplied in enumerate(supplied_columns, start=1):
        if not isinstance(supplied, Mapping):
            raise ProfilingError(
                f"Distribution profile column {position} must be a JSON object."
            )
        column = deepcopy(dict(supplied))
        real_name = column.get("raw_name")
        if not isinstance(real_name, str):
            real_name = column.get("name")
        if not isinstance(real_name, str):
            raise ProfilingError(
                f"Distribution profile column {position} has no real column name."
            )
        sample: dict[str, Any] = {"column": real_name}
        for field in _SAMPLE_FIELDS:
            if field in column:
                sample[field] = column.pop(field)
        canonical_column = {
            field: value for field, value in column.items()
            if field in _CANONICAL_COLUMN_FIELDS
        }
        canonical_column["name"] = real_name
        columns.append(canonical_column)
        samples.append(sample)

    return DataProfile(
        # FROM THE HEADER ROW, not from the sampled data rows, so that
        # len(fields) is the count that describes the same structure
        # header_definition does -- both are profile identity.
        fields=tuple(
            ProfileField(name=str(name), ordinal=ordinal)
            for ordinal, name in enumerate(header_columns, start=1)
        ),
        # A delimited header names every column in order before a row is read,
        # which is what a complete structural definition IS.
        structure_complete=True,
        # This path DOES check the data against the structure -- every row's
        # width against the header's, over the whole source -- and this is
        # where that stops being discarded as a count. The header is passed
        # because the count alone cannot say whether the check ran.
        structure_validated=_every_row_matched_the_header(
            distribution, header_columns,
        ),
        clear=_readings_of(columns, samples),
        redacted=_readings_of(columns, redact_profile(samples)),
        header_definition=header_definition,
        distribution=distribution,
    )


def _store_profile_library_record(
    ctx: Any, run_log,
    source: Path,
    settings: Any,
    file_manifest_id: int,
    source_record_id: int | None = None,
    data_profile_key: str = "",
    key_fields: Sequence[str] = (),
    *,
    profiler_version: str = "",
) -> int:
    """Build both profile representations and persist them for the group.

    Profiling is a governed mutation like every other thing that happens to a
    file: the selector named a mutation, this profiles the file as that mutation
    placed it, and the result is appended as its own record pointing back at the
    one it consumed. It rewrites nothing -- profiling the same file again
    appends another complete profiling mutation.

    ``source_record_id`` is absent when nothing was selected. The workflow step
    always has one, because a selector handed it the mutation to profile; the
    Profile Source button was handed a governed file directly and consumed no
    prior record, which is what None already means on a mutation.

    The two representations are written together on that one record or not at
    all. They are two readings of a single profiling event, so a mutation
    carrying one without the other would describe something that never happened.
    """
    canonical_sample_rows = _value(settings, "max_sample_rows")
    legacy_sample_rows = _value(settings, "sample_size")
    if canonical_sample_rows is not None and legacy_sample_rows is not None:
        raise ProfilingError(
            "max_sample_rows and legacy sample_size cannot both be configured."
        )
    args = SimpleNamespace(
        delimiter=_value(settings, "delimiter"),
        encoding=_value(settings, "encoding") or "utf-8",
        max_sample_rows=(
            canonical_sample_rows
            if canonical_sample_rows is not None
            else legacy_sample_rows
        ),
        max_sample_values_per_column=None,
        redact_all=False,
        redact_columns=[],
        redact_masks={},
        header_contains=_value(settings, "header_contains"),
        skip_blank_lines=bool(_value(settings, "skip_blank_lines", False)),
        feed=None,
        profile_scope="single_file",
        source_files=[source.name],
        file_count=1,
    )
    source_hash = sha256_file(source)
    source_size = source.stat().st_size
    parts = build_csv_parts(source, args, redact_rows=False)
    distribution_profile = build_clean_single_file_profile(parts, args)
    structure = _canonical_profile_structure(distribution_profile)
    if distribution_profile.get("profile_version") != CSV_PROFILE_VERSION:
        raise ProfilingError(
            f"Distribution profile for '{source.name}' must use "
            f"{CSV_PROFILE_VERSION}."
        )
    if sha256_file(source) != source_hash or source.stat().st_size != source_size:
        raise ProfilingError(
            f"Source '{source.name}' changed while its profile was being built."
        )
    # Evidence first, as the governed mutation model is: the run-log record
    # commits before the profile that points at it, so a stored profile always
    # resolves back to the run that wrote it. A profile whose supporting record
    # did not commit is unverifiable and is not written at all.
    run_log_id = log_run_record(run_log,
        _PROFILE_RUN_RECORD_TYPE,
        message=f"Profiled governed file object '{file_manifest_id}'.",
        object_id=str(file_manifest_id),
        source_hash=source_hash,
        path=str(source),
    )
    if run_log_id is None:
        raise ProfilingError(
            f"Profile for governed file '{file_manifest_id}' was not stored: its "
            "supporting run-log record did not commit."
        )
    # How the profile was arrived at, carried on both representations: a reader
    # holding either one can say what was sampled and from how much.
    provenance = {
        "profile_schema_version": 1,
        "source_hash": source_hash,
        # Measured above, beside the hash, and carried here because the profile
        # store holds it. Without it control.data_profile.size_bytes stays NULL
        # and the profiling queue's completeness test never passes, so the file
        # is offered for profiling again on every run.
        "size_bytes": source_size,
        # The profiling application is the runtime context's, and its version
        # is the one that application supplies (row 589, step 5).
        "profiler": {
            "application": str(getattr(ctx, "app_name", "") or ""),
            "application_version": profiler_version,
        },
        "sampling_strategy": "random_without_replacement_v1",
        "requested_sample_rows": effective_max_sample_rows(args),
        "sampled_rows": len(parts.sampled_original),
        "eligible_population_rows": parts.data_line_count,
        "sampling_provenance": {
            "implementation": "rey_lib.files.csv.sample_indices",
            "strategy": "random_without_replacement_v1",
            "inputs": [
                "eligible_population_rows",
                "requested_sample_rows",
            ],
        },
    }
    try:
        mutation_id = log_file_manifest_record(ctx, {
            "record_type": PROFILE_RECORD_TYPE,
            # Observational. Profiling reads a file and changes nothing about
            # it, which is what record_only already means on a mutation.
            "action": "record_only",
            "status": "success",
            "file_id": file_manifest_id,
            "evidence": {"run_log_id": run_log_id},
            "file": {"path": str(source)},
            # What produced the mutation, and the outcome it produced. Both are
            # NOT NULL on control.file_mutation. Every mutation written through
            # serialize_source_file_mutation gets them for free; this record is
            # built for the boundary directly, so it states them itself.
            "producer": {
                "application": str(getattr(ctx, "app_name", "") or ""),
                "operation": _OPERATION,
            },
            "result": "record_type_profile",
            # The mutation this profiled -- what the selector returned. The file
            # is named above; this says which of its mutations was read, which
            # the file alone cannot.
            **({"lineage": {"source_record_id": source_record_id}}
               if source_record_id is not None else {}),
        })
    except (FileManifestError, ValueError) as exc:
        raise ProfilingError(
            f"Profiling mutation could not be written for '{source.name}': {exc}"
        ) from exc

    # Where the profile now lives.
    if data_profile_key:
        control = getattr(ctx, "shared_control", None)
        if control is None:
            raise ProfilingError(
                "Profiles are persisted in the control database, and this "
                "context exposes no shared Control to write them through."
            )
        try:
            _persist_profile(control, data_profile_key, provenance, structure,
                             file_manifest_id, key_fields)
        except (ConfigError, DatabaseError) as exc:
            raise ProfilingError(
                f"Profile for '{source.name}' could not be persisted: {exc}"
            ) from exc

    return mutation_id


def _persist_profile(
    control: Any,
    data_profile_key: str,
    provenance: Mapping[str, Any],
    structure: DataProfile,
    file_manifest_id: int,
    key_fields: Sequence[str],
) -> None:
    """Take the profile apart here, and hand the store values.

    The decomposition belongs on this side: the object is the profiler's and is
    understood here, so nothing downstream is handed one. Every fact crosses as
    itself, under its own name, into a column of its own.

    Two steps, in order, and the split is deliberate.

    STEP 1 -- IDENTITY AND LINKAGE. One call, one transaction: resolve the
    profile by its natural key -- installation, field count, grouping key and
    header -- resolve or create the file type that links to it, and stamp this
    file's ``file_type_id``. Those three writes are one responsibility and are
    not allowed to commit partially.

    STEP 2 -- PROFILE DETAIL. The field rows, written with the
    ``data_profile_id`` Step 1 returned. This step is EXPECTED to be partial:
    later runs fill whatever is still missing, which is why it is a separate
    responsibility rather than more of the same transaction.

    Step 2 never resolves the profile again -- not by key and not by walking
    file_manifest -> file_type -> data_profile. Either would be a second
    resolution path, and a second path is a chance to disagree with the first.

    Both readings are written under one profile identity. They describe a single
    profiling event and differ only in the values their fields carry, which is
    what ``data_profile_field_type`` says on each row.
    """
    profiler = dict(provenance.get("profiler") or {})

    identity = control.insert_data_profile(
        data_profile_key,
        # The key member: how many COLUMNS THE HEADER DECLARES. Constant across
        # deliveries of one layout, which is what a profile is identified by,
        # and derived from the same row header_definition is. Counted from the
        # structural fields rather than stored beside them, so there is one
        # answer instead of two that can disagree.
        len(structure.fields),
        structure.header_definition,
        # An attribute: how many ROWS this delivery carried.
        provenance.get("eligible_population_rows"),
        # THE FILE. Required, and what makes the stamp part of this transaction
        # rather than a step that can be missed.
        file_manifest_id,
        source_hash=provenance.get("source_hash"),
        profile_schema_version=provenance.get("profile_schema_version"),
        profile_method=profiler.get("application"),
        profile_method_version=profiler.get("application_version"),
        size_bytes=provenance.get("size_bytes"),
        distribution=structure.distribution,
    )
    data_profile_id = identity.get("o_data_profile_id")
    if data_profile_id is None:
        raise ProfilingError(
            "The profile store answered with no identity, so its fields have "
            "nothing to belong to."
        )
    # Checked separately rather than folded into the line above. The routine
    # commits all three writes or none, so a profile with no type means the
    # call did not do what it says -- a different fault from an absent profile,
    # and one a caller reading only the profile id would never see.
    file_type_id = identity.get("o_file_type_id")
    if file_type_id is None:
        raise ProfilingError(
            "The profile store resolved a profile but no file type, so this "
            "file has no link back to the profile that describes it."
        )

    # The profile is written independently of its field rows, so ask whether
    # this one already has them. The selector returns a file whose profile is
    # missing OR incomplete, and the queue is read once before the loop -- so
    # after the first file of a group finishes its profile, files 2..N of that
    # same group are still in hand and would otherwise repeat every field write
    # for ON CONFLICT to discard.
    #
    # Counted, not tested for presence: a half-written set is a state the
    # system can be in, and this is what finishes it.
    #
    # A GUARD RATHER THAN AN EARLY RETURN, because the transform definition is
    # maintained below on BOTH paths. Returning here would skip it for exactly
    # the files whose profile already has its fields -- which is the re-profiling
    # case self-healing exists for.
    if not control.data_profile_fields_complete(int(data_profile_id)):
        # No translation here. The readings already carry the shared names, which
        # are the store's names, so each field crosses as itself into a column of
        # its own -- which is what the decomposition was always for.
        for reading, fields in (
            ("clear", structure.clear),
            ("redacted", structure.redacted),
        ):
            for field in fields:
                control.insert_data_profile_field(
                    int(data_profile_id), field.name, reading,
                    ordinal=field.ordinal,
                    detected_type=field.detected_type,
                    blank_count=field.blank_count,
                    min_length=field.min_length,
                    max_length=field.max_length,
                    min_decimal_places=field.min_decimal_places,
                    max_decimal_places=field.max_decimal_places,
                    min_numeric=field.min_numeric,
                    max_numeric=field.max_numeric,
                    min_date=field.min_date,
                    max_date=field.max_date,
                    sample_values=field.sample_values,
                    null_like_values=field.null_like_values,
                    constant_value=field.constant_value,
                    prepared_name=field.prepared_name,
                )

    # THE DEFINITION FOLLOWS THE PROFILE, and the database does all of it.
    #
    # Last, because the routine seeds one column per profile field and those
    # fields must be written first. On the path above they just were; on the
    # other they already were, which is why that check is a guard and not a
    # return.
    #
    # ensure by default and by intention: profiling never resets. It creates a
    # definition where there is none and backfills what is missing, and leaves
    # every authored decision exactly as the author left it.
    control.maintain_transform(int(file_type_id))


def _collect_profiling_work(
    records: Any,
    source_field: str,
    work: dict[tuple[str, str], tuple[str, int, int, str, tuple[str, ...]]],
) -> None:
    """Add each selected output, keyed by governed identity and current path.

    One workbook-level file_id may govern several individually named converted
    CSV outputs. Each selected mutation record remains a distinct profiling
    object through its unique manifest record_id.

    The source manifest row is the profile identity; file_id remains manifest
    lineage and is not duplicated into the profile record.
    """
    for record in records:
        source = record_field(record, source_field)
        if not isinstance(source, str) or not source.strip():
            raise ProfilingError(
                f"Selected row {record.get('file_mutation_id')!r} has no "
                f"{source_field!r} to profile."
            )
        source_record_id = record.get("file_mutation_id")
        if (
            not isinstance(source_record_id, int)
            or isinstance(source_record_id, bool)
            or source_record_id <= 0
        ):
            raise ProfilingError(
                f"Selected manifest record {source_record_id!r} requires a "
                "positive integer record_id for profile identity."
            )
        file_manifest_id = record.get("file_manifest_id")
        if (
            not isinstance(file_manifest_id, int)
            or isinstance(file_manifest_id, bool)
            or file_manifest_id <= 0
        ):
            raise ProfilingError(
                f"Selected mutation {source_record_id!r} names no governed "
                "file to record its profile against."
            )
        normalized_source = str(Path(source).expanduser().resolve())
        identity = (str(source_record_id), normalized_source)
        work.setdefault(
            identity,
            (
                normalized_source,
                source_record_id,
                file_manifest_id,
                # Written by classification and read from the manifest. Never
                # rebuilt here: classification owns what a file groups as.
                str(record.get("data_profile_key") or ""),
                # The field names that key was built from, recorded on the
                # classification mutation and projected back by the selector.
                # Read for the same reason as the key itself and never rebuilt:
                # one construction, in the step that owns it.
                tuple(str(field) for field in (record.get("key_fields") or ())),
            ),
        )


def _profiling_record(selected: ManifestSource) -> dict[str, Any]:
    """What profiling reads of one governed file, from the object (backlog 619).

    ``data_profile_key`` here is the MANIFEST's key, which classification wrote
    -- the object's ``manifest_data_profile_key``. The object's own
    ``data_profile_key`` is the file TYPE's profile, empty until a profile
    exists, which is exactly when this step runs. ``key_fields`` likewise come
    from the file's classification, not from its type. Both are what the
    retired profile selector projected (control.data_profile_missing_vw).
    """
    classification = selected.file_facts.get("classification")
    return {
        **selected.template_context(),
        "data_profile_key": selected.file_facts.get("manifest_data_profile_key"),
        "key_fields": (
            classification.get("key_fields") if isinstance(classification, Mapping) else None
        ),
    }


@dataclass(frozen=True)
class ProfilingBatchResult:
    """What one profiling step did, for the handler to report as legacy did.

    ``records_read`` is how many rows the selector returned; ``selected`` how
    many distinct sources they named; ``profiled`` how many were profiled; and
    ``failures`` one ``"<file>: <reason>"`` per source that was not.
    ``applied`` is False for a dry run, which profiles and writes nothing.
    """

    records_read: int
    selected: int
    profiled: int
    failures: tuple[str, ...]
    applied: bool


def run_record_type_profiling(
    ctx: Any, run_log,
    config: Mapping[str, Any],
    *,
    apply: bool = True,
    profiler_version: str = "",
) -> ProfilingBatchResult:
    """Profile every source the configured selection identifies.

    Observational. Each selected file is profiled into one current governed
    profile-library record. It removes no rows and modifies no source files.

    The files to profile are named by the routine behind the declared
    file_selection binding -- one question, one answer, whatever shape of
    record the file arrived through. Each is opened through ManifestSource at
    exactly the mutation the routine returned.

    One file that cannot be profiled is a fact about that file, not a failure
    of the profiler: it is kicked out, recorded against the file, and the batch
    continues. Only an unexpected exception escapes the loop.
    """
    file_selection = config.get("file_selection")
    if file_selection and _value(file_selection, "procedure") is not None:
        raise ProfilingError(
            "Loader workflow process 'profile_csv_record_types' "
            "file_selection.procedure is retired (backlog 619): files are "
            "retrieved by file_selection.operation through the manifest "
            "retrieval routine. Replace 'procedure' with 'operation'."
        )
    operation = _value(file_selection, "operation") if file_selection else None
    source_field = _value(file_selection, "source_field") if file_selection else None
    if not isinstance(operation, str) or not operation.strip():
        raise ProfilingError(
            "Loader workflow process 'profile_csv_record_types' requires "
            "a 'file_selection' mapping naming the operation whose remaining "
            "files it profiles."
        )
    if not isinstance(source_field, str) or not source_field.strip():
        raise ProfilingError(
            "Loader workflow process 'profile_csv_record_types' requires "
            "'file_selection.source_field', naming the field of the selected "
            "row that holds the file to profile."
        )
    source_field = source_field.strip()

    control = getattr(ctx, "shared_control", None)
    if control is None:
        raise ProfilingError(
            "Profiling reads the governed manifest from the control database, "
            "and this context exposes no shared Control to reach it through."
        )
    # One read for the operation's whole set (backlog 619): which files remain
    # to be profiled, at which mutation, is the database's answer.
    try:
        governed = ManifestSource.get_for_operation(
            control, control.installation_id, operation.strip(),
        )
    except (ConfigError, DatabaseError) as exc:
        raise ProfilingError(
            f"Profiling cannot select files for operation {operation!r}: {exc}"
        ) from exc
    by_mutation = {selected.file_mutation_id: selected for selected in governed}

    work: dict[tuple[str, str], tuple[str, int, int, str, tuple[str, ...]]] = {}
    records_read = len(governed)
    _collect_profiling_work(
        [_profiling_record(selected) for selected in governed], source_field, work,
    )

    if not work or not apply:
        return ProfilingBatchResult(
            records_read=records_read, selected=len(work), profiled=0,
            failures=(), applied=apply,
        )

    profiled = 0
    failures: list[str] = []
    for source, object_id, file_manifest_id, data_profile_key, key_fields in work.values():
        try:
            # The selected state, already opened at exactly the mutation the
            # routine returned; profiling reads the file where that state
            # placed it. Hydrated once, by get_for_operation.
            selected = by_mutation[int(object_id)]
            try:
                data_file = selected.data_file()
            except DataStructureError as exc:
                # A state that resolved no path: there is no file to profile.
                raise ProfilingError(
                    f"'{Path(source).name}' cannot be opened for profiling: {exc}"
                ) from exc
            Transform(
                values={
                    "config": config,
                    "data_profile_key": data_profile_key,
                    "key_fields": key_fields,
                    "profiler_version": profiler_version,
                },
                selected="profile",
            ).resolve(ctx).apply(data_file)
            profiled += 1
        except ProfilingError as error:
            # RULE 75: the original is already kicked out -- Transform's common
            # execution path did it before this error reached here (backlog
            # 624). Record the failure; the batch goes on.
            failures.append(f"{Path(source).name}: {error}")
            # Each fact in the field that holds it. ERROR has no typed payload
            # column -- by contract its whole payload is the failure object --
            # so source_path and failure_reason as loose fields were refused and
            # the reason never reached the log at all.
            log_run_record(run_log,
                "ERROR",
                message=f"Profiling failed for '{Path(source).name}': {error}",
                path=str(source),
                error_message={"failure_reason": str(error)},
            )

    return ProfilingBatchResult(
        records_read=records_read, selected=len(work), profiled=profiled,
        failures=tuple(failures), applied=True,
    )


@file_transform("profile", fields=("config", "data_profile_key", "key_fields",
                                  "profiler_version"), required=("config",))
class ProfileTransform(FileTransform):
    """Profile one governed file into the profile library: a Transform kind.

        DataFile -> profile -> DataFile at the profiling mutation

    The profiling operation is ``_store_profile_library_record``, moved behind
    the one Transform rather than run inline by the step (backlog 624). It
    writes one ``record_only`` profiling mutation -- profiling changes nothing
    about the file -- and the same file is returned at that real mutation.
    A file that cannot be profiled raises ProfilingError, and the common
    execution path kicks its original out.
    """

    file_failures = (ProfilingError,)

    def __init__(
        self,
        ctx: Any,
        *,
        config: Any,
        data_profile_key: str = "",
        key_fields: Sequence[str] = (),
        profiler_version: str = "",
    ) -> None:
        """Hold the step's profiling settings and the group this file is in."""
        self._ctx = ctx
        self._config = config
        self._data_profile_key = str(data_profile_key or "")
        self._key_fields = tuple(key_fields or ())
        self._profiler_version = profiler_version

    def _apply(self, data_file: DataFile) -> tuple[DataFile, ...]:
        """Profile the file; return it at the profiling mutation it recorded."""
        mutation_id = _store_profile_library_record(
            self._ctx, bound_run_log(),
            Path(data_file.path),
            self._config,
            data_file.file_manifest_id,
            data_file.file_mutation_id,
            self._data_profile_key,
            self._key_fields,
            profiler_version=self._profiler_version,
        )
        return (
            data_file_for(
                data_file.path, file_type=data_file.file_type,
                **{**data_file.governed_facts(), "file_mutation_id": mutation_id},
                **data_file.settings,
            ),
        )
