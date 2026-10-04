"""The Loader's governed whole-file text sanitization (row 589, step 4).

Copied from the legacy file_operator ``file_sanitization`` module, which stays
untouched as the reference. The step selects already-governed lifecycle records
and proves their identity before calling :func:`rey_lib.files.sanitize_file`.
Destination and collision policy are owned together by the declared output
folder; the shared boundary performs no manifest lookup or identity
reconstruction.

    selected row
      ├─ the row itself ──────────────────────→ Transform(sanitize) values
      └─ file_mutation_id → ManifestSource → DataFile → SanitizeTransform

  apply  -> (sanitized DataFile[, redacted-sister DataFile]), identities from
            the M7 record (file_manifest_record_id) and the M9 sister mutation
  plan   -> the dry run: sanitize_file(dry_run=True) writes nothing, and its
            FileSanitizationResult is the answer -- no governed identity is
            invented for a file that was not produced

What changed from the legacy module, and only because the boundary required it:
the per-row body is ``SanitizeTransform``, reached through ``Transform``; the
error is ``SanitizationError``; and the producer is the runtime context's
application rather than a literal.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from rey_lib.errors.error_utils import AppError, ConfigError, DatabaseError
from rey_lib.files import (
    EffectiveSanitizationPolicy,
    FileSanitizationCollisionPolicy,
    FileSanitizationContext,
    FileSanitizationResult,
    FileSetCollisionError,
    FileSetMember,
    GovernedFileReference,
    compose_sanitization_policy,
    publish_file_set,
    redacted_companion_path,
    sanitize_file,
)
from rey_lib.files.csv import CsvReadError, open_csv
from rey_lib.files.data_file import DataFile, data_file_for
from rey_lib.files.file_routing import FileRoutingError
from rey_lib.files.sanitization import FileSanitizationError
from rey_lib.load.file_transform import FileTransform, file_transform
from rey_lib.load.manifest_source import ManifestSource
from rey_lib.load.mutation_context import (
    MutationContext,
    MutationContextError,
    build_mutation_context,
    log_governed_source_file_mutation,
)
from rey_lib.load.record_templates import MISSING, record_field, resolve_record_template
from rey_lib.load.redacted_artifacts import redacted_csv_text
from rey_lib.load.transform import Transform
from rey_lib.logs import (
    bound_run_log,
    log_artifact_reference,
    log_input_file_reference,
    log_run_record,
)
from rey_lib.logs.file_manifest import FileManifestError

__all__ = [
    "FileSanitizationBatchResult",
    "SanitizationError",
    "SanitizeTransform",
    "run_file_sanitization",
]


class SanitizationError(AppError):
    """Raised when governed file sanitization cannot proceed."""


@dataclass(frozen=True)
class FileSanitizationBatchResult:
    """Results for the exact governed records selected by one process call."""

    selected: int
    sanitized: int
    results: tuple[FileSanitizationResult, ...]
    #: One "<file>: <reason>" per file that could not be sanitized. Each was
    #: kicked out by the common execution path and the batch went on.
    failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class _DeclaredFolder:
    """One configured folder template and its local collision authority."""

    path: str
    overwrite: bool
    redacted_copy: bool = False


@dataclass(frozen=True)
class _SanitizationConfig:
    operation: str
    source_field: str
    feed: str
    policy: EffectiveSanitizationPolicy
    outbox: _DeclaredFolder


def run_file_sanitization(
    ctx: Any, run_log,
    inline_config: Mapping[str, Any],
    *,
    apply: bool = True,
) -> FileSanitizationBatchResult:
    """Sanitize the governed files selected by one inline process mapping.

    Each selected row opens exactly its own mutation as a DataFile and is
    sanitized through ``Transform(sanitize)``: ``apply`` on an applied run,
    ``plan`` on a dry run. The run log is the bound one the transform writes
    through, as the legacy step wrote through the one it was handed.

    One file that cannot be sanitized is a fact about that file: its original
    is kicked out by the common execution path (backlog 624), its failure is
    recorded here, and the batch goes on.
    """
    config = _resolve_config(inline_config)
    # Resolved before selection, as the legacy step resolves it: a context with
    # no governed root is refused even when nothing is selected.
    _governed_root(ctx)

    # The database says which files these are, at which mutation, and each is
    # named once, so there is nothing here to deduplicate and no origin to
    # reconcile between two declared selections. One read for the whole set.
    sources = _files_to_sanitize(ctx, config.operation)

    results: list[FileSanitizationResult] = []
    failures: list[str] = []
    for source in sources:
        data_file = _selected_data_file(source)
        # The transform reads the governed file's own facts -- its path, its
        # classification, its conversion provenance -- off the object, never
        # off a selector row (backlog 619).
        sanitizer = Transform(
            values={"process": inline_config, "record": source.template_context()},
            selected="sanitize",
        ).resolve(ctx)
        try:
            if apply:
                sanitizer.apply(data_file)
            else:
                sanitizer.plan(data_file)
        except (SanitizationError, FileSanitizationError) as error:
            # Kicked out (applied runs), then recorded, by the kind (rules 75
            # and 78); the batch reports it and goes on.
            failures.append(f"{data_file.path.name}: {error}")
            continue
        results.append(sanitizer.result)

    return FileSanitizationBatchResult(
        selected=len(sources),
        sanitized=sum(result.filesystem_applied for result in results),
        results=tuple(results),
        failures=tuple(failures),
    )


def _selected_data_file(source: ManifestSource) -> DataFile:
    """Open exactly the selected file's governed state as a DataFile.

    Raises:
        SanitizationError: If the file resolves to no registered DataFile type.
            Sanitization has no rejection path, so this stops the step, as every
            legacy per-row failure does.
    """
    try:
        return source.data_file()
    except ConfigError as exc:
        raise SanitizationError(
            f"Selected mutation {source.file_mutation_id!r} names a file with no "
            f"registered DataFile type, so it cannot be sanitized: {exc}"
        ) from exc


@file_transform("sanitize", fields=("process", "record"))
class SanitizeTransform(FileTransform):
    """Sanitize one governed file, and publish its redacted sister if declared.

        DataFile (selected state) -> sanitize -> sanitized DataFile
                                                 [+ redacted-sister DataFile]

    ``process`` is the step's inline process DECLARATION, validated here as the
    step validates it. ``record`` is the selected file's
    ``ManifestSource.template_context()`` -- the governed object's own facts,
    not a selector row (backlog 619): the outbox template, the source field and
    the source origin read it exactly as the legacy step read its row, and it is
    never a DataFile property.
    """

    def __init__(self, ctx: Any, *, process: Any, record: Any) -> None:
        """Validate the process declaration this transform sanitizes by.

        Raises:
            SanitizationError: If the declaration or the row is invalid.
        """
        if not _is_mapping_like(record):
            raise SanitizationError(
                "File sanitization requires the selected row as a mapping."
            )
        self._ctx = ctx
        self._config = _resolve_config(process)
        self._record = _plain_mapping(record)
        self._result: FileSanitizationResult | None = None

    @property
    def result(self) -> FileSanitizationResult | None:
        """What the last ``apply`` or ``plan`` did, as sanitization reported it."""
        return self._result

    file_failures = (SanitizationError, FileSanitizationError)

    def _record_failure(self, data_file: DataFile, error: BaseException) -> None:
        """This file's ERROR record: after the kickout when applied
        (FileTransform.apply), and by :meth:`plan` on a dry run, which moves
        nothing. The step records nothing (rule 78)."""
        log_run_record(bound_run_log(),
            "ERROR",
            message=f"Sanitization failed for '{data_file.path.name}': {error}",
            path=str(data_file.path),
            error_message={"failure_reason": str(error)},
        )

    def _apply(self, data_file: DataFile) -> tuple[DataFile, ...]:
        """Sanitize the file, recording it, and return what was produced.

        Returns:
            The sanitized DataFile, and the redacted sister when the outbox
            declares one -- each at the mutation it was recorded under. Nothing,
            when sanitization applied nothing to the filesystem.
        """
        _result, produced = self._sanitize(data_file, dry_run=False)
        return produced

    def plan(self, data_file: DataFile) -> FileSanitizationResult:
        """Run sanitization as a dry run: nothing is written.

        Returns:
            Sanitization's own result. No governed DataFile is returned, because
            no mutation exists to identify one.
        """
        try:
            result, _produced = self._sanitize(data_file, dry_run=True)
        except self.file_failures as error:
            self._record_failure(data_file, error)
            raise
        return result

    def _sanitize(
        self, data_file: DataFile, *, dry_run: bool,
    ) -> tuple[FileSanitizationResult, tuple[DataFile, ...]]:
        """The legacy per-row body, for this transform's selected row."""
        ctx = self._ctx
        run_log = bound_run_log()
        config = self._config
        record = self._record
        application = str(getattr(ctx, "app_name", "") or "")
        governed_root = _governed_root(ctx)
        feed = config.feed
        policy = config.policy

        source_origin = _source_origin(record)
        source = record_field(record, config.source_field)
        if not isinstance(source, str) or not source.strip():
            raise SanitizationError(
                f"Selected row {record.get('file_manifest_id')!r} has no "
                f"non-empty {config.source_field!r} to sanitize."
            )
        destination = resolve_record_template(config.outbox.path, record)
        if not destination.resolved:
            raise SanitizationError(
                f"Sanitization outbox for selected row "
                f"{record.get('file_mutation_id')!r} could not be resolved; template "
                f"field {destination.missing_field!r} is missing or empty."
            )
        mutation_context = _mutation_context(record)
        try:
            file_reference = GovernedFileReference(
                file_id=mutation_context.file_id,
                current_path=source,
                classification=mutation_context.classification,
            )
            operation = FileSanitizationContext(
                state_ctx=ctx,
                run_log=run_log,
                application_name=application,
                destination_path=destination.path,
                governed_roots=(governed_root,),
                policy=policy,
                collision_policy=(
                    FileSanitizationCollisionPolicy.OVERWRITE
                    if config.outbox.overwrite
                    else FileSanitizationCollisionPolicy.FAIL
                ),
                dry_run=dry_run,
                add_source_line_number=True,
                file_operation_metadata={
                    "pipeline_step_name": getattr(ctx, "pipeline_step_name", ""),
                    "pipeline_step_id": getattr(ctx, "pipeline_step_id", ""),
                },
                mutation_run_log_fields={
                    "source_origin": source_origin,
                    "sanitization_feed": feed,
                    "source_record_id": record.get("file_mutation_id"),
                },
            )
        except (FileRoutingError, FileManifestError, FileSanitizationError) as exc:
            raise SanitizationError(f"File sanitization configuration is invalid: {exc}") from exc
        producing_step = str(getattr(ctx, "pipeline_step_name", "") or "")
        log_input_file_reference(run_log,
            str(source),
            file_role="sanitization_input",
            consumed_by_step=producing_step,
            producing_app=application,
            status="consumed",
            safe_to_preview=True,
            viewer_type="file",
            file_id=mutation_context.file_id,
            source_origin=source_origin,
            sanitization_feed=feed,
        )
        result = sanitize_file(operation, file_reference)
        self._result = result
        if not result.filesystem_applied:
            return result, ()

        log_artifact_reference(run_log,
            str(result.destination_path),
            role="sanitized_file",
            event="created",
            created_by_step=producing_step,
            producer=application,
            artifact_type="sanitized_file",
            source_path=str(source),
            artifact_group="output_files",
            producing_app=application,
            producing_step=producing_step,
            viewer_type="file",
            safe_to_preview=True,
            file_id=mutation_context.file_id,
            source_origin=source_origin,
            sanitization_feed=feed,
            destination_replaced=result.destination_replaced,
            destination_sha256=result.destination_sha256,
            destination_size=result.destination_size,
            effective_policy_digest=result.effective_policy_digest,
        )
        produced: list[DataFile] = []
        # The sanitized file is the mutation sanitization recorded (M7); without
        # that record there is no governed identity to give it.
        if result.file_manifest_record_id is not None:
            produced.append(_governed_output(
                data_file, result.destination_path, result.file_manifest_record_id))
        if config.outbox.redacted_copy:
            sister, sister_mutation = _publish_redacted_sister(ctx, run_log,
                result.destination_path,
                producing_step=producing_step,
                overwrite=config.outbox.overwrite,
                mutation_context=mutation_context,
                source_origin=source_origin,
                feed=feed,
                application=application,
            )
            produced.append(_governed_output(data_file, sister, sister_mutation))
        return result, tuple(produced)


