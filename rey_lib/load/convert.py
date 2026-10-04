"""The Loader's named workbook-to-CSV conversions (row 589, step 3).

Copied from the legacy file_operator ``excel_conversion`` module, which stays
untouched as the reference. The boundary legacy draws is kept exactly:

    selected row -> _resolve_candidate (once, in selection) -> ConversionCandidate
    ConversionCandidate
      -> ManifestSource(candidate.mutation_context.source_record_id) -> DataFile
      -> Transform(convert, candidate) -> ConvertTransform.apply()
      -> the converted CSVs, each a DataFile at its own M10 mutation

The candidate carries the validated source path, destinations and mutation
context, so nothing is resolved twice. A dry run stops after selection and its
evidence, as legacy's does, and never reaches the transform. What changed from
the legacy module, and only because the boundary required it: the per-workbook
work is ``ConvertTransform``, reached through ``Transform``; the error is
``ConversionError``; and the producer is the runtime context's application
rather than a literal.

This module owns the application-specific boundary around the neutral
``rey_lib.files.convert_workbook_to_csv`` primitive:

* validate one inline conversion from its workflow process configuration;
* select conversion candidates from classified source-file lifecycle records;
* consume each classification record's governed physical path;
* retain the legacy conversion-owned processing claim when configured;
* emit converted CSV artifacts with source lineage; and
* dispose source workbooks to archive or kickouts.

Candidate selection is manifest-driven. This module never enumerates an inbox,
never infers eligibility from a filename, and never reconstructs a source path
from classification values. New classification records carry the processing
path claimed by ``classify_source_files``; the linked inventory record remains
the identity and lineage authority.

It does not read YAML or participate in pipeline/workflow orchestration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from rey_lib.errors.error_utils import AppError, ConfigError
from rey_lib.files import (
    WorkbookConversionResult,
    convert_workbook_to_csv,
    is_supported_workbook,
    LogRunRollbackError,
    move_file,
)
from rey_lib.files.data_file import DataFile, data_file_for
from rey_lib.logs import (
    bound_run_log,
    get_logger,
    log_artifact_reference,
    log_input_discovered,
    log_row_count,
    log_validation_result,
)

from rey_lib.files.governed_file import is_governed_file_id
from rey_lib.load.file_transform import FileTransform, file_transform
from rey_lib.load.manifest_source import ManifestSource
from rey_lib.load.mutation_context import (
    MutationContext,
    build_mutation_context,
    log_governed_source_file_mutation,
)
from rey_lib.load.record_templates import record_field, resolve_record_template
from rey_lib.load.transform import Transform

__all__ = [
    "ConversionCandidate",
    "ConversionDestinations",
    "ConversionError",
    "ConvertTransform",
    "ExcelConversionValues",
    "ExcelConversionConfig",
    "RejectedCandidate",
    "SourceSelection",
    "resolve_excel_conversion_config",
    "run_excel_conversion",
    "select_conversion_candidates",
]


class ConversionError(AppError):
    """Raised when a governed workbook conversion cannot proceed."""

_CLASSIFIED_STATUS = "classified"
_MISSING_SOURCE_REASONS = frozenset(
    {"missing_classification_path", "missing_inventory_path", "missing_source_file"}
)

# What this producer declares about the files it creates. Both are written by
# the producer; neither is inferred downstream from a path or extension. The
# application is the runtime context's (see _application).
# Identifies which producer wrote the conversion payload.
_OPERATOR_NAME = "excel_conversion"
_MUTATION_RECORD_TYPE = "source_file_mutation"
_TEMPLATE_FIELD = re.compile(r"<([^<>]*)>")
_log = get_logger(__name__)


@dataclass(frozen=True)
class ConversionCandidate:
    """One classified record resolved to its governed current path.

    ``values`` is the complete classification values mapping, carried through
    generically. This module attaches no meaning to any individual variable
    name — those are installation-defined and live only in configuration.
    """

    classification_record_id: int
    mutation_context: MutationContext
    classification_type: str
    inventory_record_id: int
    source_path: Path
    values: Mapping[str, Any]
    destinations: ConversionDestinations
    #: The governed file this candidate was resolved from, opened once by
    #: ManifestSource.get_for_operation (backlog 619). Its workbook is opened
    #: from it; no second read of the file's context is made.
    governed: Any = field(default=None, compare=False, repr=False)

    @property
    def file_id(self) -> str:
        """Return the governed identity retained in canonical mutation context."""
        return self.mutation_context.file_id


@dataclass(frozen=True)
class RejectedCandidate:
    """One matched record that will not be converted, with its stable reason."""

    classification_record_id: int | None
    reason_code: str
    reason: str


@dataclass(frozen=True)
class SourceSelection:
    """Resolved conversion inputs plus the records deliberately not converted."""

    candidates: tuple[ConversionCandidate, ...]
    rejected: tuple[RejectedCandidate, ...]
    matched: int


@dataclass(frozen=True)
class ExcelConversionConfig:
    """Validated File Operator configuration for one named conversion.

    Destination templates may carry ``<field.path>`` placeholders resolved once
    per candidate against that candidate's classification record. A template
    without a placeholder is a fixed path and behaves exactly as before.
    """

    name: str
    enabled: bool
    operation: str
    source_field: str
    processing: str | None
    outbox: str
    kickouts: str
    archive: str | None
    folder_overwrite: Mapping[str, bool]
    include_hidden_sheets: bool
    include_empty_sheets: bool


@dataclass(frozen=True)
class ExcelConversionValues:
    """What the Excel conversion knows about the source it read.

    This is File Operator vocabulary, owned end to end by this module. It
    builds the conversion payload the manifest record carries, so a new
    producer can describe its own conversion without the framework learning
    anything about it.
    """

    name: str
    sheet_name: str = ""
    table_name: str = ""
    range_name: str = ""

    def payload(self) -> dict[str, Any]:
        """Return this conversion's payload, identified by its operator."""
        source = {
            field: value
            for field, value in (
                ("sheet_name", self.sheet_name),
                ("table_name", self.table_name),
                ("range_name", self.range_name),
            )
            if value
        }
        payload: dict[str, Any] = {
            "operator": _OPERATOR_NAME,
            "name": self.name,
        }
        if source:
            payload["source"] = source
        return payload


