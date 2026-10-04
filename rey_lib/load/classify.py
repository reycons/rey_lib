"""Source-file classification, the Loader's.

Copied from the legacy file_operator implementation (``source_classification``,
with the capture validation and config readers it took from ``discovery``) into
its final Loader-owned location. Behaviour, ordering, logging, config
interpretation and evidence are unchanged. What changed is only what the new
boundary requires:

  * an applied run opens each selected row at exactly the selected mutation --
    ``ManifestSource.create(file_mutation_id=...).data_file()`` -- and applies
    ``Transform(classify)`` to that DataFile; ``ClassifyTransform`` owns what
    ``_persist_source_classification_outcome`` did;
  * the move to processing goes through ``MoveTransform`` (``plan`` -> record ->
    ``apply`` -> verify), not through routing called directly;
  * the selected row's ``record_type`` travels in the step's candidate context
    (the classify kind's ``source_record_type``), never as a DataFile property;
  * a selected file whose type resolves to no registered DataFile is a
    rejection (``no_registered_data_file``);
  * failures are ``ClassificationError``.

A dry run classifies the selected rows in memory and persists nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from rey_lib.errors.error_utils import AppError, ConfigError, DatabaseError
from rey_lib.files import FileRoutingError
from rey_lib.files.data_file import DataFile, UntypedFile
from rey_lib.files.governed_file import FileId, is_governed_file_id
from rey_lib.load.file_transform import FileTransform, file_transform
from rey_lib.load.manifest_source import ManifestSource
from rey_lib.load.record_templates import MISSING, record_field
from rey_lib.load.transform import Transform
from rey_lib.logs import (
    FileManifestError,
    bound_run_log,
    log_file_manifest_record,
    log_run_record,
)
from rey_lib.workflow import RunContext


class ClassificationError(AppError):
    """A classification scope, source or outcome that cannot proceed."""


class ClassificationRejected(ClassificationError):
    """A file terminally rejected by classification: recorded, then kicked out.

    The classify kind's one file failure (backlog 624). It is raised only after
    the rejection's evidence and lifecycle record are written, for a source that
    does not retry its rejects, so the common execution path moves the original
    to ``<inbox>/kickouts``. Every other ClassificationError is a durability or
    configuration failure and kicks nothing out.
    """


_CLASSIFICATION_RECORD_TYPE = "source_file_classification"
# Distinguishes an absent manifest field from one recorded as None.

__all__ = [
    "ClassificationError",
    "ClassifyTransform",
    "FileClassificationSourceConfig",
    "SourceClassificationBatchResult",
    "SourceClassificationCandidate",
    "SourceClassificationOutcome",
    "classify_source_candidates",
    "load_source_classification_candidates",
    "build_data_profile_key",
    "resolve_classification_source_configs",
    "run_source_file_classification",
    "serialize_source_file_classification_record",
]


@dataclass(frozen=True)
class FileClassificationSourceConfig:
    """One fully validated manifest-backed classification source."""

    name: str
    enabled: bool
    classification_type: str
    operation: str
    source_field: str
    variables: tuple[DiscoveryVariable, ...]
    path_regex: re.Pattern[str]
    retry_rejects: bool = False
    processing: str | None = None
    #: Which classified values say two files hold the same kind of data, in the
    #: order configuration declared them. Empty when the source declares no
    #: ``profile`` block, which is every source that has not asked for one.
    profile_key_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceClassificationCandidate:
    """One matching manifest record prepared for later classification."""

    classification: FileClassificationSourceConfig
    manifest_record: Mapping[str, Any]
    source_record_type: str
    value: str | None
    #: The governed file this candidate classifies. One identity: the database
    #: mints control.file_manifest.file_manifest_id and the application calls
    #: it file_id. Classification writes back to this file rather than
    #: recording a separate record, so the id is the link and not a
    #: cross-reference.
    file_id: FileId | None
    status: str
    reason_code: str | None = None
    reason: str | None = None
    #: The governed file this candidate was prepared from, opened once by
    #: ManifestSource.get_for_operation (backlog 619). Its DataFile is opened
    #: from it; no second read of the file's context is made.
    governed: Any = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class SourceClassificationOutcome:
    """One generic in-memory classification decision."""

    candidate: SourceClassificationCandidate
    status: str
    values: Mapping[str, str | None]
    reason_code: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class SourceClassificationBatchResult:
    """Ordered outcomes and aggregate counts for one candidate batch."""

    outcomes: tuple[SourceClassificationOutcome, ...]
    candidates: int
    classified: int
    rejected: int


def resolve_classification_source_configs(
    process_config: Any,
) -> tuple[FileClassificationSourceConfig, ...]:
    """Validate every enabled source before any classification processing.

    Disabled entries are ignored after their ``enabled`` flag is validated.
    Every malformed enabled source is reported together so callers cannot begin
    manifest processing with a partially valid configuration scope.
    """
    entries = _get(process_config, "sources", None)
    if not isinstance(entries, list) or not entries:
        raise ClassificationError(
            "Source classification requires a non-empty ordered 'sources' list."
        )

    configs: list[FileClassificationSourceConfig] = []
    failures: list[str] = []
    enabled_names: set[str] = set()

    for index, entry in enumerate(entries):
        label = f"sources[{index}]"
        try:
            enabled = _required_bool(entry, "enabled", label)
        except ClassificationError as exc:
            failures.append(f"{label}: {exc}")
            continue
        if not enabled:
            continue

        name = str(_get(entry, "name", "") or "").strip()
        failure_name = name or label
        try:
            if not name:
                raise ClassificationError(
                    f"Source classification {label} requires a non-empty 'name'."
                )
            if name in enabled_names:
                raise ClassificationError(
                    f"Source classification declares duplicate source '{name}'."
                )
            config = _classification_source_config(entry, name)
        except ClassificationError as exc:
            failures.append(f"{failure_name}: {exc}")
            continue

        enabled_names.add(name)
        configs.append(config)

    if failures:
        raise ClassificationError(
            "Source classification configuration is invalid; no manifest was read "
            "and no records were processed. " + " | ".join(failures)
        )
    return tuple(configs)


def load_source_classification_candidates(
    ctx: Any, run_log,
    process_config: Any,
) -> tuple[SourceClassificationCandidate, ...]:
    """Select each source's governed files and prepare them without classifying.

    One read per source, through the manifest retrieval routine for the
    source's declared operation and its name (backlog 619). No filter is
    declared here and none is applied afterwards: the routine returns the work,
    including excluding what is already classified, and this module interprets
    nothing about how it decided. Each candidate is prepared from the governed
    object's own facts. Candidate-level field problems produce a rejected
    candidate and never stop later records from being prepared.
    """
    control = getattr(ctx, "shared_control", None)
    if control is None:
        raise ClassificationError(
            "The governed file manifest is held in the control database, and "
            "this context exposes no shared Control to select from it."
        )

    configs = resolve_classification_source_configs(process_config)
    candidates: list[SourceClassificationCandidate] = []

    for config in configs:
        # The source's name is its identity, and the routine's scope: the
        # database narrows to this source, never this module.
        #
        # Nothing filters the result afterwards. A file the routine did not
        # return is not this run's work -- including one already classified,
        # which the routine excludes itself.
        try:
            governed = ManifestSource.get_for_operation(
                control, control.installation_id, config.operation,
                source_name=config.name,
            )
        except (ConfigError, DatabaseError) as exc:
            raise ClassificationError(
                f"Source classification '{config.name}' cannot select files "
                f"for operation '{config.operation}': {exc}"
            ) from exc

        for selected in governed:
            candidate = _candidate(config, selected.template_context())
            candidates.append(replace(candidate, governed=selected))

    return tuple(candidates)


def classify_source_candidates(
    candidates: tuple[SourceClassificationCandidate, ...],
) -> SourceClassificationBatchResult:
    """Classify prepared candidates in order without producing durable effects."""
    outcomes = tuple(_classify_candidate(candidate) for candidate in candidates)
    classified = sum(outcome.status == "classified" for outcome in outcomes)
    rejected = sum(outcome.status == "rejected" for outcome in outcomes)
    return SourceClassificationBatchResult(
        outcomes=outcomes,
        candidates=len(outcomes),
        classified=classified,
        rejected=rejected,
    )


def serialize_source_file_classification_record(
    outcome: SourceClassificationOutcome,
    *,
    run_log_id: int,
    recorded_at: str,
    file_path: Path | str | None = None,
    application: str = "",
) -> dict[str, Any]:
    """Serialize one outcome for the governed manifest append boundary.

    ``record_id`` is intentionally absent because the shared manifest writer
    assigns it atomically when appending. This function has no I/O and does not
    write evidence or mutate the manifest.

    ``file`` carries the observed extension and governed current path. When
    classification owns the processing move, ``file_path`` supplies the moved
    path; otherwise the linked inventory path is preserved.
    """
    candidate = outcome.candidate
    config = candidate.classification
    if candidate.file_id is None:
        raise ClassificationError(
            "A classification record requires linked inventory file_id and record_id."
        )
    if (
        not isinstance(run_log_id, int)
        or isinstance(run_log_id, bool)
        or run_log_id <= 0
    ):
        raise ClassificationError(
            "A classification record requires a positive run_log_id."
        )
    if not isinstance(recorded_at, str) or not recorded_at.strip():
        raise ClassificationError(
            "A classification record requires a non-empty recorded_at timestamp."
        )

    record: dict[str, Any] = {
        "file_id": candidate.file_id,
        "recorded_at": recorded_at.strip(),
        "record_type": "source_file_classification",
        # status is execution state, not the outcome: 'classified' said what
        # happened, which action='classify' already says, and left no room for
        # whether it worked.
        "status": "success" if outcome.status == "classified" else "failed",
        "evidence": {
            "run_log_id": run_log_id,
        },
        # The path belongs on the file object, and a consumer reads it at the
        # same address on every record type. It is copied from the inventory
        # record this classification was built from, so no consumer has to
        # resolve lineage merely to learn where the file is.
        "file": _file_object(candidate, file_path=file_path),
        # file_id identifies the governed file occurrence; lineage identifies
        # the exact record this one was produced from -- the mutation the
        # selector returned, never the file id. file_id already says which
        # governed file this belongs to, so copying it here recorded nothing:
        # a file has many mutations and knowing the file does not say which of
        # them was read.
        "lineage": {
            "source_record_id": candidate.manifest_record.get("file_mutation_id"),
        },
        "classification": {
            "type": config.classification_type,
            "source_field": config.source_field,
        },
    }
    if outcome.status == "classified":
        expected = [variable.name for variable in config.variables]
        if list(outcome.values) != expected:
            raise ClassificationError(
                "Classified values must exactly match configured variable order."
            )
        record["classification"] = _classification_payload(
            config.classification_type, config.source_field, outcome.values,
            config.profile_key_fields)
        # What the operation produced. The classification itself is the
        # payload; this names the outcome a reader renders.
        record["result"] = "classified"
    elif outcome.status == "rejected":
        # No result, and so no mutation. log_file_manifest_record writes a
        # mutation only for the 'classified' outcome, and the rejection's
        # reason_code and prose are recorded on the evidence record below,
        # which is their only home.
        pass
    else:
        raise ClassificationError(
            f"Unsupported classification outcome status '{outcome.status}'."
        )
    # Classification is the operation. The move to processing that follows is
    # the same operation's second record, and states the same value.
    record["producer"] = {
        # The application recording it: the runtime context's, as inventory's.
        "application": application,
        "operation": "classification",
    }
    return record


def run_source_file_classification(
    ctx: Any,
    run_log: Any,
    process_config: Any,
    run: RunContext,
) -> SourceClassificationBatchResult:
    """Classify one scope; at the apply boundary, through ``Transform(classify)``.

    Selection is this step's: each source's ``file_selection`` routine names
    the files. A dry run classifies the selected rows in memory and persists
    nothing. An applied run opens each selected row at exactly the selected
    mutation and applies ``Transform(classify)`` to that DataFile, which
    records, plans, moves and returns the file in processing.

    Stops at the first durability failure, as the legacy persistence did.
    """
    candidates = load_source_classification_candidates(ctx, run_log, process_config)
    if not run.apply:
        return classify_source_candidates(candidates)

    entries = _source_entries(process_config)
    classified = 0
    for candidate in candidates:
        if _classify_selected(ctx, run_log, entries[candidate.classification.name],
                              candidate):
            classified += 1
    return SourceClassificationBatchResult(
        outcomes=(),
        candidates=len(candidates),
        classified=classified,
        rejected=len(candidates) - classified,
    )


def _classify_selected(
    ctx: Any, run_log: Any,
    entry: Any,
    candidate: SourceClassificationCandidate,
) -> bool:
    """Apply ``Transform(classify)`` to one selected file; nothing else.

    Returns whether it was classified. The step records nothing itself (rule
    78, backlog 624): the classify kind classifies the file from its own
    DataFile, records the outcome -- a rejection included -- and raises a
    terminal rejection as ``ClassificationRejected``, which the common
    execution path kicks out. The step counts it and continues.

    Raises:
        ClassificationError: If the candidate carries no governed object, which
            load_source_classification_candidates always attaches.
    """
    selected = candidate.governed
    if selected is None:
        raise ClassificationError(
            f"Source classification candidate {candidate.file_id!r} was selected "
            "with no governed object to open."
        )
    try:
        return bool(
            Transform(
                values={"source": entry, "source_record_type": candidate.source_record_type},
                selected="classify",
            ).resolve(ctx).apply(selected.data_file())
        )
    except ClassificationRejected:
        return False


def _record_rejection(
    ctx: Any, run_log: Any,
    outcome: SourceClassificationOutcome,
) -> None:
    """Persist a rejection: evidence, then the lifecycle append.

    The classify kind's (rule 78): called by ClassifyTransform._record_failure
    after the kickout. A rejection produces no result, so the append writes no
    mutation; the reason lives on the evidence record, which is its only home.
    """
    run_log_id = _record_classification_evidence(run_log, outcome)
    manifest_record = serialize_source_file_classification_record(
        outcome,
        run_log_id=run_log_id,
        recorded_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        application=str(getattr(ctx, "app_name", "") or ""),
    )
    try:
        log_file_manifest_record(ctx, manifest_record)
    except FileManifestError as exc:
        raise ClassificationError(
            "Source classification lifecycle record could not be appended to "
            f"the file manifest: {exc}"
        ) from exc


def _source_entries(process_config: Any) -> dict[str, Any]:
    """Each enabled classification source's declaration, by name.

    The classify Transform holds the DECLARATION, which is data, and validates
    it itself exactly as ``resolve_classification_source_configs`` already has
    for the whole scope.
    """
    entries: dict[str, Any] = {}
    for entry in _get(process_config, "sources", None) or []:
        name = str(_get(entry, "name", "") or "").strip()
        if name and _get(entry, "enabled", False) is True:
            entries[name] = entry
    return entries


def _record_classification_evidence(
    run_log: Any,
    outcome: SourceClassificationOutcome,
) -> int:
    """Commit one classification outcome's run-log evidence; return its id.

    The evidence the legacy persistence wrote, field for field, including the
    selected row's ``source_record_type``.

    Raises:
        ClassificationError: If the evidence did not commit; nothing is
            recorded in the file manifest without it.
    """
    candidate = outcome.candidate
    config = candidate.classification
    evidence_fields: dict[str, Any] = {
        "classification_type": config.classification_type,
        "file_id": candidate.file_id,
        "file_extension": _file_extension(candidate),
        "source_record_type": candidate.source_record_type,
        "source_record_id": candidate.manifest_record.get("file_mutation_id"),
        "source_field": config.source_field,
        "status": outcome.status,
    }
    if outcome.status == "classified":
        evidence_fields["values"] = dict(outcome.values)
    else:
        evidence_fields["reason_code"] = outcome.reason_code
        evidence_fields["reason"] = outcome.reason
    run_log_id = log_run_record(run_log,
        "SOURCE_FILE_CLASSIFICATION",
        message=(
            f"Source file classification {outcome.status} for "
            f"inventory record {candidate.file_id}."
        ),
        **evidence_fields,
    )
    if run_log_id is None:
        raise ClassificationError(
            "Source classification evidence did not commit; the file manifest "
            "was not modified."
        )
    return run_log_id


@file_transform("classify", fields=("source", "source_record_type"), required=("source",))
class ClassifyTransform(FileTransform):
    """Classify one governed file, and route it to processing.

        DataFile (selected state) -> classify -> DataFile (in processing)

    ``source`` is the classification source's DECLARATION, validated here as
    the step validates it. ``source_record_type`` is the selected row's record
    type, carried for the evidence and never a DataFile property.

    A file of no registered format (an ``UntypedFile``) is a rejection,
    ``no_registered_data_file``; the criterion is the DataFile registry, never a
    list of suffixes. A rejection is recorded, and -- unless the source retries
    its rejects -- raised as ``ClassificationRejected``, so the common execution
    path kicks the original out.
    """

    file_failures = (ClassificationRejected,)

    def __init__(self, ctx: Any, *, source: Any, source_record_type: Any = None) -> None:
        """Validate the source declaration this transform classifies by.

        Raises:
            ClassificationError: If the declaration is invalid.
        """
        name = str(_get(source, "name", "") or "").strip()
        self._ctx = ctx
        self._config = _classification_source_config(source, name)
        self._source_record_type = source_record_type
        self._rejection: SourceClassificationOutcome | None = None

    def _record_failure(self, data_file: DataFile, error: BaseException) -> None:
        """After the kickout: record the terminal rejection, evidence then the
        lifecycle append, exactly as an inline rejection is recorded."""
        if self._rejection is not None:
            _record_rejection(self._ctx, bound_run_log(), self._rejection)

    def _apply(self, data_file: DataFile) -> tuple[DataFile, ...]:
        """Classify the file; record, plan, move and return it.

        Returns:
            The file in processing (or where it was, when the source declares
            no processing route), carrying its classification and base_path --
            or nothing, for a rejection by a source that retries its rejects.

        Raises:
            ClassificationRejected: For a rejection by a source that does not
                retry its rejects; the original is kicked out, then the
                rejection is recorded (:meth:`_record_failure`).
            ClassificationError: At the first durability failure: evidence that
                did not commit, a file that cannot be routed, a record that could
                not be appended, or a move that did not land on the destination
                the record names.
        """
        candidate = _candidate(self._config, _selected_record(data_file, self._source_record_type))
        if not isinstance(data_file, UntypedFile):
            outcome = _classify_candidate(candidate)
        else:
            outcome = SourceClassificationOutcome(
                candidate=candidate,
                status="rejected",
                values={},
                reason_code="no_registered_data_file",
                reason=f"No registered DataFile type: {data_file.refusal}",
            )
        classified = outcome.status == "classified"
        # A TERMINAL REJECTION IS RECORDED AFTER THE KICKOUT (rule 75): raised
        # here unrecorded, and written by _record_failure once the common path
        # has moved the original. A retried rejection is not kicked out, so it
        # is recorded inline below like any outcome.
        if not classified and not self._config.retry_rejects:
            self._rejection = outcome
            raise ClassificationRejected(
                f"Source classification rejected '{data_file.path.name}' "
                f"({outcome.reason_code}): {outcome.reason}"
            )
        run_log_id = _record_classification_evidence(bound_run_log(), outcome)

        governed = data_file
        if classified:
            classification = _classification_payload(
                self._config.classification_type, self._config.source_field,
                outcome.values, self._config.profile_key_fields,
            )
            governed = _restated(
                data_file, classification=classification,
                base_path=classification.get("base_path"),
            )

        # THE FILE DOES NOT MOVE UNTIL ITS RECORD IS WRITTEN.
        #
        # The record names where the file will be, so the destination has to be
        # known before the move rather than reported by it: the move's own
        # side-effect-free plan runs every routing validation and writes nothing.
        #
        # It used to move first, because that was the shortest way to obtain the
        # path. The cost was paid on 2026-09-18: the move succeeded, the mutation
        # write failed, and the file sat in processing while the manifest said
        # inbox -- a divergence no query could see, recovered by hand.
        move = None
        planned: Path | None = None
        if classified and self._config.processing is not None:
            move = Transform(
                values={
                    "role": "processing",
                    "route": self._config.processing,
                    # Classification is the operation; the move to processing is
                    # what it does after classifying. One operation, two records.
                    "operation": "classification",
                },
                selected="move",
            ).resolve(self._ctx)
            try:
                planned = move.plan(governed)
            except (FileRoutingError, ConfigError, ValueError) as exc:
                raise ClassificationError(
                    f"Classified source file could not be routed to processing: {exc}"
                ) from exc

        manifest_record = serialize_source_file_classification_record(
            outcome,
            run_log_id=run_log_id,
            recorded_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            file_path=planned,
            application=str(getattr(self._ctx, "app_name", "") or ""),
        )
        try:
            manifest_record_id = log_file_manifest_record(self._ctx, manifest_record)
        except FileManifestError as exc:
            raise ClassificationError(
                "Source classification lifecycle record could not be appended to "
                f"the file manifest: {exc}"
            ) from exc
        if not classified:
            return ()
        recorded = _restated(governed, file_mutation_id=manifest_record_id)

        # Recorded; now move. A failure here leaves the file where it has been
        # all along -- in the inbox -- and two records standing together say so:
        # this classification mutation, naming the destination, and the
        # failed-move evidence routing writes itself.
        #
        # The move is handed the SELECTED state, not the one just recorded: the
        # move evidence's source_record_id is the mutation it moved from, and
        # legacy classification names the selected row there. The moved file
        # takes its own new identity from routing either way.
        if move is not None:
            try:
                (moved,) = move.apply(governed)
            except (FileRoutingError, ConfigError, ValueError) as exc:
                raise ClassificationError(
                    f"Source classification was recorded against '{planned}' and "
                    f"the file did not move, so it remains at '{data_file.path}': {exc}"
                ) from exc
            # The invariant the ordering rests on: the record names the PLANNED
            # path, and nothing guarantees the move agrees, because collision
            # state can change between the two.
            if moved.path != planned:
                raise ClassificationError(
                    f"Source classification routed the file to '{moved.path}' after "
                    f"recording it against '{planned}'. The manifest and the "
                    "filesystem no longer agree."
                )
            recorded = moved

        _store_data_profile_key(self._ctx, outcome)
        return (recorded,)


def _selected_record(data_file: DataFile, source_record_type: Any) -> dict[str, Any]:
    """The selected state, in the shape the classification mechanics read.

    The DataFile's identity and the path it was opened at, at the addresses a
    source's ``source_field`` may name, plus the selected row's record type,
    carried in from the step.
    """
    path = str(data_file.path)
    return {
        "file_manifest_id": data_file.file_manifest_id,
        "file_mutation_id": data_file.file_mutation_id,
        "record_type": source_record_type,
        "path": path,
        "file_name": data_file.path.name,
        "file": {"path": path, "file_name": data_file.path.name},
    }


def _restated(data_file: DataFile, **identity: Any) -> DataFile:
    """The same file, as the same DataFile subtype, with part of its governed
    identity restated."""
    held = {
        "file_manifest_id": data_file.file_manifest_id,
        "file_mutation_id": data_file.file_mutation_id,
        "classification": data_file.classification,
        "base_path": data_file.base_path,
    }
    held.update(identity)
    return type(data_file)(
        data_file.path, encoding=data_file.encoding, **held, **data_file.settings,
    )


def _store_data_profile_key(ctx: Any, outcome: SourceClassificationOutcome) -> None:
    """Record what this file now groups as, on the file.

    Written after the classification mutation and never instead of it: the
    mutation stays the record of the classification operation, unchanged. This
    is current manifest state derived from it -- what the file groups as *now*
    -- which is why it belongs on control.file_manifest rather than only inside
    a mutation payload that a later classification would supersede.

    Classification owns this. A profiler reads the stored key rather than
    rebuilding the grouping from classification values of its own, so there is
    one construction and one place it can change.

    Does nothing when the source declares no key fields, or when the file was
    not classified: a rejected candidate has no values to group by, and writing
    a key for one would claim a grouping the classification never made.
    """
    key_fields = outcome.candidate.classification.profile_key_fields
    if not key_fields or outcome.status != "classified":
        return

    file_id = outcome.candidate.file_id
    if file_id is None:
        raise ClassificationError(
            "A classified file carries the identity the control database "
            "minted, and this outcome carries none, so its data_profile_key "
            "has nothing to be written against."
        )

    control = getattr(ctx, "shared_control", None)
    if control is None:
        raise ClassificationError(
            "The governed file manifest is held in the control database, and "
            "this context exposes no shared Control to record the "
            "data_profile_key through."
        )

    control.update_file_manifest(
        int(file_id),
        data_profile_key=build_data_profile_key(key_fields, outcome.values),
    )


def _file_object(
    candidate: SourceClassificationCandidate,
    *,
    file_path: Path | str | None = None,
) -> dict[str, Any]:
    """Return the classification record's file object.

    ``path`` is either the processing path supplied after a successful move or
    the selected inventory record's path copied verbatim. It is omitted only
    when neither boundary supplies one.
    """
    file_object: dict[str, Any] = {}
    path = (
        str(file_path)
        if file_path is not None
        else record_field(candidate.manifest_record, "path")
    )
    if isinstance(path, str) and path.strip():
        file_object["path"] = path
        file_object["file_name"] = Path(path).name
    file_object["base_name"] = _base_name(candidate)
    file_object["file_extension"] = _file_extension(candidate)
    return file_object


def _base_name(candidate: SourceClassificationCandidate) -> str:
    """Return the observed filename without its final extension."""
    file_name = record_field(candidate.manifest_record, "file_name")
    source_name = (
        file_name if isinstance(file_name, str) and file_name else candidate.value
    )
    if not isinstance(source_name, str) or not source_name:
        return ""
    return Path(source_name).stem


def _file_extension(candidate: SourceClassificationCandidate) -> str:
    """Return the observed final filename suffix without inferring content type."""
    file_name = record_field(candidate.manifest_record, "file_name")
    source_name = (
        file_name if isinstance(file_name, str) and file_name else candidate.value
    )
    if not isinstance(source_name, str) or not source_name:
        return ""
    return Path(source_name).suffix.removeprefix(".").lower()


def _classification_payload(
    classification_type: str,
    source_field: str,
    values: Mapping[str, Any],
    key_fields: Sequence[str],
) -> dict[str, Any]:
    """Return one classification as every reader of it expects to find it.

    base_path is the path authority, not something the file was classified as.
    It is captured by the same regex -- the only thing that sees the hierarchy
    the file was found in -- and lifted out of ``values`` so a destination reads
    it without interpreting a semantic value. What the file *is* stays in
    values; where its lifecycle is rooted sits beside them.

    key_fields names which of those values decided the grouping. It is part of
    how this classification was made, not a separate fact about the file, so it
    is recorded with the operation rather than stamped on the manifest beside
    the key. A profiler establishing a file type reads it back from here: the
    field *names* have no other home, where the key's *value* is already on
    control.file_manifest.

    Omitted, like base_path, when there is none. A source with no ``profile``
    block groups nothing, and an empty list would claim it was grouped by
    nothing rather than that it was never grouped.

    Built here rather than at each place that needs one: the record written to
    the manifest and the reference handed to routing are the same
    classification, and two builders would let them disagree about it.
    """
    remaining = dict(values)
    base_path = str(remaining.pop("base_path", "") or "").strip()
    payload: dict[str, Any] = {
        "type": classification_type,
        "source_field": source_field,
        "values": remaining,
    }
    if base_path:
        payload["base_path"] = base_path
    if key_fields:
        payload["key_fields"] = list(key_fields)
    return payload


def _classify_candidate(
    candidate: SourceClassificationCandidate,
) -> SourceClassificationOutcome:
    """Apply one compiled regex and validate its named captures."""
    config = candidate.classification
    if candidate.status == "rejected":
        return SourceClassificationOutcome(
            candidate=candidate,
            status="rejected",
            values={},
            reason_code=candidate.reason_code,
            reason=candidate.reason,
        )

    value = candidate.value
    if value is None:
        return SourceClassificationOutcome(
            candidate=candidate,
            status="rejected",
            values={},
            reason_code="invalid_source_field",
            reason="Classification candidate has no source value.",
        )

    match = config.path_regex.fullmatch(value)
    if match is None:
        return SourceClassificationOutcome(
            candidate=candidate,
            status="rejected",
            values={},
            reason_code="path_regex_mismatch",
            reason="Configured classification regex did not match the source field.",
        )

    captured = match.groupdict()
    validated, reason_code, reason = _validate_captured_variables(
        config.variables,
        captured,
    )
    ordered = {
        variable.name: validated.get(variable.name) for variable in config.variables
    }
    if reason_code is not None:
        return SourceClassificationOutcome(
            candidate=candidate,
            status="rejected",
            values=ordered,
            reason_code=reason_code,
            reason=reason,
        )

    return SourceClassificationOutcome(
        candidate=candidate,
        status="classified",
        values=ordered,
    )


def _candidate(
    config: FileClassificationSourceConfig,
    manifest_record: Mapping[str, Any],
) -> SourceClassificationCandidate:
    """Prepare one selected row without applying its regex.

    Every value comes from the selector's declared row contract, so the file's
    identity travels selection -> candidate -> classification without a second
    lookup and without this module knowing how the row was chosen.
    """
    record_type = str(manifest_record.get("record_type") or "")
    found = manifest_record.get("file_manifest_id")
    file_id = found if is_governed_file_id(found) else None

    raw_value = record_field(manifest_record, config.source_field)
    if raw_value is MISSING:
        return SourceClassificationCandidate(
            classification=config,
            manifest_record=manifest_record,
            source_record_type=record_type,
            value=None,
            file_id=file_id,
            status="rejected",
            reason_code="missing_source_field",
            reason=(
                f"Manifest record is missing configured source field "
                f"'{config.source_field}'."
            ),
        )

    if not isinstance(raw_value, str) or not raw_value.strip():
        return SourceClassificationCandidate(
            classification=config,
            manifest_record=manifest_record,
            source_record_type=record_type,
            value=None,
            file_id=file_id,
            status="rejected",
            reason_code="invalid_source_field",
            reason=(
                f"Manifest source field '{config.source_field}' must be a "
                "non-empty string."
            ),
        )

    if file_id is None:
        # One identity, so one rejection. This was two -- a missing record_id
        # and a missing file_id -- because the candidate carried two fields
        # holding the same value.
        return SourceClassificationCandidate(
            classification=config,
            manifest_record=manifest_record,
            source_record_type=record_type,
            value=raw_value,
            file_id=None,
            status="rejected",
            reason_code="missing_file_id",
            reason=(
                "Manifest record carries no governed file id -- a positive "
                "file_manifest_id."
            ),
        )

    return SourceClassificationCandidate(
        classification=config,
        manifest_record=manifest_record,
        source_record_type=record_type,
        value=raw_value,
        file_id=file_id,
        status="ready",
    )


def build_data_profile_key(
    key_fields: Sequence[str],
    values: Mapping[str, Any],
) -> str:
    """The grouping identity of one classified file.

    Files holding the same kind of data share a key however often they are
    delivered and whatever they are called. ``profile.key_fields`` defines that
    grouping completely -- both which classified values take part and the order
    they appear in -- so this reads the named fields in the order it was given
    them and joins their values with ``|``:

        key_fields: [first, second]
        first="alpha", second="beta"
        -> "alpha|beta"

    The field names are not stored. Position carries the meaning, and position
    comes from the configuration.

    Determinism is the configuration's: the same declaration and the same values
    give the same key, every time. What cannot reach the key is the order the
    classification result happens to be built in, because the fields are taken
    by name rather than by position in the mapping.

    It reads nothing from the file, the manifest, the mutation or the run --
    only the named classification values -- which is what makes two deliveries
    of one group agree.

    Args:
        key_fields: The declared field names, in the order the YAML lists them.
        values: The classification result to take them from.

    Returns:
        The key: the named values, in that order, joined with ``|``.

    Raises:
        ClassificationError: If a named field has no value. A key built from a partial
            classification would group files that are not the same kind of data,
            silently and permanently.
    """
    selected: list[str] = []
    for field in key_fields:
        value = values.get(field)
        text = "" if value is None else str(value).strip()
        if not text:
            raise ClassificationError(
                f"Classification value '{field}' is required to build the "
                "data_profile_key and is missing or empty."
            )
        selected.append(text)
    return "|".join(selected)


def _classification_source_config(
    entry: Any,
    name: str,
) -> FileClassificationSourceConfig:
    """Validate and project one enabled classification source."""
    classification_type = _required_string(entry, "classification_type", name)
    retry_rejects = _get(entry, "retry_rejects", False)
    if not isinstance(retry_rejects, bool):
        raise ClassificationError(
            f"Source classification '{name}' retry_rejects must be a boolean."
        )
    # Both live in the selection block: which operation this source's work is
    # retrieved for, and which field of the governed file holds its path.
    file_selection = _required_mapping(entry, "file_selection", name)
    if _get(file_selection, "procedure", MISSING) is not MISSING:
        raise ClassificationError(
            f"Source classification '{name}' file_selection.procedure is retired "
            "(backlog 619): files are retrieved by file_selection.operation "
            "through the manifest retrieval routine. Replace 'procedure' with "
            "'operation'."
        )
    operation = _required_string(file_selection, "operation", name)
    source_field = _required_string(file_selection, "source_field", name)
    processing_value = _get(entry, "processing", MISSING)
    processing = (
        None
        if processing_value is MISSING
        else _required_string(entry, "processing", name)
    )
    variables = _variables(
        entry,
        name,
        config_kind="Source classification",
    )
    for variable in variables:
        if variable.source != "classification":
            raise ClassificationError(
                f"Source classification '{name}' variable '{variable.name}' "
                "must use source 'classification'."
            )

    classification = _required_mapping(entry, "classification", name)
    raw_regex = _required_string(classification, "path_regex", name)
    try:
        path_regex = re.compile(raw_regex)
    except re.error as exc:
        raise ClassificationError(
            f"Source classification '{name}' path_regex is invalid: {exc}"
        ) from exc

    _validate_named_groups(name, variables, path_regex)
    profile_key_fields = _profile_key_fields(classification, name, variables)
    return FileClassificationSourceConfig(
        name=name,
        enabled=True,
        classification_type=classification_type,
        retry_rejects=retry_rejects,
        operation=operation,
        source_field=source_field,
        variables=variables,
        path_regex=path_regex,
        processing=processing,
        profile_key_fields=profile_key_fields,
    )


def _profile_key_fields(
    classification: Any,
    name: str,
    variables: tuple[DiscoveryVariable, ...],
) -> tuple[str, ...]:
    """Which declared values form this source's data_profile_key.

    Optional. A source with no ``profile`` block groups nothing and its files
    keep a null key, which is what every source that has not asked for one
    relies on.

    Every named field must be a declared variable. Naming one that is not is a
    configuration error rather than a key quietly built from fewer values than
    intended -- and a key that is quietly narrower groups files that are not
    the same kind of data, which is the failure this whole increment exists to
    avoid.
    """
    profile = _get(classification, "profile", MISSING)
    if profile is MISSING or profile is None:
        return ()
    # Through _mapping, as every other block here is read: configuration
    # arrives as a Namespace at runtime and as a plain mapping in a test, and
    # only one of those is a Mapping.
    profile = _mapping(
        profile, f"Source classification '{name}' classification.profile")

    fields = profile.get("key_fields", MISSING)
    if not isinstance(fields, (list, tuple)) or not fields:
        raise ClassificationError(
            f"Source classification '{name}' classification.profile.key_fields "
            "must be a non-empty list of declared variable names."
        )

    declared = {variable.name for variable in variables}
    named: list[str] = []
    for field in fields:
        if not isinstance(field, str) or not field.strip():
            raise ClassificationError(
                f"Source classification '{name}' "
                "classification.profile.key_fields entries must be non-empty "
                "strings."
            )
        field = field.strip()
        if field not in declared:
            raise ClassificationError(
                f"Source classification '{name}' "
                f"classification.profile.key_fields names '{field}', which is "
                "not a declared variable."
            )
        if field in named:
            raise ClassificationError(
                f"Source classification '{name}' "
                f"classification.profile.key_fields names '{field}' twice."
            )
        named.append(field)
    return tuple(named)


def _validate_named_groups(
    name: str,
    variables: tuple[DiscoveryVariable, ...],
    path_regex: re.Pattern[str],
) -> None:
    """Require exact correspondence between declarations and named groups."""
    declared = {variable.name for variable in variables}
    captured = set(path_regex.groupindex)
    missing = sorted(declared - captured)
    extra = sorted(captured - declared)
    if not missing and not extra:
        return

    details: list[str] = []
    if missing:
        details.append(f"variables without named groups: {', '.join(missing)}")
    if extra:
        details.append(f"named groups without variables: {', '.join(extra)}")
    raise ClassificationError(
        f"Source classification '{name}' path_regex groups do not exactly match "
        f"its variables ({'; '.join(details)})."
    )


# ---------------------------------------------------------------------------
# Capture validation and config readers (copied from the legacy discovery)
# ---------------------------------------------------------------------------

_ALLOWED_SOURCES = ("path", "filename", "classification")
# Closed, deterministic format validators. Adding a format is a deliberate code
# change here; configuration cannot introduce one.
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_FORMAT_MATCHERS = {
    "mmmyy": f"(?:{'|'.join(_MONTHS)})[0-9]{{2}}",
}

_REJECT_FILENAME_MISMATCH = "filename_pattern_mismatch"
_REJECT_VALUE_NOT_ALLOWED = "value_not_allowed"
_REJECT_FORMAT_INVALID = "format_invalid"
_REJECT_MISSING_REQUIRED = "missing_required_variable"
_REJECT_EXCLUDED = "excluded"



@dataclass(frozen=True)
class DiscoveryVariable:
    """One declared discovery variable, in configured order."""

    name: str
    source: str
    required: bool
    values: tuple[str, ...] | None
    format: str | None

    @property
    def anchored(self) -> bool:
        """Return whether this variable proves its own deterministic boundary."""
        return bool(self.values) or bool(self.format)


def _variables(
    entry: Any,
    config_name: str,
    *,
    config_kind: str = "File discovery",
) -> tuple[DiscoveryVariable, ...]:
    """Return the ordered declared variables, validated independently."""
    raw = _get(entry, "variables", None)
    if not isinstance(raw, list):
        raise ClassificationError(
            f"{config_kind} '{config_name}' requires an ordered 'variables' list."
        )
    if not raw:
        raise ClassificationError(f"{config_kind} '{config_name}' declares no variables.")

    seen: set[str] = set()
    variables: list[DiscoveryVariable] = []
    for index, item in enumerate(raw):
        label = f"{config_name}.variables[{index}]"
        name = _required_string(item, "name", label)
        if name in seen:
            raise ClassificationError(f"{label} redeclares variable '{name}'.")
        seen.add(name)

        source = _required_string(item, "source", label)
        if source not in _ALLOWED_SOURCES:
            raise ClassificationError(
                f"{label} source must be one of {', '.join(_ALLOWED_SOURCES)}."
            )

        required = _get(item, "required", True)
        if not isinstance(required, bool):
            raise ClassificationError(f"{label} 'required' must be a boolean.")

        values = _get(item, "values", None)
        if values is not None:
            if not isinstance(values, list) or any(
                not isinstance(value, str) for value in values
            ):
                raise ClassificationError(
                    f"{label} 'values' must be a list of strings or null."
                )
            values = tuple(values)

        fmt = _get(item, "format", None)
        if fmt is not None:
            if not isinstance(fmt, str) or fmt not in _FORMAT_MATCHERS:
                raise ClassificationError(
                    f"{label} 'format' must be one of "
                    f"{', '.join(sorted(_FORMAT_MATCHERS))}."
                )

        variables.append(
            DiscoveryVariable(
                name=name,
                source=source,
                required=required,
                values=values,
                format=fmt,
            )
        )
    return tuple(variables)


def _validate_captured_variables(
    variables: tuple[DiscoveryVariable, ...],
    captured: Mapping[str, str | None],
) -> tuple[dict[str, str | None], str | None, str | None]:
    """Validate captured values in declaration order.

    Returns the values validated before any rejection plus a stable reason code
    and reason. This is the existing discovery validation contract factored
    into one reusable boundary for manifest-driven classification.
    """
    validated: dict[str, str | None] = {}
    for variable in variables:
        value = captured.get(variable.name)
        if value is None or (variable.required and value == ""):
            if variable.required:
                return (
                    validated,
                    _REJECT_MISSING_REQUIRED,
                    f"Required variable '{variable.name}' was not captured.",
                )
            validated[variable.name] = None
            continue
        if variable.values and value not in variable.values:
            return (
                validated,
                _REJECT_VALUE_NOT_ALLOWED,
                (
                    f"Variable '{variable.name}' value '{value}' is not a "
                    "configured value."
                ),
            )
        if variable.format and not re.fullmatch(
            _FORMAT_MATCHERS[variable.format], value
        ):
            return (
                validated,
                _REJECT_FORMAT_INVALID,
                (
                    f"Variable '{variable.name}' value '{value}' does not match "
                    f"format '{variable.format}'."
                ),
            )
        validated[variable.name] = value

    return validated, None, None


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    keys = getattr(value, "keys", None)
    if callable(keys):
        if name not in keys():
            return default
        try:
            return value[name]
        except (KeyError, TypeError):
            return getattr(value, name, default)
    getter = getattr(value, "get", None)
    if callable(getter):
        return getter(name, default)
    return getattr(value, name, default)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    keys = getattr(value, "keys", None)
    if callable(keys):
        return {str(key): getattr(value, key) for key in keys()}
    if hasattr(value, "__dict__"):
        return vars(value)
    raise ClassificationError(f"{label} must be a mapping.")


def _required_mapping(value: Any, key: str, label: str) -> Mapping[str, Any]:
    mapping = _mapping(value, label)
    if key not in mapping:
        raise ClassificationError(f"File discovery '{label}' is missing required '{key}'.")
    return _mapping(mapping[key], f"{label}.{key}")


def _required_string(value: Any, key: str, label: str) -> str:
    result = _get(value, key, None)
    if not isinstance(result, str) or not result.strip():
        raise ClassificationError(f"{label} requires non-empty string field '{key}'.")
    return result.strip()


def _required_bool(value: Any, key: str, label: str) -> bool:
    result = _get(value, key, None)
    if not isinstance(result, bool):
        raise ClassificationError(f"{label} requires boolean field '{key}'.")
    return result