def _governed_output(source: DataFile, path: Path, file_mutation_id: int) -> DataFile:
    """A file this step produced, at the mutation that recorded it.

    It is the same governed file, so its governed facts -- identity,
    classification, base path and its original's -- are the source's; only the
    path and the state are new.
    """
    return data_file_for(
        Path(path),
        **{**source.governed_facts(), "file_mutation_id": file_mutation_id},
    )


def _files_to_sanitize(ctx: Any, operation: str) -> list[ManifestSource]:
    """The governed files still to sanitize, one ManifestSource each.

    One read through the manifest retrieval routine for the declared operation
    (backlog 619): which files remain, at which mutation, in this installation,
    and without kicked-out files, is the database's answer.
    """
    control = getattr(ctx, "shared_control", None)
    if control is None:
        raise SanitizationError(
            "The governed file manifest is held in the control database, and "
            "this context exposes no shared Control to ask which files still "
            "need sanitizing."
        )
    try:
        return ManifestSource.get_for_operation(control, control.installation_id, operation)
    except (ConfigError, DatabaseError) as exc:
        raise SanitizationError(
            f"File sanitization cannot select files for operation {operation!r}: "
            f"{exc}"
        ) from exc


def _source_origin(record: Mapping[str, Any]) -> str:
    """Which of the two origins one selected record came in through.

    The routine's two branches are the two origins, and the record says which
    without anything being declared: a CSV that an operator produced carries
    the conversion that produced it, and a delivered CSV carries none. Its own
    path is a .csv either way, so the file's extension cannot tell them apart.
    """
    # Absence, not falsiness: record_field answers a missing path with the
    # MISSING sentinel, which is an object() and therefore truthy. Tested for
    # truth, every record took the first branch and every delivered CSV was
    # recorded as excel_generated.
    operator = record_field(record, "conversion.operator")
    return "excel_generated" if operator is not MISSING else "delivered_csv"