@dataclass(frozen=True)
class ConversionDestinations:
    """One candidate's resolved destination paths."""

    processing: Path | None
    outbox: Path
    kickouts: Path
    archive: Path | None
    folder_overwrite: Mapping[str, bool]


def resolve_excel_conversion_config(
    inline_config: Mapping[str, Any],
) -> ExcelConversionConfig:
    """Validate and project the canonical process-local configuration."""

    if not isinstance(inline_config, Mapping):
        if isinstance(inline_config, str):
            raise ConversionError(
                "Excel conversion config_ref is unsupported; supply the inline "
                "workflow process configuration."
            )
        raise ConversionError(
            "Excel conversion requires an inline process configuration mapping."
        )
    if "config_ref" in inline_config:
        raise ConversionError(
            "Excel conversion config_ref is unsupported; supply the inline "
            "workflow process configuration."
        )
    return _resolve_inline_excel_conversion_config(inline_config)


def select_conversion_candidates(
    ctx: Any, run_log,
    config: ExcelConversionConfig,
) -> SourceSelection:
    """Ask the control database for the workbooks this conversion may claim.

    The selection is relational and the database owns it: which governed files
    are classified workbooks, and where each one currently is. A file's current
    location is its latest surviving mutation, and resolving that is exactly
    what a mutation history is for -- so this module does not walk lineage,
    join inventory records, or reconstruct a path.

    What remains here is what selection deliberately excludes: proving the
    resolved file is still on disk and is a convertible workbook, and refusing
    a duplicate claim. A classified non-workbook is skipped exactly as inbox
    discovery skipped it, and one malformed row never stops another file
    converting.
    """

    control = getattr(ctx, "shared_control", None)
    if control is None:
        raise ConversionError(
            f"Excel conversion '{config.name}' reads the governed manifest "
            "from the control database, and this context exposes no shared "
            "Control to reach it through."
        )

    # Which workbooks are this step's work -- extensions, classification and
    # what "not yet converted" means -- is the routine's, for the operation the
    # config declares. One read for the whole set (backlog 619).
    try:
        governed = ManifestSource.get_for_operation(
            control, control.installation_id, config.operation,
        )
    except Exception as exc:  # noqa: BLE001 -- reported as a selection failure.
        raise ConversionError(
            f"Excel conversion '{config.name}' cannot select workbooks "
            f"for operation {config.operation!r}: {exc}"
        ) from exc

    resolved: list[ConversionCandidate] = []
    rejected: list[RejectedCandidate] = []
    claimed: dict[tuple[int, str], int] = {}

    # Stage one: turn each governed file into a candidate, from the object's
    # own facts. Nothing here touches the filesystem, and nothing re-derives
    # what the routine already resolved.
    for selected in governed:
        candidate, rejection = _resolve_candidate(config, selected.template_context())
        if rejection is not None:
            _reject(ctx, run_log, config, rejected, rejection)
            continue
        assert candidate is not None
        candidate = replace(candidate, governed=selected)
        identity = (candidate.inventory_record_id, candidate.file_id)
        first = claimed.get(identity)
        if first is not None:
            rejected.append(
                RejectedCandidate(
                    classification_record_id=candidate.classification_record_id,
                    reason_code="duplicate_inventory_source",
                    reason=(
                        f"Inventory record {candidate.inventory_record_id} is "
                        f"already claimed by classification record {first}."
                    ),
                )
            )
            continue
        claimed[identity] = candidate.classification_record_id
        resolved.append(candidate)

    # Stage two: validate the resolved file. This is the only stage that reads
    # the filesystem, and it is deliberately after selection and resolution.
    candidates: list[ConversionCandidate] = []
    for candidate in resolved:
        rejection = _validate_resolved_source(candidate)
        if rejection is not None:
            _reject(ctx, run_log, config, rejected, rejection)
            continue
        candidates.append(candidate)

    return SourceSelection(
        candidates=tuple(candidates),
        rejected=tuple(rejected),
        matched=len(governed),
    )


