"""Manifest-authoritative rollback for one execution log.

The engine is deliberately neutral to workflows, pipelines, applications, and
installation-specific folder conventions. It selects immutable
``source_file_mutation`` records by ``evidence.run_log_id`` and delegates
operation-specific behavior to the compensation registry.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rey_lib.files.governed_file import FileId, is_governed_file_id
from rey_lib.files.file_utils import delete_file, run_artifact_path
from rey_lib.files.jsonl import JsonlReadError, read_jsonl_file, write_jsonl_file
from rey_lib.run import establish_run_identity
from rey_lib.logs import (
    FileManifestError,
    ProfileLibraryError,
    bind_run,
    bound_run_log,
    clear_run,
    file_manifest_session,
    log_file_manifest_record,
    log_run_complete,
    log_run_record,
    log_run_start,
    log_run_summary,
    resolve_profile_library_path,
)

MUTATION_RECORD_TYPE = "source_file_mutation"
ROLLBACK_RECORD_TYPE = "source_file_rollback"
INVENTORY_RECORD_TYPE = "source_file_inventory"
CLASSIFICATION_RECORD_TYPE = "source_file_classification"
RUN_ROLLBACK_RECORD_TYPE = "run_rollback"

# The manifest describes current governed state, so a rolled-back run's records
# are removed. These are the types a run introduces; the run log keeps the
# history permanently and is never modified.
_SELECTABLE_RECORD_TYPES = (
    INVENTORY_RECORD_TYPE,
    CLASSIFICATION_RECORD_TYPE,
    MUTATION_RECORD_TYPE,
)

class LogRunRollbackError(Exception):
    """Raised when a rollback request cannot be planned or governed safely."""


class SourceFileMutationEvidenceFailurePhase(str, Enum):
    """Provable acknowledgement phase for mutation-evidence failure."""

    RUN_LOG_NOT_COMMITTED = "run_log_not_committed"
    RUN_LOG_COMMITTED_COMPLETE_EVIDENCE_NOT_ACKNOWLEDGED = (
        "run_log_committed_complete_evidence_not_acknowledged"
    )


class SourceFileMutationEvidenceError(LogRunRollbackError):
    """Report mutation-evidence acknowledgement without inferring manifest state."""

    def __init__(
        self,
        message: str,
        *,
        phase: SourceFileMutationEvidenceFailurePhase,
        run_log_id: int | None,
    ) -> None:
        normalized_phase = SourceFileMutationEvidenceFailurePhase(phase)
        committed = (
            normalized_phase
            is SourceFileMutationEvidenceFailurePhase.RUN_LOG_COMMITTED_COMPLETE_EVIDENCE_NOT_ACKNOWLEDGED
        )
        if committed:
            if _optional_positive_int(run_log_id) is None:
                raise ValueError(
                    "The post-run-log mutation-evidence phase requires a positive "
                    "run_log_id."
                )
        elif run_log_id is not None:
            raise ValueError(
                "The pre-run-log mutation-evidence phase cannot carry a committed "
                "run-log reference."
            )

        super().__init__(message)
        self.phase = normalized_phase
        self.run_log_id = run_log_id

    @property
    def run_log_committed(self) -> bool:
        """Whether the run-log row is known to have committed."""
        return (
            self.phase
            is SourceFileMutationEvidenceFailurePhase.RUN_LOG_COMMITTED_COMPLETE_EVIDENCE_NOT_ACKNOWLEDGED
        )

    @property
    def manifest_record_id(self) -> None:
        """No manifest ID was acknowledged by a successful function return."""
        return None

    @property
    def complete_evidence_acknowledged(self) -> bool:
        """Failure never acknowledges the complete canonical evidence pair."""
        return False


class SourceFileMutationEvidenceResult(int):
    """Acknowledged manifest ID with its committed run-log reference.

    This remains an ``int`` for every existing caller while allowing consumers
    that need the complete acknowledged evidence pair to use its references
    without a manifest or run-log lookup.
    """

    def __new__(
        cls,
        manifest_record_id: int,
        *,
        run_log_id: int,
    ) -> SourceFileMutationEvidenceResult:
        value = _positive_int(manifest_record_id, "manifest_record_id")
        instance = int.__new__(cls, value)
        instance.run_log_id = _positive_int(
            run_log_id,
            "run_log_id",
        )
        return instance

    @property
    def manifest_record_id(self) -> int:
        return int(self)

    @property
    def complete_evidence_acknowledged(self) -> bool:
        return True


@dataclass(frozen=True)
class Compensation:
    """Registered validation and execution for one filesystem action."""

    action: str
    compensating_action: str
    validate: Callable[[Mapping[str, Any]], str | None]
    execute: Callable[[Mapping[str, Any]], dict[str, Any]]


_COMPENSATIONS: dict[str, Compensation] = {}


def register_file_compensation(
    action: str,
    *,
    compensating_action: str,
    validate: Callable[[Mapping[str, Any]], str | None],
    execute: Callable[[Mapping[str, Any]], dict[str, Any]],
    replace: bool = False,
) -> None:
    """Register one operation-specific compensation without changing the engine."""
    normalized = _non_empty(action, "action")
    compensation_name = _non_empty(
        compensating_action, "compensating_action"
    )
    if normalized in _COMPENSATIONS and not replace:
        raise LogRunRollbackError(
            f"Compensation is already registered for action '{normalized}'."
        )
    if not callable(validate) or not callable(execute):
        raise LogRunRollbackError(
            "Compensation validate and execute values must be callable."
        )
    _COMPENSATIONS[normalized] = Compensation(
        action=normalized,
        compensating_action=compensation_name,
        validate=validate,
        execute=execute,
    )


def unregister_file_compensation(action: str) -> None:
    """Remove a registered compensation, primarily for isolated extension tests."""
    _COMPENSATIONS.pop(str(action or "").strip().lower(), None)


def serialize_source_file_mutation(
    *,
    action: str,
    status: str,
    source_path: str = "",
    destination_path: str = "",
    recovery_path: str = "",
    previous_version_path: str = "",
    run_log_id: int,
    application_name: str = "",
    file_id: FileId | None = None,
    classification: Mapping[str, Any] | None = None,
    conversion: Mapping[str, Any] | None = None,
    reason_code: str = "",
    reason: str = "",
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Build one authoritative canonical filesystem-mutation manifest record.

    This function owns the shape of ``file``, ``rollback``, ``result``,
    ``producer``, and ``evidence``, and omits a section whose values are all
    absent rather than emitting it empty.

    ``conversion`` is the one section whose contents belong to the converting
    producer: it is written unchanged under that key and interpreted nowhere
    here. It is also the only thing a caller may place in the record wholesale
    — no input reaches the record root.
    """
    normalized_action = _non_empty(action, "action")
    normalized_status = _non_empty(status, "status")
    evidence_id = _positive_int(run_log_id, "run_log_id")

    # The file object describes the logical file's lifecycle state: its current
    # location, and the location it came from. A caller passes only the paths
    # its operation actually had, so an absent value is exactly the omission
    # the canonical layout requires — no file after a delete, no previous file
    # before a create.
    current_path = _path_text(destination_path)
    original_path = _path_text(source_path)
    file_object: dict[str, Any] = {}
    if current_path:
        file_object["path"] = current_path
    if original_path:
        file_object["original_path"] = original_path
    # Both locations name the same logical file, so its name and extension come
    # from whichever location the action left recorded.
    named_path = current_path or original_path
    if named_path:
        file_name = Path(named_path).name
        file_object["file_name"] = file_name
        file_object["base_name"] = Path(file_name).stem
        file_object["file_extension"] = (
            Path(file_name).suffix.removeprefix(".").lower()
        )

    # Compensation metadata stays separate from file identity.
    rollback_object: dict[str, Any] = {}
    recovery = _path_text(recovery_path)
    previous_version = _path_text(previous_version_path)
    if recovery:
        rollback_object["recovery_path"] = recovery
    if previous_version:
        rollback_object["previous_version_path"] = previous_version

    result_object: dict[str, Any] = {}
    if _path_text(reason_code):
        result_object["reason_code"] = _path_text(reason_code)
    if _path_text(reason):
        result_object["reason"] = _path_text(reason)

    record: dict[str, Any] = {
        "recorded_at": recorded_at or _timestamp(),
        "record_type": MUTATION_RECORD_TYPE,
        "action": normalized_action,
        "status": normalized_status,
    }
    if is_governed_file_id(file_id):
        # Flat identity leads the record, so file_id is placed before the
        # objects rather than appended after them.
        record = {"file_id": file_id, **record}
    record["evidence"] = {"run_log_id": evidence_id}
    record["file"] = file_object
    if classification is not None:
        # Classification was governed before this serialization boundary.  It
        # is preserved as supplied and is never resolved or interpreted here.
        record["classification"] = dict(classification)
    if rollback_object:
        record["rollback"] = rollback_object
    if conversion:
        # The converting producer owns what its conversion payload says. This
        # function neither reads nor interprets it; it only places it in the
        # one canonical section it is allowed to occupy.
        record["conversion"] = dict(conversion)
    if result_object:
        record["result"] = result_object
    record["producer"] = {"application": str(application_name or "")}
    return record