def _resolve_config(inline_config: Mapping[str, Any]) -> _SanitizationConfig:
    if not _is_mapping_like(inline_config):
        raise SanitizationError(
            "File sanitization requires an inline process configuration mapping."
        )
    inline_config = _plain_mapping(inline_config)
    if "config_ref" in inline_config:
        raise SanitizationError(
            "File sanitization config_ref is unsupported; supply inline "
            "workflow process configuration."
        )
    if "overwrite" in inline_config:
        raise SanitizationError(
            "File sanitization step-level 'overwrite' is unsupported; declare "
            "it on the folder that owns the collision policy."
        )
    if "selections" in inline_config:
        raise SanitizationError(
            "File sanitization declares no selection of its own; the manifest "
            "retrieval routine names the files that still need sanitizing for "
            "file_selection.operation. Remove 'selections'."
        )
    file_selection = _required_mapping(inline_config, "file_selection")
    if "procedure" in file_selection:
        raise SanitizationError(
            "File sanitization's file_selection.procedure is retired (backlog "
            "619): files are retrieved by file_selection.operation through the "
            "manifest retrieval routine. Replace 'procedure' with 'operation'."
        )
    operation = _required_text(file_selection, "operation")
    source_field = _required_text(file_selection, "source_field")
    outbox = _required_folder(inline_config, "outbox")
    feed = _required_text(inline_config, "feed")
    sanitization = inline_config.get("sanitization")
    if not isinstance(sanitization, Mapping):
        raise SanitizationError(
            "File sanitization requires an inline 'sanitization' mapping."
        )
    global_policy = sanitization.get("global")
    feeds = sanitization.get("feeds")
    if not isinstance(global_policy, Mapping) or not isinstance(feeds, Mapping):
        raise SanitizationError(
            "File sanitization requires sanitization.global and "
            "sanitization.feeds mappings."
        )
    feed_policy = feeds.get(feed)
    if not isinstance(feed_policy, Mapping):
        raise SanitizationError(
            f"File sanitization names unknown feed {feed!r}."
        )
    try:
        policy = compose_sanitization_policy(global_policy, feed_policy)
    except FileSanitizationError as exc:
        raise SanitizationError(
            f"File sanitization policy for feed {feed!r} is invalid: {exc}"
        ) from exc
    return _SanitizationConfig(
        operation=operation,
        source_field=source_field,
        feed=feed,
        policy=policy,
        outbox=outbox,
    )