def _reject(
    ctx: Any, run_log,
    config: ExcelConversionConfig,
    rejected: list[RejectedCandidate],
    rejection: RejectedCandidate,
) -> None:
    """Record one rejection, logging the governed-source failures explicitly."""
    if rejection.reason_code in _MISSING_SOURCE_REASONS:
        _log_missing_source(ctx, run_log, config, rejection)
    rejected.append(rejection)


def run_excel_conversion(
    ctx: Any, run_log,
    inline_config: Mapping[str, Any],
    *,
    apply: bool = True,
) -> int:
    """Execute one inline Excel conversion over selected lifecycle records.

    ``apply`` is the workflow engine's existing mutation boundary, forwarded by
    the File Operator workflow adapter. This module owns no dry-run policy of
    its own: when ``apply`` is false, selection and its evidence still run and
    nothing on disk is touched.
    """

    config = resolve_excel_conversion_config(inline_config)
    if not config.enabled:
        _log.info("Excel conversion '%s' is disabled; skipping.", config.name)
        log_validation_result(run_log,
            validation_name="excel_conversion_config",
            status="skipped",
            message=f"Excel conversion '{config.name}' is disabled.",
            conversion_name=config.name,
        )
        return 0

    selection = select_conversion_candidates(ctx, run_log, config)
    _record_selection_evidence(ctx, run_log, config, selection)
    if not selection.candidates:
        # Nothing to convert is an outcome, not a failure. A workbook already
        # converted is not a candidate again, so a second run over the same
        # governed files selects none of them -- which is the step being
        # idempotent, not the step going wrong.
        #
        # It is recorded rather than passed over in silence: a selection that
        # matched nothing because the declared extensions match nothing is the
        # same shape, and an operator needs to see which one happened.
        _log.info(
            "Excel conversion '%s' has no unconverted workbook to convert.",
            config.name,
        )
        log_validation_result(run_log,
            validation_name="excel_conversion_selection",
            status="skipped",
            message=(
                f"Excel conversion '{config.name}' selected no workbook "
                f"for operation {config.operation!r}."
            ),
            conversion_name=config.name,
            rejected_count=len(selection.rejected),
        )
        return 0

    log_row_count(run_log,
        count_name="workbooks_queued",
        count=len(selection.candidates),
        subject=config.name,
    )

    if not apply:
        _log.info(
            "Excel conversion '%s' resolved %d workbook(s); dry run made no "
            "filesystem change.",
            config.name,
            len(selection.candidates),
        )
        return 0

    # Each candidate is already resolved; it crosses into the transform whole,
    # and its workbook is opened at exactly the mutation the selector named.
    for candidate in selection.candidates:
        workbook = _candidate_data_file(ctx, candidate)
        Transform(
            values={"config": config, "candidate": candidate},
            selected="convert",
        ).resolve(ctx).apply(workbook)

    log_row_count(run_log,
        count_name="workbooks_converted",
        count=len(selection.candidates),
        subject=config.name,
    )
    return 0


def _candidate_data_file(ctx: Any, candidate: ConversionCandidate) -> DataFile:
    """Open the candidate's workbook at exactly the mutation the selector named.

    Raises:
        ConversionError: If the candidate names no consumed mutation, or its
            workbook resolves to no registered DataFile type. Every candidate
            passed ``is_supported_workbook``, so this is a refusal for a
            disagreement between the two, and it stops the step.
    """
    mutation = candidate.mutation_context.source_record_id
    selected = candidate.governed
    if selected is None:
        raise ConversionError(
            f"Governed file {candidate.file_id} was selected with no governed "
            "object to open its workbook from."
        )
    try:
        return selected.data_file()
    except ConfigError as exc:
        raise ConversionError(
            f"Selected workbook {candidate.source_path} (mutation {mutation}) has "
            f"no registered DataFile type, so it cannot be converted: {exc}"
        ) from exc


