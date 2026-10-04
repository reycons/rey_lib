"""The Loader's governed prepared CSV files (row 589, step 6).

Copied from the legacy file_operator ``create_prepared_files`` module, which
stays untouched as the reference, with the two ``record_types`` helpers it
calls (``normalize_row``, ``normalized_header_fields``). What changed, and only
because the boundary required it:

* each kept record opens its selected (sanitized) state through
  ``ManifestSource`` and is prepared through ``Transform(prepare)``:
  ``PrepareTransform.apply`` returns the prepared file (and its redacted
  companion, and the row-kickout files when there are any) as DataFiles at
  their own M17 mutations; ``plan`` is the dry run -- legacy's result with
  ``applied=False``, nothing published, no identity invented;
* a file that cannot be prepared is KICKED OUT first (rule 75,
  ``a_failed_file_is_kicked_out``): its ORIGINAL in processing moves to the
  declared ``file_kickouts`` destination under operation
  ``create_prepared_files`` -- which is also what stops the prepare selector
  offering it again -- and then its failure is recorded (M18) as legacy did.
  The step's own ``kickouts`` stays what it was: the ROW-kickout JSONL;
* the error is ``PreparationError``, and the producer is the runtime context's
  application.

The process selects already-governed sanitized CSV mutation records, resolves
the current profile-library record for each file object, and uses its canonical
header to materialize the prepared CSV. No structure is rediscovered here.

The process selects already-governed sanitized CSV mutation records, resolves
the current profile-library record for each file object, and uses its canonical
header to materialize the prepared CSV. No structure is rediscovered here.

The profile's selected header row is the canonical header. It is consumed once,
normalized to snake_case, and written as the prepared file's header; it is never
kickout content and never a data row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rey_lib.files import (
    FileSetCollisionError,
    FileSetMember,
    publish_file_set,
    redacted_companion_path,
)
from rey_lib.files.csv import (
    CsvReadError,
    normalized_header,
    open_csv,
    parse_delimited_line,
    render_delimited_line,
)
from rey_lib.files.primitive_file_io import render_jsonl_line
from rey_lib.errors.error_utils import ConfigError, DatabaseError
from rey_lib.logs import (
    get_logger,
    log_artifact_reference,
    log_input_file_reference,
)
from rey_lib.redaction.redactors import redact_delimited
from rey_lib.redaction.registry import RedactionRegistry

from rey_lib.data.errors import DataStructureError
from rey_lib.errors.error_utils import AppError
from rey_lib.files.data_file import DataFile, data_file_for
from rey_lib.files.governed_file import FileId
from rey_lib.load.file_transform import FileTransform, file_transform
from rey_lib.load.kickout import kick_out_original
from rey_lib.load.manifest_source import ManifestSource
from rey_lib.load.mutation_context import (
    MutationContextError,
    build_mutation_context,
    log_governed_source_file_mutation,
)
from rey_lib.load.record_templates import record_field, resolve_record_template
from rey_lib.load.redacted_artifacts import redacted_csv_text
from rey_lib.load.transform import Transform
from rey_lib.logs import bound_run_log
from rey_lib.profiling.file_profiler import is_profile_excluded_column
from rey_lib.redaction.registry import RedactionExhausted

__all__ = [
    "CreatePreparedFilesBatchResult",
    "PreparationError",
    "PrepareTransform",
    "run_create_prepared_files",
]

#: The operation every prepared-file record and kickout move states.
_OPERATION = "create_prepared_files"


class PreparationError(AppError):
    """Raised when a governed file cannot be prepared."""


def _application(ctx: Any) -> str:
    """The producing application: the runtime context's, never a literal."""
    return str(getattr(ctx, "app_name", "") or "")

_log = get_logger(__name__)

# Evidence columns lead every kickout row so a reviewer can trace it back to
# the exact source line and the reason it was excluded. Redaction never sees
# them, so they read identically in the original and redacted files.
_SOURCE_LINE_COLUMN = "source_line_number"
_REASON_COLUMN = "kickout_reason"


@dataclass(frozen=True)
class PreparedFileResult:
    """One governed file's prepared outcome."""

    file_id: FileId
    source_path: str
    prepared_path: str
    kickout_path: str | None
    prepared_redacted_path: str | None
    kickout_redacted_path: str | None
    included_rows: int
    excluded_rows: int
    header_mapping: tuple[tuple[str, str], ...]
    applied: bool
    status: str = "success"
    reason: str = ""


@dataclass(frozen=True)
class CreatePreparedFilesBatchResult:
    """Results for the exact governed records selected by one process call."""

    selected: int
    prepared: int
    failed: int
    results: tuple[PreparedFileResult, ...]

    @property
    def status(self) -> str:
        """Report the batch outcome without hiding any per-file failure.

        One unusable profile is that file's failure, not the batch's, so the
        remaining files still publish. The batch only fails when nothing
        succeeded; a configuration error never reaches here because it stops
        the batch before any record is processed.
        """
        if not self.failed:
            return "success"
        if not self.prepared:
            return "failed"
        return "partial_success"