def _publish_redacted_sister(
    ctx: Any, run_log,
    destination: Path,
    *,
    producing_step: str,
    overwrite: bool,
    mutation_context: MutationContext,
    source_origin: str,
    feed: str,
    application: str,
) -> tuple[Path, int]:
    """Write the sanitized artifact's redacted sister beside it.

    The sister is derived from the sanitized artifact, so header, row order,
    column order, and both counts are whatever sanitization produced; only
    field values are replaced, through the shared governed boundary.

    Returns:
        The sister's path and the mutation that recorded it (M9).
    """
    sister = redacted_companion_path(destination)
    try:
        # Every physical row is read, so a file whose header cannot be located
        # still has all of its values redacted rather than none of them.
        stream = open_csv(destination, include_all_rows=True)
        columns = list(stream.header_fields)
        every_row = [list(row.fields) for row in stream.rows]
        rows = (
            every_row
            if stream.header_line_number is None
            else every_row[stream.header_line_number:]
        )
    except (CsvReadError, OSError) as exc:
        raise SanitizationError(
            f"Sanitized artifact '{destination}' could not be read for its "
            f"redacted sister: {exc}"
        ) from exc
    try:
        publish_file_set(
            [FileSetMember(
                destination=sister,
                text=redacted_csv_text(columns, rows, stream.delimiter),
            )],
            on_collision="replace" if overwrite else "fail",
        )
    except (FileSetCollisionError, OSError) as exc:
        raise SanitizationError(
            f"Redacted sister for '{destination}' could not be published: {exc}"
        ) from exc
    # The companion is a created artifact of the same governed file, so it is
    # recorded as its own mutation. Without one it exists on disk and in the
    # run log but never appears in the governed file's lifecycle.
    sister_mutation = log_governed_source_file_mutation(
        ctx,
        mutation_context,
        action="create",
        status="success",
        destination_path=str(sister),
        application_name=application,
        operation="file_sanitization",
        reason="redacted_sanitized_file",
        run_log_fields={
            "source_origin": source_origin,
            "sanitization_feed": feed,
        },
    )
    log_artifact_reference(run_log, str(sister), role="redacted_sanitized_file", event="created",
        created_by_step=producing_step, producer=application,
        artifact_type="redacted_sanitized_file", source_path=str(destination),
        artifact_group="output_files", producing_app=application,
        producing_step=producing_step, viewer_type="file", safe_to_preview=True,
        file_id=mutation_context.file_id, source_origin=source_origin,
        sanitization_feed=feed,
    )
    return sister, sister_mutation