def _application(ctx: Any) -> str:
    """The producing application: the runtime context's, never a literal."""
    return str(getattr(ctx, "app_name", "") or "")


@file_transform("convert", fields=("config", "candidate"))
class ConvertTransform(FileTransform):
    """Convert one governed workbook to CSV, and dispose of the workbook.

        workbook DataFile -> convert -> converted CSV DataFile(s)

    ``config`` is the step's validated conversion. ``candidate`` is the
    selection's already-resolved ConversionCandidate -- its validated source
    path, destinations and mutation context -- so nothing is resolved twice and
    no raw database row reaches the transform.
    """

    def __init__(
        self, ctx: Any, *,
        config: ExcelConversionConfig,
        candidate: ConversionCandidate,
    ) -> None:
        """Keep the validated conversion and the candidate it converts.

        Raises:
            ConversionError: If either is not what selection produces.
        """
        if not isinstance(config, ExcelConversionConfig):
            raise ConversionError(
                "Transform (convert) requires the step's validated "
                "ExcelConversionConfig."
            )
        if not isinstance(candidate, ConversionCandidate):
            raise ConversionError(
                "Transform (convert) requires a resolved ConversionCandidate."
            )
        self._ctx = ctx
        self._config = config
        self._candidate = candidate

    def apply(self, data_file: DataFile) -> tuple[DataFile, ...]:
        """Convert the workbook and return each CSV at its M10 mutation.

        The conversion reads the workbook where it is when conversion runs --
        after the processing move, when one is declared -- exactly as legacy
        does; ``data_file`` identifies the governed file, not where it now is.

        Returns:
            One DataFile per converted CSV, or nothing when the workbook
            produced no output.
        """
        produced = _convert_claimed_workbook(
            self._ctx, bound_run_log(), self._config, self._candidate)
        # The conversion produces CSV by definition, so the type is declared,
        # never derived from the output's name.
        return tuple(
            data_file_for(
                Path(path),
                file_type="CSV",
                **{**data_file.governed_facts(), "file_mutation_id": mutation},
            )
            for path, mutation in produced
        )


def _resolve_inline_excel_conversion_config(
    entry: Mapping[str, Any],
) -> ExcelConversionConfig:
    """Validate the canonical process-local Excel conversion mapping."""

    name_value = _get(entry, "name", _OPERATOR_NAME)
    if not isinstance(name_value, str) or not name_value.strip():
        raise ConversionError(
            "Excel conversion inline field 'name' must be a non-empty string."
        )
    name = name_value.strip()
    enabled_value = _get(entry, "enabled", True)
    if not isinstance(enabled_value, bool):
        raise ConversionError(
            "Excel conversion inline field 'enabled' must be a boolean."
        )
    if _has(entry, "inbox"):
        raise ConversionError(
            "Excel conversion inline configuration must not declare 'inbox'; "
            "candidates are named for the file_selection operation."
        )
    if _has(entry, "file_extensions"):
        raise ConversionError(
            "Excel conversion inline configuration must not declare "
            "'file_extensions'; which files are workbooks is owned by the "
            "manifest retrieval routine for file_selection.operation."
        )
    processing, processing_overwrite = _optional_folder_template(
        entry, "processing", name
    )
    outbox, outbox_overwrite = _required_folder_template(entry, "outbox", name)
    kickouts, kickouts_overwrite = _required_folder_template(
        entry, "kickouts", name
    )
    archive, archive_overwrite = _optional_folder_template(entry, "archive", name)
    return ExcelConversionConfig(
        name=name,
        enabled=enabled_value,
        operation=_operation(entry, name),
        source_field=_source_field(entry, name),
        processing=processing,
        outbox=outbox,
        kickouts=kickouts,
        archive=archive,
        folder_overwrite={
            "processing": processing_overwrite,
            "outbox": outbox_overwrite,
            "kickouts": kickouts_overwrite,
            "archive": archive_overwrite,
        },
        include_hidden_sheets=_optional_bool(
            entry, "include_hidden_sheets", False, name
        ),
        include_empty_sheets=_optional_bool(
            entry, "include_empty_sheets", False, name
        ),
    )


def _log_missing_source(
    ctx: Any, run_log,
    config: ExcelConversionConfig,
    rejection: RejectedCandidate,
) -> None:
    """Log an unresolvable governed source through the common error path.

    The classification record owns the current governed path; this process only
    proves that the referenced file is still there. A file that has gone missing
    is an explicit application error, never a silent skip and never a reason to
    search for the file on the filesystem.
    """

    error = ConversionError(rejection.reason)
    _log.error(
        "Excel conversion '%s' cannot resolve a governed source: %s",
        config.name,
        rejection.reason,
        exc_info=error,
    )