@dataclass(frozen=True)
class _DeclaredFolder:
    """One configured folder template and its local collision authority."""

    path: str
    overwrite: bool
    redacted_copy: bool = False


@dataclass(frozen=True)
class _PreparedConfig:
    operation: str
    source_field: str
    outbox: _DeclaredFolder
    kickouts: _DeclaredFolder
    convert_headers_to: str
    # Where a FILE that cannot be prepared goes (rule 75) -- distinct from
    # ``kickouts``, which is the ROW-kickout JSONL. None leaves it in place.
    file_kickouts: str | None = None


def run_create_prepared_files(
    ctx: Any, run_log,
    inline_config: Mapping[str, Any],
    *,
    apply: bool = True,
) -> CreatePreparedFilesBatchResult:
    """Create one prepared CSV per governed record selected by this process."""
    config = _resolve_config(inline_config)
    work: dict[tuple[str, str], tuple[Mapping[str, Any], ManifestSource]] = {}

    control = getattr(ctx, "shared_control", None)
    if control is None:
        raise PreparationError(
            "The governed file manifest is held in the control database, and "
            "this context exposes no shared Control to select from it."
        )
    # One read for the operation's whole set (backlog 619): which files remain,
    # at which mutation, is the database's answer.
    try:
        governed = ManifestSource.get_for_operation(
            control, control.installation_id, config.operation,
        )
    except (ConfigError, DatabaseError) as exc:
        raise PreparationError(
            f"create_prepared_files cannot select files for operation "
            f"{config.operation!r}: {exc}"
        ) from exc

    for selected in governed:
        # The governed object's own facts, not a selector row.
        record = selected.template_context()
        mutation_context = _mutation_context(record)
        source = _source_path(record, config.source_field)
        identity = (
            mutation_context.file_id,
            str(Path(source).expanduser().resolve()),
        )
        existing = work.get(identity)
        # Later committed evidence for one governed file supersedes earlier.
        if existing is not None and (
            _manifest_record_id(record) <= _manifest_record_id(existing[0])
        ):
            continue
        work[identity] = (record, selected)

    results: list[PreparedFileResult] = []
    for record, selected in work.values():
        # One file's unusable evidence is that file's failure. Publication is
        # already all-or-nothing per file, so a failure here leaves no partial
        # output behind and the remaining selected files still run.
        try:
            data_file = _selected_data_file(selected)
            preparer = Transform(
                values={"config": config, "record": record},
                selected="prepare",
            ).resolve(ctx)
            if apply:
                preparer.apply(data_file)
                results.append(preparer.result)
            else:
                results.append(preparer.plan(data_file))
        except (PreparationError, RedactionExhausted) as error:
            # RULE 75: KICK THE FILE OUT, THEN RECORD THE FAILURE. A dry run
            # writes nothing, so it moves nothing either. A redacted companion
            # that cannot be built (RedactionExhausted, row 613) is this file's
            # failure too: nothing was published, and the batch goes on.
            if apply:
                kick_out_original(
                    ctx,
                    destination=config.file_kickouts,
                    file_manifest_id=int(record.get("file_manifest_id") or 0),
                    source=Path(_optional_text(
                        record_field(record, config.source_field)) or "unknown"),
                    operation=_OPERATION,
                )
            results.append(
                _failed_result(ctx, record, error,
                               source_field=config.source_field,
                               apply=apply)
            )

    return CreatePreparedFilesBatchResult(
        selected=len(work),
        prepared=sum(result.applied for result in results),
        failed=sum(result.status == "failed" for result in results),
        results=tuple(results),
    )