def _required_folder(mapping: Mapping[str, Any], field: str) -> _DeclaredFolder:
    """Return one declared folder with folder-local overwrite authority."""
    declared = mapping.get(field)
    if not isinstance(declared, Mapping):
        raise SanitizationError(
            f"File sanitization requires inline {field!r} as a folder mapping."
        )
    unknown = sorted(set(declared) - {"path", "overwrite", "redacted_copy"})
    if unknown:
        raise SanitizationError(
            f"File sanitization folder {field!r} contains unknown fields: "
            f"{', '.join(unknown)}."
        )
    path = _required_text(declared, "path")
    overwrite = declared.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise SanitizationError(
            f"File sanitization folder {field!r} 'overwrite' must be true or false."
        )
    redacted_copy = declared.get("redacted_copy", False)
    if not isinstance(redacted_copy, bool):
        raise SanitizationError(
            f"File sanitization folder {field!r} 'redacted_copy' must be true "
            "or false."
        )
    return _DeclaredFolder(
        path=path, overwrite=overwrite, redacted_copy=redacted_copy
    )


def _required_mapping(mapping: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    value = mapping.get(field)
    if not _is_mapping_like(value):
        raise SanitizationError(
            f"File sanitization requires an inline {field!r} mapping."
        )
    return _plain_mapping(value)


def _required_text(mapping: Mapping[str, Any], field: str) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SanitizationError(
            f"File sanitization requires non-empty inline {field!r}."
        )
    return value.strip()


def _is_mapping_like(value: Any) -> bool:
    """Return whether config loading exposes mapping keys for ``value``."""
    return isinstance(value, Mapping) or callable(getattr(value, "keys", None))


def _plain_mapping(value: Any) -> dict[str, Any]:
    """Convert one loaded Mapping/Namespace tree without changing its values."""
    if not _is_mapping_like(value):
        raise ConfigError("Expected a mapping-like configuration value.")
    return {
        str(key): _plain_config_value(value.get(key))
        for key in value.keys()
    }


def _plain_config_value(value: Any) -> Any:
    if _is_mapping_like(value):
        return _plain_mapping(value)
    if isinstance(value, list):
        return [_plain_config_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_plain_config_value(item) for item in value)
    return value


def _mutation_context(record: Mapping[str, Any]) -> MutationContext:
    """Bind one selected row to the governed identity it names.

    The row is a row and the context is an identity: ``MutationContext`` is
    producer-neutral and does not know a selector exists, so the translation
    between ``file_manifest_id`` and ``file_id`` happens here, in the step that
    knows both.
    """
    try:
        return build_mutation_context({
            "file_id": record.get("file_manifest_id"),
            "classification": record.get("classification"),
            # The mutation this step consumed, as the selector returned it.
            "source_record_id": record.get("file_mutation_id"),
        })
    except MutationContextError as exc:
        raise SanitizationError(
            f"Selected row {record.get('file_mutation_id')!r} cannot bind "
            f"governed sanitization identity: {exc}"
        ) from exc


def _governed_root(ctx: Any) -> Path:
    try:
        return Path(ctx.paths.resolve("data")).expanduser().resolve()
    except (AttributeError, ConfigError, TypeError, ValueError) as exc:
        raise SanitizationError(
            "File sanitization requires the configured governed 'data' path."
        ) from exc