def _record_selection_evidence(
    ctx: Any, run_log,
    config: ExcelConversionConfig,
    selection: SourceSelection,
) -> None:
    """Record what the selector matched, resolved, and deliberately skipped."""

    log_row_count(run_log,
        count_name="classification_records_matched",
        count=selection.matched,
        subject=config.name,
    )
    for rejection in selection.rejected:
        log_validation_result(run_log,
            validation_name="excel_conversion_candidate",
            status="skipped",
            message=rejection.reason,
            conversion_name=config.name,
            reason_code=rejection.reason_code,
            classification_record_id=rejection.classification_record_id,
        )


def _convert_claimed_workbook(
    ctx: Any, run_log,
    config: ExcelConversionConfig,
    candidate: ConversionCandidate,
) -> tuple[tuple[Path, int], ...]:
    """Convert, record, and dispose one selected governed workbook.

    Returns:
        Each converted CSV's path and the M10 mutation that recorded it.
    """

    original_source = candidate.source_path
    log_input_discovered(run_log,
        input_name=original_source.name,
        path=str(original_source),
        exists=True,
        safe_to_preview=False,
        source=_application(ctx),
        **_lifecycle_evidence(config, candidate),
    )
    processing_file = original_source
    if candidate.destinations.processing is not None:
        processing_file = _move_source(
            ctx,
            original_source,
            candidate.destinations.processing,
            reason="processing",
            candidate=candidate,
            config=config,
            original_source=original_source,
        )

    try:
        result = convert_workbook_to_csv(
            processing_file,
            candidate.destinations.outbox,
            include_hidden_sheets=config.include_hidden_sheets,
            include_empty_sheets=config.include_empty_sheets,
            overwrite=candidate.destinations.folder_overwrite["outbox"],
        )
    except Exception as conversion_error:
        # The conversion ran against a governed workbook and produced nothing.
        # Recorded before the source is moved to kickouts, so the evidence
        # exists whether or not that move succeeds.
        _record_failed_conversion(ctx, candidate, original_source, conversion_error)
        try:
            _move_source(
                ctx,
                processing_file,
                candidate.destinations.kickouts,
                reason="kickouts",
                candidate=candidate,
                config=config,
                original_source=original_source,
            )
        except Exception as kickout_error:
            raise ConversionError(
                f"Workbook conversion failed for '{original_source.name}', and "
                f"the claimed source could not be moved to kickouts: {kickout_error}"
            ) from conversion_error
        raise

    produced = _record_conversion_result(ctx, run_log, config, candidate, result)
    if candidate.destinations.archive is not None:
        _move_source(
            ctx,
            processing_file,
            candidate.destinations.archive,
            reason="archive",
            candidate=candidate,
            config=config,
            original_source=original_source,
        )
    return produced


def _record_conversion_result(
    ctx: Any, run_log,
    config: ExcelConversionConfig,
    candidate: ConversionCandidate,
    result: WorkbookConversionResult,
) -> tuple[tuple[Path, int], ...]:
    """Record converted CSV artifacts and conversion warnings.

    Returns:
        Each converted CSV's path and the M10 mutation that recorded it.
    """

    original_source = candidate.source_path
    lifecycle = _lifecycle_evidence(config, candidate)
    producing_step = str(getattr(ctx, "pipeline_step_name", "") or "")
    application = _application(ctx)
    produced: list[tuple[Path, int]] = []
    for output in result.outputs:
        log_artifact_reference(run_log,
            str(output.output_path),
            role="converted_csv",
            event="created",
            created_by_step=producing_step,
            producer=application,
            artifact_type="converted_csv",
            source_path=str(original_source),
            artifact_group="output_files",
            producing_app=application,
            producing_step=producing_step,
            viewer_type="file",
            safe_to_preview=True,
            metadata={
                "conversion_name": config.name,
                "workbook_name": result.workbook_name,
                "extraction_kind": output.extraction_kind,
                "sheet_name": output.sheet_name,
                "sheet_index": output.sheet_index,
                "table_name": output.table_name,
                "row_count": output.row_count,
                "column_count": output.column_count,
                "column_names": list(output.column_names),
                **lifecycle,
            },
        )
        mutation = _append_mutation_record(
            ctx,
            config,
            candidate,
            action="create",
            operation=_OPERATOR_NAME,
            reason="converted_csv",
            destination_path=output.output_path,
            message=(
                f"Created converted output '{output.output_path.name}' for "
                f"inventory record {candidate.inventory_record_id}."
            ),
            # The producer states which part of the workbook it read, so a
            # consumer never infers conversion from a folder or a file name.
            conversion=ExcelConversionValues(
                name=config.name,
                sheet_name=output.sheet_name,
                table_name=output.table_name,
            ),
        )
        produced.append((output.output_path, mutation))

    for warning in result.warnings:
        log_validation_result(run_log,
            validation_name="excel_conversion",
            status="warning",
            message=warning.message,
            conversion_name=config.name,
            source_path=str(original_source),
            warning_code=warning.code,
            sheet_name=warning.sheet_name or "",
            table_name=warning.table_name or "",
            **lifecycle,
        )
    return tuple(produced)