def _prepare_one_file(
    ctx: Any, run_log,
    config: _PreparedConfig,
    record: Mapping[str, Any],
    *,
    apply: bool,
) -> tuple[PreparedFileResult, tuple[tuple[str, int], ...]]:
    """Materialize one governed file's prepared and kickout artifacts.

    Returns:
        The result, and each published artifact's path with its M17 mutation
        (none on a dry run, which publishes nothing).
    """
    mutation_context = _mutation_context(record)
    source = _source_path(record, config.source_field)
    consumed_record_id = _manifest_record_id(record)
    profile = _current_profile_structure(ctx, record)

    # The source is read FIRST because the stored header is text now, and
    # turning text back into ordered columns needs the delimiter the source
    # declares. _read_source is what answers that.
    delimiter, rows = _read_source(source, record)
    canonical_header = _profile_header(profile, consumed_record_id, delimiter)

    # Header phase: the first match is the canonical header and every match is
    # removed. A repeated header is neither data nor a kickout.
    header_fields, remaining = _partition_headers(
        rows, canonical_header, source
    )
    normalized = normalized_header(header_fields)
    _require_unique_headers(header_fields, normalized, source)
    header_mapping = tuple(zip(header_fields, normalized))

    data_rows = remaining
    kickout_rows: list[tuple[int, str, list[str]]] = []
    included_lines = [
        render_delimited_line(fields, delimiter) for _, _, fields in data_rows
    ]

    prepared_path = _resolved_template(config.outbox.path, record, source, "outbox")
    kickout_path = (
        _resolved_template(config.kickouts.path, record, source, "kickouts")
        if kickout_rows
        else None
    )
    # The prepared file's redacted companion carries the same rows and columns
    # with only its values replaced, so a prepared file can be reviewed without
    # its contents.
    prepared_redacted_path = (
        str(redacted_companion_path(prepared_path))
        if config.outbox.redacted_copy
        else None
    )
    kickout_redacted_path = (
        str(redacted_companion_path(kickout_path))
        if kickout_rows and config.kickouts.redacted_copy
        else None
    )

    prepared_text = "".join(
        line + "\n"
        for line in [render_delimited_line(normalized, delimiter), *included_lines]
    )
    # The publication primitive owns companion naming and atomicity; each
    # producer supplies only what a companion contains.
    members = [FileSetMember(
        destination=Path(prepared_path),
        text=prepared_text,
        redacted_text=(
            redacted_csv_text(
                normalized, [fields for _, _, fields in data_rows], delimiter
            )
            if config.outbox.redacted_copy
            else None
        ),
    )]
    if kickout_rows:
        columns = _kickout_columns(kickout_rows, normalized)
        members.append(FileSetMember(
            destination=Path(kickout_path),
            text=_kickout_text(kickout_rows),
            redacted_text=(
                _kickout_text(_redacted_kickout_rows(kickout_rows, columns))
                if config.kickouts.redacted_copy
                else None
            ),
        ))

    result = PreparedFileResult(
        file_id=mutation_context.file_id,
        source_path=source,
        prepared_path=prepared_path,
        kickout_path=kickout_path,
        prepared_redacted_path=prepared_redacted_path,
        kickout_redacted_path=kickout_redacted_path,
        included_rows=len(included_lines),
        excluded_rows=len(kickout_rows),
        header_mapping=header_mapping,
        applied=False,
    )
    if not apply:
        return result, ()

    producing_step = str(getattr(ctx, "pipeline_step_name", "") or "")
    log_input_file_reference(run_log, str(source), file_role="prepared_file_input",
        consumed_by_step=producing_step, producing_app=_application(ctx),
        status="consumed", safe_to_preview=True, viewer_type="file",
        file_id=mutation_context.file_id,
    )

    # Collision policy is set-scoped, so publication may only replace when
    # every folder contributing a member authorizes it.
    overwrite = config.outbox.overwrite and (
        not kickout_rows or config.kickouts.overwrite
    )
    try:
        publish_file_set(
            members, on_collision="replace" if overwrite else "fail"
        )
    except FileSetCollisionError as exc:
        raise PreparationError(
            f"Prepared output for '{source}' already exists and its folder does "
            f"not authorize replacement: {exc}"
        ) from exc
    except OSError as exc:
        raise PreparationError(
            f"Prepared output for '{source}' could not be published: {exc}"
        ) from exc

    produced = _record_evidence(ctx, run_log, mutation_context, result, producing_step,
                                record, header_mapping)
    return PreparedFileResult(**{**vars(result), "applied": True}), produced


def _failed_result(
    ctx: Any,
    record: Mapping[str, Any],
    error: PreparationError | RedactionExhausted,
    *,
    source_field: str,
    apply: bool,
) -> PreparedFileResult:
    """Record one file's failure as governed evidence and as an item result.

    The paths are reported best effort: a record can fail before either is
    resolvable, and an unreportable path must not replace the real error.
    """
    source = _optional_text(record_field(record, source_field))
    file_id = _optional_text(record.get("file_manifest_id"))
    # The failure itself goes through the logger with its error, which is what
    # writes the run's ERROR record.
    _log.error("Prepared file for '%s' could not be created: %s", source, error,
               exc_info=error)
    if apply:
        try:
            log_governed_source_file_mutation(
                ctx,
                _mutation_context(record),
                action="create",
                status="failed",
                source_path=source,
                application_name=_application(ctx),
                operation=_OPERATION,
                # The outcome of a failed preparation is that nothing was
                # produced; why is the error, and it belongs in the message.
                # Same result as a success; status distinguishes them.
                reason="prepared_file",
                message=str(error),
                run_log_fields={
                    "source_record_id": record.get("file_mutation_id"),
                },
            )
        except (MutationContextError, PreparationError):
            # Evidence for an unbindable record cannot be written; the item
            # result below still reports the original failure.
            pass
    return PreparedFileResult(
        file_id=file_id,
        source_path=source,
        prepared_path="",
        kickout_path=None,
        prepared_redacted_path=None,
        kickout_redacted_path=None,
        included_rows=0,
        excluded_rows=0,
        header_mapping=(),
        applied=False,
        status="failed",
        reason=str(error),
    )