def serialize_run_rollback(
    *,
    rollback_run_id: str,
    original_run_id: str,
    status: str,
    records_removed: int,
    filesystem_operations_reversed: int,
    affected_file_ids: Sequence[FileId] = (),
    started_at: str,
    ended_at: str,
    failure_details: Sequence[Mapping[str, Any]] = (),
    application_name: str = "",
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Build the one summary record for an operator-initiated run rollback.

    Exactly one of these is written per rolled-back run, however many records
    the run touched. The per-record detail already exists in the original run
    log, which the rollback reads to reverse the run; copying every reversed
    action into new records would duplicate an audit trail in a store whose job
    is current state.

    This is not ``source_file_rollback``. That type is per-file compensation
    created during normal execution, it references the mutation it compensates,
    and the lifecycle projection consumes it. Its shape and meaning are
    unchanged by this contract.
    """
    normalized_status = _non_empty(status, "status")
    if normalized_status not in {"success", "partial_success", "failure"}:
        raise LogRunRollbackError(
            "run_rollback.status must be 'success', 'partial_success', or "
            "'failure'."
        )
    rollback_object: dict[str, Any] = {
        "rollback_run_id": _non_empty(rollback_run_id, "rollback_run_id"),
        "original_run_id": _non_empty(original_run_id, "original_run_id"),
        "records_removed": _count(records_removed, "records_removed"),
        "filesystem_operations_reversed": _count(
            filesystem_operations_reversed, "filesystem_operations_reversed"
        ),
        "affected_file_ids": list(affected_file_ids),
        "started_at": _non_empty(started_at, "started_at"),
        "ended_at": _non_empty(ended_at, "ended_at"),
    }
    if failure_details:
        # Every record that could not be reversed, so a partial outcome is
        # never reported as a bare count.
        rollback_object["failure_details"] = [
            dict(detail) for detail in failure_details
        ]

    return {
        "recorded_at": recorded_at or _timestamp(),
        "record_type": RUN_ROLLBACK_RECORD_TYPE,
        "status": normalized_status,
        "rollback": rollback_object,
        "producer": {"application": str(application_name or "")},
    }


def log_source_file_mutation(
    ctx: Any,
    *,
    action: str,
    status: str,
    source_path: Path | str = "",
    destination_path: Path | str = "",
    recovery_path: Path | str = "",
    previous_version_path: Path | str = "",
    application_name: str = "",
    file_id: FileId | None = None,
    classification: Mapping[str, Any] | None = None,
    conversion: Mapping[str, Any] | None = None,
    reason_code: str = "",
    reason: str = "",
    message: str = "",
    run_log_fields: Mapping[str, Any] | None = None,
) -> int:
    """Commit run evidence, then append its linked mutation manifest record.

    This is the shared producer boundary for governed filesystem operations.
    It deliberately records no inferred compensation data: callers must pass
    the exact paths made durable by the operation they performed.

    Every manifest field is a governed value forwarded to the serializer, so a
    caller cannot inject or replace a canonical section. ``run_log_fields``
    enriches the run-log record only and never reaches the manifest.
    """
    # The owner of the write, resolved rather than threaded. Governed file
    # operations run deep in utility code that holds no run log, which is what
    # the ambient binding exists for; every execution boundary binds one.
    run_log = bound_run_log()
    if run_log is None:
        raise SourceFileMutationEvidenceError(
            "A governed file mutation must be recorded against a run, and no "
            "run log is bound. Every execution boundary binds one around the "
            "work it owns; a mutation reaching here outside that scope has no "
            "run to be evidence of.",
            phase=SourceFileMutationEvidenceFailurePhase.RUN_LOG_NOT_COMMITTED,
            run_log_id=None,
        )
    extra_run_log_fields = dict(run_log_fields or {})
    normalized_action = _non_empty(action, "action")
    normalized_status = _non_empty(status, "status")
    run_log_id = log_run_record(run_log,
        "SOURCE_FILE_MUTATION",
        message=message,
        application_name=str(application_name or ""),
        action=normalized_action,
        status=normalized_status,
        source_path=_path_text(source_path),
        destination_path=_path_text(destination_path),
        recovery_path=_path_text(recovery_path),
        previous_version_path=_path_text(previous_version_path),
        **extra_run_log_fields,
    )
    if run_log_id is None:
        raise SourceFileMutationEvidenceError(
            "Source-file mutation run-log evidence did not commit; the file "
            "manifest was not modified.",
            phase=SourceFileMutationEvidenceFailurePhase.RUN_LOG_NOT_COMMITTED,
            run_log_id=None,
        )

    try:
        record = serialize_source_file_mutation(
            action=normalized_action,
            status=normalized_status,
            source_path=source_path,
            destination_path=destination_path,
            recovery_path=recovery_path,
            previous_version_path=previous_version_path,
            run_log_id=run_log_id,
            application_name=application_name,
            file_id=file_id,
            classification=classification,
            conversion=conversion,
            reason_code=reason_code,
            reason=reason,
        )
        manifest_record_id = log_file_manifest_record(ctx, record)
        return SourceFileMutationEvidenceResult(
            manifest_record_id,
            run_log_id=run_log_id,
        )
    except Exception as exc:
        message = str(exc)
        if isinstance(exc, FileManifestError):
            message = (
                f"{MUTATION_RECORD_TYPE} lifecycle record could not be appended "
                f"to the file manifest: {exc}"
            )
        raise SourceFileMutationEvidenceError(
            message,
            phase=(
                SourceFileMutationEvidenceFailurePhase.RUN_LOG_COMMITTED_COMPLETE_EVIDENCE_NOT_ACKNOWLEDGED
            ),
            run_log_id=run_log_id,
        ) from exc


def preview_log_run_rollback(ctx: Any, run_id: int) -> dict[str, Any]:
    """What a rollback of this run would reverse, changing nothing.

    Marking is what selects a rollback set, and marking is a write, so this
    asks what a request for this run *would* mark rather than performing one.
    The database answers with the request routine's own predicate, so a
    preview and its execution cannot disagree about the set.
    """
    control = _require_control(ctx)
    requestable = control.requestable_file_rollbacks(int(run_id), required=True)
    reversible, refused = _partition_by_reversibility(requestable)
    return {
        "operation": "log_run_rollback_preview",
        "run_id": int(run_id),
        "requestable_count": len(requestable),
        "reversible_count": len(reversible),
        "non_reversible": refused,
        # Stated rather than implied: a set with anything irreversible in it
        # reverses nothing, so a preview says so before an operator asks.
        "would_refuse": bool(refused),
    }


def rollback_log_run(
    ctx: Any,
    run_id: int,
    *,
    reason: str = "",
) -> dict[str, Any]:
    """Reverse one run's governed filesystem mutations.

    The database owns the rollback set. ``run_id`` scopes the *request*: it
    marks that run's mutations, and what is then reversed is whatever carries
    the request. Rollback is state on the mutation row -- ``rollback_request_in``
    and ``rollback_complete_in`` -- so there is no plan to build here, no
    selection to evaluate, and no separate evidence record to append.

    Audit goes to ``control.run_log`` through the run that asked for the
    rollback, like every other record this estate writes.
    """
    control = _require_control(ctx)
    run_log = bound_run_log()
    if run_log is None:
        raise LogRunRollbackError(
            "A rollback records into control.run_log, and no run log is bound. "
            "Every execution boundary binds one around the work it owns."
        )

    started_at = _timestamp()
    log_run_record(run_log, "INFO",
                   message=f"Rolling back run {int(run_id)}.",
                   reason=str(reason or ""))

    control.request_file_rollback(run_id=int(run_id), required=True)
    pending = control.pending_file_rollbacks(required=True)
    reversible, refused = _partition_by_reversibility(pending)

    if refused:
        # Nothing is reversed when the set contains something that cannot be.
        # A partial rollback leaves a run half-undone, which is worse than one
        # that refused and said why.
        actions = sorted({str(row["action"]) for row in refused})
        raise LogRunRollbackError(
            f"Run {int(run_id)} cannot be rolled back: "
            f"{', '.join(actions)} is not reversible. "
            "The forward operation preserved no state to restore from, so "
            "nothing was reversed."
        )

    succeeded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    affected_file_ids: set[FileId] = set()
    filesystem_reversals = 0

    for row in reversible:
        candidate = _reversal_candidate(row)
        compensation = _resolved_compensation(candidate)
        problem = compensation.validate(candidate)
        if problem is not None:
            failed.append(_failed_result(row, problem))
            continue
        try:
            outcome = compensation.execute(candidate)
        except OSError as exc:
            failed.append(_failed_result(row, str(exc)))
            continue

        control.complete_file_rollback(
            int(row["file_mutation_id"]), required=True)
        if is_governed_file_id(row.get("file_manifest_id")):
            affected_file_ids.add(int(row["file_manifest_id"]))
        if _is_filesystem_reversal(compensation, outcome):
            filesystem_reversals += 1
        succeeded.append({
            "file_mutation_id": int(row["file_mutation_id"]),
            "file_manifest_id": row.get("file_manifest_id"),
            "action": str(row["action"]),
            "compensating_action": compensation.compensating_action,
            **outcome,
        })
        log_run_record(run_log, "SOURCE_FILE_ROLLBACK",
                       message=(f"Reversed {row['action']} on file "
                                f"{row.get('file_manifest_id')}."),
                       file_id=row.get("file_manifest_id"),
                       status="success",
                       **outcome)

    status = _aggregate_status(len(succeeded), len(failed))
    summary = {
        "operation": "log_run_rollback",
        "run_id": int(run_id),
        "status": status,
        "filesystem_operations_reversed": filesystem_reversals,
        "succeeded_count": len(succeeded),
        "failed_count": len(failed),
        "failures": failed,
    }
    _write_run_rollback_summary(
        ctx, run_log, status=status,
        rollback_run_id=str(getattr(run_log, "run_id", "")),
        original_run_id=str(int(run_id)),
        records_removed=0,
        filesystem_operations_reversed=filesystem_reversals,
        affected_file_ids=sorted(affected_file_ids),
        started_at=started_at,
        failed=failed,
    )
    log_run_summary(run_log, summary)
    return {**summary, "reason": str(reason or ""),
            "succeeded": succeeded, "failed": failed}


def _require_control(ctx: Any) -> Any:
    """The Control a rollback reaches the governed file model through."""
    control = getattr(ctx, "shared_control", None)
    if control is None:
        raise LogRunRollbackError(
            "A rollback reads and writes the governed file model in the "
            "control database, and this context exposes no shared Control."
        )
    return control


def _partition_by_reversibility(
    pending: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Split pending rows into what can be reversed and what cannot.

    An action is reversible only when a compensation resolves for it, and one
    resolves only where the forward operation preserved enough state to
    reverse. ``delete`` and ``replace`` overwrite in place and preserve
    nothing, so neither is registered and neither is reversed.
    """
    reversible: list[Mapping[str, Any]] = []
    refused: list[Mapping[str, Any]] = []
    for row in pending:
        target = reversible if _is_reversible(row.get("action")) else refused
        target.append(row)
    return reversible, refused


def _is_reversible(action: Any) -> bool:
    """Whether a compensation resolves for this action."""
    return str(action or "") == "record_only" or str(action or "") in _COMPENSATIONS


def _reversal_candidate(row: Mapping[str, Any]) -> dict[str, Any]:
    """The paths a compensation works from, named as the executors expect.

    ``path`` is where the file is now; ``restore_to_path`` is where the file's
    own history says it belongs. Both come from the row, so nothing is
    reconstructed here.
    """
    return {
        "action": str(row.get("action") or ""),
        "current_path": _path_text(row.get("path")),
        "original_path": _path_text(row.get("restore_to_path")),
    }


# Logical compensation input -> (canonical object, canonical field). The two
# lifecycle locations are grouped under ``file`` and the two compensation
# locations under ``rollback``.
_RECORDED_PATHS: dict[str, tuple[str, str]] = {
    "current_path": ("file", "path"),
    "original_path": ("file", "original_path"),
    "recovery_path": ("rollback", "recovery_path"),
    "previous_version_path": ("rollback", "previous_version_path"),
}


def _record_path(record: Mapping[str, Any], name: str) -> str:
    """Resolve one recorded location from its canonical object."""
    object_name, field = _RECORDED_PATHS[name]
    grouped = record.get(object_name)
    if not isinstance(grouped, Mapping):
        return ""
    return _path_text(grouped.get(field))


def _is_filesystem_reversal(
    candidate: Mapping[str, Any],
    result: Mapping[str, Any],
) -> bool:
    """Whether reversing this record actually changed the filesystem.

    Inventory and classification records describe state without touching the
    filesystem. Neither does a reversal that found its work already done — the
    record is still removed, but nothing moved, and the summary must not claim
    otherwise.
    """
    if str(candidate.get("action")) == "record_only":
        return False
    return str(result.get("outcome") or "") not in {
        "already_absent",
        "already_restored",
        "unchanged",
        "frozen",
    }


def _write_run_rollback_summary(
    ctx: Any, run_log,
    *,
    status: str,
    rollback_run_id: str,
    original_run_id: str,
    records_removed: int,
    filesystem_operations_reversed: int,
    affected_file_ids: Sequence[FileId],
    started_at: str,
    failed: Sequence[Mapping[str, Any]],
) -> int | None:
    """Append the one summary record for this rollback.

    Written on every outcome, including failure, because an operator needs to
    see that a rollback was attempted and what it managed to do. A summary that
    cannot itself be appended is reported rather than raised: the reversals it
    describes have already happened, and losing the record of them would be
    worse than a rollback whose summary is missing.
    """
    record = serialize_run_rollback(
        rollback_run_id=rollback_run_id,
        original_run_id=original_run_id,
        status=status,
        records_removed=records_removed,
        filesystem_operations_reversed=filesystem_operations_reversed,
        affected_file_ids=affected_file_ids,
        started_at=started_at,
        ended_at=_timestamp(),
        failure_details=failed,
        application_name="rey_lib",
    )
    try:
        return log_file_manifest_record(ctx, record)
    except FileManifestError:
        return None


def _resolved_compensation(
    candidate: Mapping[str, Any],
) -> Compensation:
    """Return the compensation for one planned candidate.

    Dispatch is on what the record did. A plain record removal is resolved here
    rather than through the filesystem action registry.
    """
    action = str(candidate["action"])
    if action == "record_only":
        return Compensation(
            action="record_only",
            compensating_action="remove_record",
            validate=lambda _record: None,
            execute=lambda _record: {},
        )
    return _COMPENSATIONS[action]


def _validate_recorded_paths(
    record: Mapping[str, Any],
    record_type: str,
) -> None:
    """Require the canonical location objects and their recorded values.

    ``file`` is mandatory: every mutation leaves the logical file somewhere or
    took it from somewhere. ``rollback`` is optional because an action may
    carry no compensation metadata. A location the action never had is absent
    rather than empty, so only present fields are type-checked.
    """
    file_object = record.get("file")
    if not isinstance(file_object, Mapping):
        raise LogRunRollbackError(f"{record_type}.file must be an object.")
    rollback_object = record.get("rollback")
    if rollback_object is not None and not isinstance(rollback_object, Mapping):
        raise LogRunRollbackError(f"{record_type}.rollback must be an object.")
    for object_name, fields in (
        ("file", ("path", "original_path")),
        ("rollback", ("recovery_path", "previous_version_path")),
    ):
        grouped = record.get(object_name)
        if not isinstance(grouped, Mapping):
            continue
        for field in fields:
            if field in grouped and not isinstance(grouped[field], str):
                raise LogRunRollbackError(
                    f"{record_type}.{object_name}.{field} must be a string."
                )


def _validate_mutation_record(record: Mapping[str, Any]) -> None:
    """Reject malformed authoritative mutations before planning any work."""
    _positive_int(record.get("record_id"), "source_file_mutation.record_id")
    _non_empty(record.get("action"), "source_file_mutation.action")
    _non_empty(record.get("status"), "source_file_mutation.status")
    _validate_recorded_paths(record, MUTATION_RECORD_TYPE)
    evidence = record.get("evidence")
    if not isinstance(evidence, Mapping):
        raise LogRunRollbackError(
            "source_file_mutation.evidence must be an object."
        )
    _positive_int(
        evidence.get("run_log_id"),
        "source_file_mutation.evidence.run_log_id",
    )


def _validate_move(record: Mapping[str, Any]) -> str | None:
    return _require_paths(record, "original_path", "current_path")


def _execute_move(record: Mapping[str, Any]) -> dict[str, Any]:
    current = Path(_record_path(record, "current_path"))
    original = Path(_record_path(record, "original_path"))
    outcome = _move_exact(current, original)
    return {
        "from_path": str(current),
        "to_path": str(original),
        "outcome": outcome,
    }


def _validate_create(record: Mapping[str, Any]) -> str | None:
    return _require_paths(record, "current_path")


def _execute_create(record: Mapping[str, Any]) -> dict[str, Any]:
    created = Path(_record_path(record, "current_path"))
    if not created.is_file():
        # The point of reversing a create is that the file is gone. It is.
        return {"deleted_path": str(created), "outcome": "already_absent"}
    created.unlink()
    return {"deleted_path": str(created), "outcome": "deleted"}


def _move_exact(source: Path, destination: Path) -> str:
    """Put the file back, or report that it is already back.

    Reversal is judged by the end state, not by the act. A rollback that is run
    twice finds the first one's work already done, and that is the goal
    reached, not a failure: treating it as one leaves the record behind
    forever, describing a state that no longer exists.

    The file being in neither place is a real failure. Nothing can be restored
    and nothing may be assumed.
    """
    if not source.is_file():
        if destination.is_file():
            return "already_restored"
        raise FileNotFoundError(f"Recovery source is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    return "restored"


def _require_paths(
    record: Mapping[str, Any],
    *names: str,
) -> str | None:
    missing = [name for name in names if not _record_path(record, name)]
    if missing:
        return f"missing recorded compensation field(s): {', '.join(missing)}"
    return None


def _failed_result(
    original: Mapping[str, Any],
    reason: str,
) -> dict[str, Any]:
    return {
        "original_manifest_record_id": original.get("record_id"),
        "action": original.get("action"),
        "failure_reason": reason,
    }


def _aggregate_status(succeeded: int, failed: int) -> str:
    if failed and succeeded:
        return "partial_success"
    if failed:
        return "failure"
    return "success"


def _path_text(value: Any) -> str:
    return str(value or "").strip()


def _count(value: Any, field: str) -> int:
    """Validate one non-negative count for the rollback summary."""
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise LogRunRollbackError(f"{field} must be an integer.") from exc
    if number < 0:
        raise LogRunRollbackError(f"{field} must not be negative.")
    return number


def _non_empty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LogRunRollbackError(f"{field} must be a non-empty string.")
    return value.strip().lower()


def _positive_int(value: Any, field: str) -> int:
    parsed = _optional_positive_int(value)
    if parsed is None:
        raise LogRunRollbackError(f"{field} must be a positive integer.")
    return parsed


def _optional_positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


register_file_compensation(
    "move",
    compensating_action="move_back",
    validate=_validate_move,
    execute=_execute_move,
)
register_file_compensation(
    "create",
    compensating_action="delete_created_file",
    validate=_validate_create,
    execute=_execute_create,
)
# A governed file that was present and is now absent. An ordinary lifecycle
# fact, not a compensation: the run did not remove the file, it recorded that
# the file had gone -- externally, manually, or by something outside this
# system. The action deliberately does not say which.
#
# `delete` is not this. That action means the system deleted the file and can
# put it back, which is why it compensates to restore_recoverable_file.
#
# Rolling this back removes the record and touches nothing on disk, the same
# shape record_only uses. There is no file to restore: undoing the run that
# noticed a disappearance cannot un-disappear the file, and a compensation that
# tried would be inventing a file it never had.
register_file_compensation(
    "disappear",
    compensating_action="remove_record",
    validate=lambda _record: None,
    execute=lambda _record: {},
)


# ---------------------------------------------------------------------------
# The pending rollback service
# ---------------------------------------------------------------------------


def run_pending_file_rollbacks(ctx: Any) -> dict[str, Any]:
    """Reverse every mutation currently marked for rollback.

    The pending rows are the queue. Each is reversed on its own and completed
    the moment its inverse succeeds, so a run that dies partway leaves exactly
    the unfinished rows pending and the next run continues them. Nothing is
    inserted and nothing is deleted: the mutation row that recorded the change
    also records that it was reversed.

    Targets are never derived from the filesystem. ``restore_to_path`` comes
    from the file's own history -- the path of the previous mutation that has
    not itself been rolled back -- because current location is whatever the
    newest surviving mutation says.

    Returns
    -------
    dict[str, Any]
        ``reversed`` and ``failed`` counts, and the failures with their reasons.
    """
    from rey_lib.files.manifest import FileManifest

    control = getattr(ctx, "shared_control", None)
    if control is None:
        raise LogRunRollbackError(
            "Rollback is recorded in the control database, and this context "
            "exposes no shared Control to reach it through."
        )
    manifest = FileManifest(control)

    reversed_count = 0
    failures: list[dict[str, Any]] = []
    for row in manifest.pending_rollbacks():
        mutation_id = row.get("file_mutation_id")
        record = _pending_as_compensation_record(row)
        try:
            compensation = _resolved_compensation(record)
            refusal = compensation.validate(record)
            if refusal:
                raise LogRunRollbackError(refusal)
            compensation.execute(record)
        except Exception as exc:  # noqa: BLE001 — one failure must not stop the rest
            # The row stays pending. It is picked up again next time, which is
            # the whole reason the queue lives on the rows.
            failures.append({"file_mutation_id": mutation_id, "reason": str(exc)})
            continue
        manifest.complete_rollback(int(mutation_id))
        reversed_count += 1

    return {
        "reversed": reversed_count,
        "failed": len(failures),
        "failures": failures,
    }


def _pending_as_compensation_record(row: Mapping[str, Any]) -> dict[str, Any]:
    """Present one pending mutation row in the shape a compensation reads.

    The compensations already know every inverse; what they want is the
    canonical ``file`` object. ``original_path`` is the resolved restore target,
    not a stored field -- for a mutation with no surviving predecessor it is
    absent, and the action's own inverse decides what that means.
    """
    rollback_payload = row.get("rollback")
    return {
        "record_type": row.get("record_type"),
        "action": row.get("action"),
        "status": row.get("status"),
        "file": {
            "path": row.get("path"),
            "original_path": row.get("restore_to_path"),
        },
        "rollback": rollback_payload if isinstance(rollback_payload, Mapping) else {},
    }