def _move_source(
    ctx: Any,
    source_path: Path,
    destination: Path,
    *,
    reason: str,
    original_source: Path,
    candidate: ConversionCandidate,
    config: ExcelConversionConfig,
) -> Path:
    """Move one source workbook through the standard File Operator evidence path."""

    destination_file = destination / source_path.name
    if destination_file.exists() and not config.folder_overwrite[reason]:
        raise ConversionError(
            f"Excel conversion folder '{reason}' does not authorize replacing "
            f"existing file '{destination_file}'."
        )

    moved = move_file(
        source_path,
        destination,
        state_ctx=ctx,
        app=_application(ctx),
        pipeline=getattr(ctx, "pipeline_name", None),
        reason=reason,
        original_source=original_source,
        metadata={
            "pipeline_step_name": getattr(ctx, "pipeline_step_name", ""),
            "pipeline_step_id": getattr(ctx, "pipeline_step_id", ""),
        },
    )
    _log.info("moved to %s: %s", reason, moved)
    _append_mutation_record(
        ctx,
        config,
        candidate,
        action="move",
        source_path=source_path,
        destination_path=moved,
        message=(
            f"Source file moved to {reason} for inventory record "
            f"{candidate.inventory_record_id}."
        ),
        conversion=ExcelConversionValues(name=config.name),
        # Excel is the operation; the destination is the outcome.
        operation=_OPERATOR_NAME,
        reason="moved_to_" + reason,
    )
    return moved


def _record_failed_conversion(
    ctx: Any,
    candidate: ConversionCandidate,
    source_path: Path,
    error: BaseException,
) -> None:
    """Record that a conversion ran against a governed workbook and failed.

    Written before the exception continues, which is what makes it durable:
    the control connection is autocommit, so the row commits and the exception
    then travels through Python rather than through an open transaction.

    Never raises. Failing to record a failure must not replace the original
    error with a logging one.
    """
    try:
        log_governed_source_file_mutation(
            ctx,
            candidate.mutation_context,
            action="create",
            status="failed",
            source_path=str(source_path),
            application_name=_application(ctx),
            operation=_OPERATOR_NAME,
            # Same result as a success; status distinguishes them.
            reason="converted_csv",
            message=(
                f"Workbook conversion failed for '{source_path.name}': {error}"
            ),
        )
    except Exception:  # noqa: BLE001 -- the conversion error is the one to raise
        _log.warning("Could not record the failed conversion mutation for '%s'",
                     source_path.name)


def _append_mutation_record(
    ctx: Any,
    config: ExcelConversionConfig,
    candidate: ConversionCandidate,
    *,
    action: str,
    message: str,
    conversion: ExcelConversionValues,
    operation: str = "",
    reason: str = "",
    status: str = "success",
    source_path: Path | str = "",
    destination_path: Path | str = "",
    recovery_path: Path | str = "",
    previous_version_path: Path | str = "",
) -> int:
    """Append one evidence-linked authoritative filesystem mutation.

    Every value here is an approved input of the shared mutation contract.
    This module supplies what its conversion knows and constructs no part of
    the canonical record.
    """
    try:
        return log_governed_source_file_mutation(
            ctx,
            candidate.mutation_context,
            action=action,
            status=status,
            source_path=source_path,
            destination_path=destination_path,
            recovery_path=recovery_path,
            previous_version_path=previous_version_path,
            application_name=_application(ctx),
            message=message,
            conversion=conversion.payload(),
            operation=operation,
            reason=reason,
        )
    except LogRunRollbackError as exc:
        raise ConversionError(
            f"{_MUTATION_RECORD_TYPE} lifecycle evidence could not be "
            f"committed: {exc}"
        ) from exc