def _optional_text(value: Any) -> str:
    """Return one reportable string, or empty when the value is unusable."""
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _record_evidence(
    ctx: Any, run_log,
    mutation_context: Any,
    result: PreparedFileResult,
    producing_step: str,
    record: Mapping[str, Any],
    header_mapping: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, int], ...]:
    """Record the created artifacts through the existing governed boundary.

    Returns:
        Each artifact's path and the M17 mutation that recorded it.
    """
    mapping_payload = {original: new for original, new in header_mapping}
    produced: list[tuple[str, int]] = []
    for path, role in (
        (result.prepared_path, "prepared_file"),
        (result.prepared_redacted_path, "redacted_prepared_file"),
        (result.kickout_path, "kickout_file"),
        (result.kickout_redacted_path, "redacted_kickout_file"),
    ):
        if path is None:
            continue
        mutation = log_governed_source_file_mutation(
            ctx,
            mutation_context,
            action="create",
            status="success",
            destination_path=path,
            application_name=_application(ctx),
            operation=_OPERATION,
            reason=role,
            run_log_fields={
                "source_record_id": record.get("file_mutation_id"),
                "included_row_count": result.included_rows,
                "excluded_row_count": result.excluded_rows,
            },
        )
        log_artifact_reference(run_log, str(path), role=role, event="created",
            created_by_step=producing_step, producer=_application(ctx),
            artifact_type=role, source_path=str(result.source_path),
            artifact_group="output_files", producing_app=_application(ctx),
            producing_step=producing_step, viewer_type="file",
            safe_to_preview=True, file_id=result.file_id,
            included_row_count=result.included_rows,
            excluded_row_count=result.excluded_rows,
            header_mapping=mapping_payload,
        )
        produced.append((str(path), mutation))
    return tuple(produced)


# ---------------------------------------------------------------------------
# Source and profile reading
# ---------------------------------------------------------------------------


def _read_source(
    source: str,
    record: Mapping[str, Any],
) -> tuple[str, list[tuple[int, str, list[str]]]]:
    """Return the source's delimiter and every physical row, read once.

    ``include_all_rows`` streams the preamble, the header row itself, and every
    row after it exactly once, each carrying its physical line number — the same
    coordinate space the profile's ranges use, so no translation is needed.
    """
    try:
        stream = open_csv(source, include_all_rows=True)
        # The normalized text travels with each row: a signature was generated
        # against the normalized source line, so that is what it must be
        # matched against. Fields are left exactly as read, because they are
        # what gets written to the prepared file.
        rows = [
            (
                row.physical_line_number,
                normalize_row(row.text, stream.delimiter),
                list(row.fields),
            )
            for row in stream.rows
        ]
    except (CsvReadError, OSError) as exc:
        raise PreparationError(
            f"Selected manifest record {record.get('record_id')!r} source "
            f"'{source}' could not be read: {exc}"
        ) from exc
    return stream.delimiter, rows