def _resolve_candidate(
    config: ExcelConversionConfig,
    row: Mapping[str, Any],
) -> tuple[ConversionCandidate | None, RejectedCandidate | None]:
    """Turn one selected row into a candidate, or reject it with a reason.

    The row arrives resolved: the database selected the governed file and its
    current location from the mutation history. What is checked here is that
    the row is usable -- a governed identity, a path, a classification -- and
    that the destination tokens the configuration declares can be filled from
    the classification's own values.

    Identity and lineage resolution used to live here. It does not any more:
    a governed file has one identity, and where it currently is, is a
    relational fact the routine answers.
    """

    file_id = row.get("file_manifest_id")

    def reject(code: str, reason: str) -> tuple[None, RejectedCandidate]:
        return None, RejectedCandidate(
            classification_record_id=(
                file_id if is_governed_file_id(file_id) else None
            ),
            reason_code=code,
            reason=reason,
        )

    if not is_governed_file_id(file_id):
        return reject(
            "missing_file_id",
            "Selected row carries no governed file id.",
        )

    classification = row.get("classification")
    if not isinstance(classification, Mapping):
        return reject(
            "missing_classification",
            f"Governed file {file_id} carries no classification, so it cannot "
            "supply destination values.",
        )
    raw_path = record_field(row, config.source_field)
    if not isinstance(raw_path, str) or not raw_path.strip():
        return reject(
            "missing_current_path",
            f"Governed file {file_id} has no current path in its mutation "
            "history.",
        )

    source_path = Path(raw_path).expanduser().resolve()
    if not source_path.is_file():
        return reject(
            "missing_source_file",
            f"Governed file {file_id} source file does not exist: "
            f"{source_path}",
        )

    if not is_supported_workbook(source_path):
        # Selection matched the recorded extension; this proves the file on
        # disk is actually a workbook. A classified non-workbook must not stop
        # a valid workbook from converting.
        return reject(
            "unsupported_workbook",
            f"Governed file {file_id} source file is not a supported "
            f"workbook: {source_path}",
        )

    destinations, unresolved = _resolve_destinations(config, row)
    if destinations is None:
        return reject(
            "unresolved_destination_token",
            f"Governed file {file_id} does not supply a usable value for "
            f"destination placeholder '<{unresolved}>'.",
        )

    classification_type = classification.get("type")
    values = classification.get("values")
    return (
        ConversionCandidate(
            classification_record_id=int(file_id),
            mutation_context=build_mutation_context({
                "file_id": file_id,
                "classification": dict(classification),
                # The mutation this step consumed, as the selector returned it.
                "source_record_id": row.get("file_mutation_id"),
            }),
            classification_type=(
                classification_type if isinstance(classification_type, str) else ""
            ),
            inventory_record_id=int(file_id),
            source_path=source_path,
            values=dict(values) if isinstance(values, Mapping) else {},
            destinations=destinations,
        ),
        None,
    )


def _validate_resolved_source(
    candidate: ConversionCandidate,
) -> RejectedCandidate | None:
    """Validate one resolved governed path, or reject it with a stable reason.

    The classification record owns the current governed path, so this proves
    only that the referenced file still exists and is convertible. A classified
    CSV must still not stop a valid workbook from converting.
    """
    source_path = candidate.source_path

    if not source_path.is_file():
        return RejectedCandidate(
            classification_record_id=candidate.classification_record_id,
            reason_code="missing_source_file",
            reason=(
                f"Classification record {candidate.classification_record_id} "
                "source file does not "
                f"exist: {source_path}"
            ),
        )

    if not is_supported_workbook(source_path):
        return RejectedCandidate(
            classification_record_id=candidate.classification_record_id,
            reason_code="unsupported_workbook",
            reason=(
                f"Classification record {candidate.classification_record_id} "
                "source file is not a "
                f"supported workbook: {source_path}"
            ),
        )
    return None


def _lifecycle_evidence(
    config: ExcelConversionConfig,
    candidate: ConversionCandidate,
) -> dict[str, Any]:
    """Return the lifecycle linkage carried by every conversion evidence row.

    Classification values are carried whole. No installation-defined variable
    name is read, named, or promoted to its own field by this module.
    """

    return {
        "classification_record_id": candidate.classification_record_id,
        "classification_file_id": candidate.file_id,
        "classification_type": candidate.classification_type,
        "classification_values": dict(candidate.values),
        "inventory_record_id": candidate.inventory_record_id,
        "inventory_source_path": str(candidate.source_path),
    }
def _source_field(entry: Any, name: str) -> str:
    """Return the field of the selected row that holds the workbook."""
    file_selection = _get(entry, "file_selection") if _has(entry, "file_selection") else None
    value = _get(file_selection, "source_field") if file_selection is not None else None
    if not isinstance(value, str) or not value.strip():
        raise ConversionError(
            f"Excel conversion '{name}' requires 'file_selection.source_field'."
        )
    return value.strip()


def _operation(entry: Any, name: str) -> str:
    """Return the operation whose remaining workbooks this conversion takes.

    The manifest retrieval routine (control.f_file_manifest_get) answers which
    governed files remain for it (backlog 619). The binding-name
    ``file_selection.procedure`` is retired and refused by name.
    """
    file_selection = _get(entry, "file_selection") if _has(entry, "file_selection") else None
    if file_selection is not None and _has(file_selection, "procedure"):
        raise ConversionError(
            f"Excel conversion '{name}' file_selection.procedure is retired "
            "(backlog 619): workbooks are retrieved by file_selection.operation "
            "through the manifest retrieval routine. Replace 'procedure' with "
            "'operation'."
        )
    operation = _get(file_selection, "operation") if file_selection is not None else None
    if not isinstance(operation, str) or not operation.strip():
        raise ConversionError(
            f"Excel conversion '{name}' requires a 'file_selection' mapping "
            "naming the operation whose remaining workbooks it converts."
        )
    return operation.strip()

def _is_config_mapping(value: Any) -> bool:
    """Return whether a value is a mapping- or Namespace-like config section."""

    if isinstance(value, Mapping):
        return True
    return callable(getattr(value, "keys", None))


def _get(entry: Any, key: str, default: Any = None) -> Any:
    """Return a value from a mapping- or Namespace-like entry."""

    if isinstance(entry, dict):
        return entry.get(key, default)
    return getattr(entry, key, default)


def _has(entry: Any, key: str) -> bool:
    """Return whether a mapping- or Namespace-like entry declares ``key``."""

    if isinstance(entry, dict):
        return key in entry
    keys = getattr(entry, "keys", None)
    return key in keys() if callable(keys) else hasattr(entry, key)


def _optional_bool(
    entry: Any,
    key: str,
    default: bool,
    name: str,
) -> bool:
    """Return one optional strict boolean configuration value."""

    if not _has(entry, key):
        return default
    value = _get(entry, key)
    if not isinstance(value, bool):
        raise ConversionError(
            f"Excel conversion '{name}' field '{key}' must be a boolean."
        )
    return value


def _required_folder_template(
    entry: Any,
    key: str,
    name: str,
) -> tuple[str, bool]:
    """Return one required folder template and collision authority."""

    if not _has(entry, key):
        raise ConversionError(
            f"Excel conversion '{name}' is missing required field '{key}'."
        )
    return _folder_template(_get(entry, key), key, name)


def _optional_folder_template(
    entry: Any,
    key: str,
    name: str,
) -> tuple[str | None, bool]:
    """Return one optional folder template and collision authority."""

    if not _has(entry, key):
        return None, False
    raw = _get(entry, key)
    if raw is None:
        return None, False
    return _folder_template(raw, key, name)


def _folder_template(raw: Any, key: str, name: str) -> tuple[str, bool]:
    """Parse a legacy string or canonical path/overwrite folder mapping."""

    overwrite = False
    if _is_config_mapping(raw):
        unknown = sorted(set(raw.keys()) - {"path", "overwrite"})
        if unknown:
            raise ConversionError(
                f"Excel conversion '{name}' folder '{key}' contains unknown "
                f"fields: {', '.join(unknown)}."
            )
        overwrite = _get(raw, "overwrite", False)
        if not isinstance(overwrite, bool):
            raise ConversionError(
                f"Excel conversion '{name}' folder '{key}' overwrite must be "
                "a boolean."
            )
        raw = _get(raw, "path")
    if not isinstance(raw, (str, Path)) or not str(raw).strip():
        raise ConversionError(
            f"Excel conversion '{name}' folder '{key}' must contain a non-empty path."
        )
    template = str(raw).strip()
    _validate_template(template, key, name)
    return template, overwrite


def _validate_template(template: str, key: str, name: str) -> None:
    """Require every declared placeholder to name a non-empty field path."""

    for field in _TEMPLATE_FIELD.findall(template):
        if not field.strip():
            raise ConversionError(
                f"Excel conversion '{name}' field '{key}' declares an empty "
                "'<>' placeholder."
            )
        if field.strip() != field:
            raise ConversionError(
                f"Excel conversion '{name}' field '{key}' placeholder "
                f"'<{field}>' must not be padded with whitespace."
            )


def _resolve_destinations(
    config: ExcelConversionConfig,
    record: Mapping[str, Any],
) -> tuple[ConversionDestinations | None, str | None]:
    """Resolve one candidate's destinations, or report the unresolved field.

    Placeholders are resolved from the selected row as the selector returned
    it. This module never names a field itself -- the configured template
    supplies every field path, so installation vocabulary stays in YAML.
    """

    resolved: dict[str, Path | None] = {}
    for key in ("processing", "outbox", "kickouts", "archive"):
        template = getattr(config, key)
        if template is None:
            resolved[key] = None
            continue
        resolution = resolve_record_template(template, record)
        if not resolution.resolved:
            return None, resolution.missing_field
        resolved[key] = resolution.path

    return (
        ConversionDestinations(
            processing=resolved["processing"],
            outbox=resolved["outbox"],
            kickouts=resolved["kickouts"],
            archive=resolved["archive"],
            folder_overwrite=dict(config.folder_overwrite),
        ),
        None,
    )