def _current_profile_structure(
    ctx: Any,
    record: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Resolve and validate the current governed profile for one FILE.

    THE PROFILE IS REACHED FROM THE FILE, through the link profiling stamped:

        file_manifest.file_type_id -> file_type.data_profile_id -> data_profile

    which is what control.f_file_profile_get walks. NOT reached through the
    consumed mutation: a profile describes the file's STRUCTURE, not one event
    in its history, so keyed on the mutation a file that is sanitized again
    would lose a profile that still describes it perfectly.

    THE CLEAR REPRESENTATION, and only it. Redacted is the representation
    model-facing callers are served; internal work reads clear. Asking for both
    made this function hold a document it never read.
    """
    control = getattr(ctx, "shared_control", None)
    if control is None:
        raise PreparationError(
            "Profiles are held in the control database, and this context "
            "exposes no shared Control to read them through."
        )
    file_manifest_id = record.get("file_manifest_id")
    try:
        clear = control.file_profile(file_manifest_id, "clear")
    except (OSError, ConfigError, DatabaseError) as exc:
        raise PreparationError(
            f"Selected manifest record {record.get('record_id')!r} could not "
            f"resolve its governed profile: {exc}"
        ) from exc

    if clear is None:
        raise PreparationError(
            f"Governed file {file_manifest_id!r} requires a current clear "
            "profile and has none. A file reaches its profile through the "
            "file_type_id its manifest carries, so a file that was never "
            "typed, or whose profile has no field rows, has no structure to "
            "prepare from."
        )
    clear = clear.get("profile")
    if not isinstance(clear, Mapping):
        raise PreparationError(
            f"Governed file {file_manifest_id!r} has an invalid profile: "
            "the clear representation must be a JSON object."
        )
    if clear.get("profile_schema_version") != 1:
        raise PreparationError(
            f"Governed file {file_manifest_id!r} has an invalid profile: "
            "profile_schema_version must be 1."
        )
    # Nothing here compares the file on disk against a hash the profile records.
    # A profile is shared by every file of its structure, so the one file it was
    # built from is not a currency test for any of the others.
    structure = dict(clear)
    if not isinstance(structure.get("header_definition"), str):
        raise PreparationError(
            f"Governed file {file_manifest_id!r} has an invalid profile: "
            "header_definition is not a header line."
        )
    if not isinstance(structure.get("distribution"), Mapping):
        raise PreparationError(
            f"Governed file {file_manifest_id!r} has an invalid profile: "
            "distribution is not a JSON object."
        )
    for field in ("columns", "samples"):
        if not isinstance(structure.get(field), list):
            raise PreparationError(
                f"Governed file {file_manifest_id!r} has an invalid "
                f"profile: {field} is not a JSON array."
            )
    return structure


def _require_unique_headers(
    original: Sequence[str],
    normalized: Sequence[str],
    source: str,
) -> None:
    """Fail closed when two distinct headers collapse to one snake_case name."""
    seen: dict[str, str] = {}
    for source_name, new_name in zip(original, normalized):
        if new_name in seen:
            raise PreparationError(
                f"Source '{source}' headers {seen[new_name]!r} and "
                f"{source_name!r} both normalize to {new_name!r}."
            )
        seen[new_name] = source_name


def _profile_header(
    profile: Mapping[str, Any],
    consumed_record_id: int,
    delimiter: str,
) -> list[str]:
    """Return the governed canonical header's ordered columns.

    The profile stores the header as TEXT -- the line the file carried -- so
    the ordered columns are parsed back out of it with the source's own
    delimiter. Storing the line rather than a column list is what makes the
    profile's natural key the header itself.
    """
    header = profile.get("header_definition")
    if not isinstance(header, str) or not header.strip():
        raise PreparationError(
            f"Profile of consumed record '{consumed_record_id}' declares no "
            "usable canonical header."
        )
    try:
        columns = parse_delimited_line(header, delimiter)
    except CsvReadError as exc:
        raise PreparationError(
            f"Profile of consumed record '{consumed_record_id}' has a header "
            f"that cannot be parsed with delimiter {delimiter!r}: {exc}"
        ) from exc
    if not columns:
        raise PreparationError(
            f"Profile of consumed record '{consumed_record_id}' has an "
            "approved header carrying no columns."
        )
    return columns


def _partition_headers(
    rows: Sequence[tuple[int, str, list[str]]],
    header: Sequence[str],
    source: str,
) -> tuple[list[str], list[tuple[int, str, list[str]]]]:
    """Remove every header row, keeping the first as the canonical header.

    The profile's declared columns are authoritative: they are what a consumer
    writes and the parsed identity used to remove every repeated header. They
    arrive already parsed out of the stored header text by _profile_header.
    """
    declared = header
    identity = (
        normalized_header_fields(declared)
        if isinstance(declared, (list, tuple))
        else ()
    )
    remaining: list[tuple[int, str, list[str]]] = []
    matched = False
    has_source_line_column = False
    for _line_number, _text, fields in rows:
        # The prepended source-line cell is always FIRST, and it carries its
        # name only on physical line 1 -- below a preamble the header row
        # carries the NUMBER instead, which is why stripping by name alone
        # left it in. The row's own physical_line_number is that number:
        # sanitization only prepends to each record, so output line N is
        # source line N.
        source_line_cell = bool(fields) and is_profile_excluded_column(
            fields[0], _line_number)
        comparison_fields = [
            field for index, field in enumerate(fields)
            if not (index == 0 and source_line_cell)
            and field.strip().casefold() != _SOURCE_LINE_COLUMN
        ]
        if identity and normalized_header_fields(comparison_fields) == identity:
            matched = True
            has_source_line_column = (
                has_source_line_column
                or source_line_cell
                or any(field.strip().casefold() == _SOURCE_LINE_COLUMN
                       for field in fields)
            )
            continue
        remaining.append((_line_number, _text, fields))
    if not matched:
        raise PreparationError(
            f"Source '{source}' contains no row matching the approved header "
            "definition, so it has no canonical header."
        )
    if not isinstance(declared, (list, tuple)) or not declared:
        raise PreparationError(
            f"Source '{source}' has an approved header carrying no columns."
        )
    canonical = [str(value) for value in declared]
    if has_source_line_column:
        canonical.insert(0, _SOURCE_LINE_COLUMN)
    return canonical, remaining


def _kickout_columns(
    kickout_rows: Sequence[tuple[int, str, list[str]]],
    normalized: Sequence[str],
) -> list[str]:
    """Return the source column names a kicked-out row's fields map onto.

    A kicked-out row may carry more fields than the canonical header has
    columns — that width is often why it was excluded. Overflow positions are
    named the way the profiler already names unnamed columns, so every value
    has a name to be redacted under and none is dropped.
    """
    widest = max((len(fields) for _, _, fields in kickout_rows), default=0)
    columns = list(normalized)
    for index in range(len(normalized), widest):
        columns.append(f"column_{index + 1}")
    return columns


def _kickout_text(kickout_rows: Sequence[tuple[int, str, list[str]]]) -> str:
    """Render one kickout file as JSONL, one excluded row per line.

    Evidence is kept out of the record itself: the line number and reason are
    their own fields, and ``fields`` holds the source row exactly as it was
    read. A CSV would have had to interleave the two, which both widens the
    original record and misaligns any row whose width is why it was excluded.
    """
    return "".join(
        render_jsonl_line({
            _SOURCE_LINE_COLUMN: line_number,
            _REASON_COLUMN: reason,
            "fields": list(fields),
        }) + "\n"
        for line_number, reason, fields in kickout_rows
    )


def _redacted_kickout_rows(
    kickout_rows: Sequence[tuple[int, str, list[str]]],
    columns: Sequence[str],
) -> list[tuple[int, str, list[str]]]:
    """Return the same rows with only their source values redacted.

    Redaction runs through the shared governed boundary with the source
    columns registered and nothing else, so the evidence columns are never
    reachable by it and no redaction rule is decided here.
    """
    source_columns = [
        column for column in columns
        if column not in {_SOURCE_LINE_COLUMN, _REASON_COLUMN}
    ]
    registry = RedactionRegistry(source_columns)
    rows = [
        {
            column: fields[index] if index < len(fields) else ""
            for index, column in enumerate(source_columns)
        }
        for _, _, fields in kickout_rows
    ]
    redacted = redact_delimited(rows, source_columns, registry)
    return [
        (
            line_number,
            reason,
            [redacted[position][column] for column in source_columns[:len(fields)]],
        )
        for position, (line_number, reason, fields) in enumerate(kickout_rows)
    ]


def _resolve_config(inline_config: Mapping[str, Any]) -> _PreparedConfig:
    """Resolve one inline process mapping into its governed configuration."""
    if not _is_mapping_like(inline_config):
        raise PreparationError(
            "create_prepared_files requires an inline process configuration mapping."
        )
    config = _plain_mapping(inline_config)
    if "config_ref" in config:
        raise PreparationError(
            "create_prepared_files config_ref is unsupported; supply inline "
            "workflow process configuration."
        )
    if "overwrite" in config:
        raise PreparationError(
            "create_prepared_files step-level 'overwrite' is unsupported; declare "
            "it on the folder that owns the collision policy."
        )
    if "selections" in config:
        raise PreparationError(
            "create_prepared_files declares no selection of its own; the "
            "manifest retrieval routine names the files that still need "
            "preparing for file_selection.operation. Remove 'selections'."
        )
    file_selection = config.get("file_selection")
    if isinstance(file_selection, Mapping) and "procedure" in file_selection:
        raise PreparationError(
            "create_prepared_files' file_selection.procedure is retired (backlog "
            "619): files are retrieved by file_selection.operation through the "
            "manifest retrieval routine. Replace 'procedure' with 'operation'."
        )
    operation = (file_selection or {}).get("operation") if isinstance(
        file_selection, Mapping) else None
    source_field = (file_selection or {}).get("source_field") if isinstance(
        file_selection, Mapping) else None
    if not isinstance(operation, str) or not operation.strip():
        raise PreparationError(
            "create_prepared_files requires a 'file_selection' mapping naming "
            "the operation whose remaining files it prepares."
        )
    if not isinstance(source_field, str) or not source_field.strip():
        raise PreparationError(
            "create_prepared_files requires 'file_selection.source_field', "
            "naming the field of the selected row that holds the file."
        )

    preparation = config.get("preparation")
    if not isinstance(preparation, Mapping):
        raise PreparationError("create_prepared_files requires a 'preparation' mapping.")
    headers = preparation.get("headers")
    if not isinstance(headers, Mapping):
        raise PreparationError(
            "create_prepared_files requires 'preparation.headers'."
        )
    convert_to = _required_text(headers, "convert_to", "preparation.headers")
    if convert_to != "snake_case":
        raise PreparationError(
            f"create_prepared_files header conversion {convert_to!r} is "
            "unsupported; the prepared header is snake_case."
        )

    return _PreparedConfig(
        operation=operation.strip(),
        source_field=source_field.strip(),
        outbox=_required_folder(config, "outbox"),
        kickouts=_required_folder(config, "kickouts"),
        convert_headers_to=convert_to,
        file_kickouts=(
            _required_folder(config, "file_kickouts").path
            if config.get("file_kickouts") is not None
            else None
        ),
    )


def _required_folder(mapping: Mapping[str, Any], field: str) -> _DeclaredFolder:
    """Return one declared folder with folder-local overwrite authority."""
    declared = mapping.get(field)
    if not isinstance(declared, Mapping):
        raise PreparationError(
            f"create_prepared_files requires inline {field!r} as a folder mapping."
        )
    unknown = sorted(set(declared) - {"path", "overwrite", "redacted_copy"})
    if unknown:
        raise PreparationError(
            f"create_prepared_files folder {field!r} contains unknown fields: "
            f"{', '.join(unknown)}."
        )
    overwrite = declared.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise PreparationError(
            f"create_prepared_files folder {field!r} 'overwrite' must be true or false."
        )
    redacted_copy = declared.get("redacted_copy", False)
    if not isinstance(redacted_copy, bool):
        raise PreparationError(
            f"create_prepared_files folder {field!r} 'redacted_copy' must be "
            "true or false."
        )
    return _DeclaredFolder(
        path=_required_text(declared, "path", f"folder {field!r}"),
        overwrite=overwrite,
        redacted_copy=redacted_copy,
    )


def _required_text(mapping: Mapping[str, Any], field: str, owner: str) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value.strip():
        raise PreparationError(
            f"create_prepared_files {owner} requires a non-empty {field!r}."
        )
    return value.strip()


# ---------------------------------------------------------------------------
# Record helpers
# ---------------------------------------------------------------------------


def _source_path(record: Mapping[str, Any], source_field: str) -> str:
    value = record_field(record, source_field)
    if not isinstance(value, str) or not value.strip():
        raise PreparationError(
            f"Selected row {record.get('file_manifest_id')!r} has no "
            f"non-empty {source_field!r} to prepare."
        )
    return value


def _resolved_template(
    template: str,
    record: Mapping[str, Any],
    source: str,
    owner: str,
) -> str:
    """Resolve one configured template against the selected governed record."""
    resolved = resolve_record_template(template, record)
    if not resolved.resolved:
        raise PreparationError(
            f"create_prepared_files {owner} for source '{source}' could not be "
            f"resolved; template field {resolved.missing_field!r} is missing or empty."
        )
    return str(resolved.path)


def _manifest_record_id(record: Mapping[str, Any]) -> int:
    """The selected row's own identity, used to dedup and order rows.

    ``file_mutation_id`` is the column the selector returns. ``record_id`` was
    the JSONL manifest's line identity and no longer exists on a row.
    """
    value = record.get("file_mutation_id")
    if not isinstance(value, int) or isinstance(value, bool):
        raise PreparationError(
            f"Selected row {value!r} has no integer file_mutation_id."
        )
    return value


def _mutation_context(record: Mapping[str, Any]) -> Any:
    """Bind one selected row to the governed identity it names.

    The row is a row and the context is an identity: ``MutationContext`` is
    producer-neutral and does not know a selector exists, so the translation
    between ``file_manifest_id`` and ``file_id`` happens here, in the step that
    knows both -- as ``excel_conversion`` already does.
    """
    try:
        return build_mutation_context({
            "file_id": record.get("file_manifest_id"),
            "classification": record.get("classification"),
            # The mutation this step consumed, as the selector returned it.
            "source_record_id": record.get("file_mutation_id"),
        })
    except MutationContextError as exc:
        raise PreparationError(
            f"Selected manifest record {record.get('record_id')!r} cannot bind "
            f"governed prepared-file identity: {exc}"
        ) from exc


def _is_mapping_like(value: Any) -> bool:
    """Return whether config loading exposes mapping keys for ``value``."""
    return isinstance(value, Mapping) or callable(getattr(value, "keys", None))


def _plain_mapping(value: Any) -> dict[str, Any]:
    """Convert one loaded Mapping/Namespace tree without changing its values."""
    if not _is_mapping_like(value):
        raise ConfigError("Expected a mapping-like configuration value.")
    return {str(key): _plain_config_value(value.get(key)) for key in value.keys()}


def _plain_config_value(value: Any) -> Any:
    if _is_mapping_like(value):
        return _plain_mapping(value)
    if isinstance(value, list):
        return [_plain_config_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_plain_config_value(item) for item in value)
    return value


# ---------------------------------------------------------------------------
# The Loader boundary (row 589, step 6)
# ---------------------------------------------------------------------------


def _selected_data_file(selected: ManifestSource) -> DataFile:
    """Open exactly the selected file's governed state as a DataFile.

    Raises:
        PreparationError: If its state cannot be opened (no registered DataFile
            type, no resolved path) -- a per-file failure, kicked out and
            recorded like any other.
    """
    try:
        return selected.data_file()
    except (ConfigError, DataStructureError) as exc:
        raise PreparationError(
            f"Selected mutation {selected.file_mutation_id!r} cannot be opened "
            f"for preparation: {exc}"
        ) from exc


@file_transform("prepare", fields=("config", "record"))
class PrepareTransform(FileTransform):
    """Prepare one governed sanitized file.

        DataFile (sanitized) -> prepare -> prepared DataFile
                                           [+ redacted companion]
                                           [+ row kickouts (+ redacted)]

    ``config`` is the step's validated preparation. ``record`` is the selected
    row: the templates, the source field and the governed identity read it
    exactly as legacy did, and it is never a DataFile property.
    """

    def __init__(self, ctx: Any, *, config: _PreparedConfig, record: Any) -> None:
        """Keep the validated preparation and the row it prepares.

        Raises:
            PreparationError: If either is not what the step produces.
        """
        if not isinstance(config, _PreparedConfig):
            raise PreparationError(
                "Transform (prepare) requires the step's validated preparation."
            )
        if not _is_mapping_like(record):
            raise PreparationError(
                "Transform (prepare) requires the selected row as a mapping."
            )
        self._ctx = ctx
        self._config = config
        self._record = _plain_mapping(record)
        self._result: PreparedFileResult | None = None

    @property
    def result(self) -> PreparedFileResult | None:
        """What the last ``apply`` or ``plan`` did, as preparation reported it."""
        return self._result

    def apply(self, data_file: DataFile) -> tuple[DataFile, ...]:
        """Prepare and publish the file, recording each artifact.

        Returns:
            Each published artifact as a DataFile at its own M17 mutation.
        """
        result, produced = _prepare_one_file(
            self._ctx, bound_run_log(), self._config, self._record, apply=True)
        self._result = result
        return tuple(
            data_file_for(
                Path(path),
                # The prepared files are CSV; the row kickouts are JSONL. Each
                # path's own suffix says which, as the producer named it.
                file_manifest_id=data_file.file_manifest_id,
                file_mutation_id=mutation,
                classification=data_file.classification,
                base_path=data_file.base_path,
            )
            for path, mutation in produced
        )

    def plan(self, data_file: DataFile) -> PreparedFileResult:
        """Prepare as a dry run: nothing is published or recorded.

        Returns:
            Preparation's own result, ``applied`` False. No governed DataFile,
            because no mutation exists to identify one.
        """
        result, _produced = _prepare_one_file(
            self._ctx, bound_run_log(), self._config, self._record, apply=False)
        self._result = result
        return result


# ---------------------------------------------------------------------------
# Copied from the legacy file_operator ``record_types`` module: the two row
# and header helpers preparation calls (row 589, step 6).
# ---------------------------------------------------------------------------


def normalize_row(text: str, separator: str) -> str:
    """Return one physical row with its trailing padding removed.

    Trailing whitespace and trailing delimiters carry no information: they are
    how a writer terminated the line, not part of what the row says. They are
    removed in turn until the row ends with neither, so a row padded out to a
    fixed width reduces to the same text as the row that was not.

    ``separator`` is required and has no default, because the delimiter is
    whatever the CSV reader resolved for this file — supplied or detected — and
    a default here would be this module quietly deciding it is a comma.

    Leading delimiters are left alone — they position a value in a column and
    are meaningful — as is every delimiter and space inside the row. A row that
    is nothing but padding reduces to the empty string, which is what makes a
    blank row blank.

    This is the shared boundary: profiling proposes signatures against
    normalized rows and preparation matches them against normalized rows, so
    generation and consumption cannot drift.
    """
    previous: str | None = None
    while previous != text:
        previous = text
        text = text.rstrip()
        if separator and text.endswith(separator):
            text = text[: -len(separator)]
    return text


def normalized_header_fields(fields: Sequence[str]) -> tuple[str, ...]:
    """Return the header's identity: its column names and nothing else.

    A header is the same header whether or not the writer padded it with
    spaces, quoted it, or left a trailing delimiter. Those are artifacts of how
    a row was written, not differences in what it says, so identity is the
    stripped, unquoted field values with trailing empties dropped.

    Quotes are removed here as well as by the CSV reader, because a reader only
    resolves the quoting it recognises: a field written with the delimiter
    outside the quotes, or quoted a second time by an exporting tool, arrives
    with its quote characters intact.
    """
    stripped: list[str] = []
    for value in fields:
        text = str(value).strip()
        while len(text) > 1 and text[0] == text[-1] and text[0] in "\"'":
            text = text[1:-1].strip()
        stripped.append(text)
    while stripped and not stripped[-1]:
        stripped.pop()
    return tuple(stripped)
