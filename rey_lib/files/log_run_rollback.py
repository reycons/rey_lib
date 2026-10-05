"""Manifest-authoritative rollback for one execution log.

The engine is deliberately neutral to workflows, pipelines, applications, and
installation-specific folder conventions. It selects immutable
``source_file_mutation`` records by ``evidence.run_log_id`` and delegates
operation-specific behavior to the compensation registry.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rey_lib.files.governed_file import FileId, is_governed_file_id
from rey_lib.errors.error_utils import AppError
from rey_lib.files.file_utils import delete_file, run_artifact_path
from rey_lib.files.jsonl import JsonlReadError, read_jsonl_file, write_jsonl_file
from rey_lib.run import establish_run_identity
from rey_lib.logs import (
    FileManifestError,
    get_logger,
    ProfileLibraryError,
    bind_run,
    bound_run_log,
    clear_run,
    file_manifest_session,
    log_file_manifest_record,
    log_run_complete,
    log_run_record,
    log_run_start,
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

class LogRunRollbackError(AppError):
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
                raise LogRunRollbackError(
                    "The post-run-log mutation-evidence phase requires a positive "
                    "run_log_id."
                )
        elif run_log_id is not None:
            raise LogRunRollbackError(
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
    source_record_id: int | None = None,
    conversion: Mapping[str, Any] | None = None,
    operation: str = "",
    reason: str = "",
    recorded_at: str | None = None,
    transform_id: int | None = None,
    transform_snapshot: Mapping[str, Any] | None = None,
    destination: Mapping[str, Any] | None = None,
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

    # The outcome this operation produced. It is a single canonical name and
    # is stored as one: it used to sit under a "reason" key, which asked a
    # different question than the value ever answered.
    outcome = _path_text(reason)

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
    if source_record_id is not None:
        # The prior record this mutation was produced from, as the step's own
        # selector returned it. file_manifest_id says which governed file this
        # belongs to; this says which record was consumed to make it, and the
        # two are never the same fact. Placed, never resolved: what it points
        # at is the writer's to know.
        record["lineage"] = {"source_record_id": int(source_record_id)}
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
    # WHAT THE EXECUTION WAS, as its Transform and Destination stated it: the
    # saved setting, the definition that ran, and where it wrote. Placed as
    # given, like conversion; a mutation that executed no transform has none.
    if transform_id is not None:
        record["transform_id"] = int(transform_id)
    if transform_snapshot is not None:
        record["transform_snapshot"] = dict(transform_snapshot)
    if destination is not None:
        record["destination"] = dict(destination)
    if outcome:
        record["result"] = outcome
    # producer identifies what produced the mutation, not merely which
    # application was running: the operation is the act that caused it. action
    # says what happened to the file; this says which operation did it. A
    # consumer asking "which operation produced this row" reads one field
    # instead of guessing between conversion.operator and result.reason.
    record["producer"] = {"application": str(application_name or "")}
    if _path_text(operation):
        record["producer"]["operation"] = _path_text(operation)
    return record


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
    source_record_id: int | None = None,
    conversion: Mapping[str, Any] | None = None,
    operation: str = "",
    reason: str = "",
    message: str = "",
    run_log_fields: Mapping[str, Any] | None = None,
    transform_id: int | None = None,
    transform_snapshot: Mapping[str, Any] | None = None,
    destination: Mapping[str, Any] | None = None,
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
            source_record_id=source_record_id,
            conversion=conversion,
            operation=operation,
            reason=reason,
            transform_id=transform_id,
            transform_snapshot=transform_snapshot,
            destination=destination,
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


def _reversal_candidate(row: Mapping[str, Any]) -> dict[str, Any]:
    """One rollback row in the shape a compensation reads.

    The canonical grouped record, not a flat one: the validator resolves every
    location through its owning object -- ``current_path`` is ``file.path`` and
    ``original_path`` is ``file.original_path`` -- so a flat dict presents as a
    record missing both.

    ``path`` is where the file is now; ``restore_to_path`` is where the file's
    own history says it belongs. Both come from the row, so nothing is
    reconstructed here.

    ``rollback`` carries the compensation payload for the inverses that need
    one. The request dataset returns no such column, and the actions that would
    read it carry no command, so they are reversed by deleting the record and
    never reach a compensation.
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


# Logical compensation input -> (canonical object, canonical field). The two
# lifecycle locations are grouped under ``file`` and the two compensation
# locations under ``rollback``.
_RECORDED_PATHS: dict[str, tuple[str, str]] = {
    "current_path": ("file", "path"),
    "original_path": ("file", "original_path"),
    "recovery_path": ("rollback", "recovery_path"),
    "previous_version_path": ("rollback", "previous_version_path"),
}


_logger = get_logger(__name__)


def _record_path(record: Mapping[str, Any], name: str) -> str:
    """Resolve one recorded location from its canonical object."""
    object_name, field = _RECORDED_PATHS[name]
    grouped = record.get(object_name)
    if not isinstance(grouped, Mapping):
        return ""
    return _path_text(grouped.get(field))


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
    _logger.debug("attempting delete of created file path=%s", created)
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
        _logger.debug(
            "recovery source absent and destination not restored source=%s "
            "destination=%s", source, destination,
        )
        raise FileNotFoundError(f"Recovery source is missing: {source}")
    _logger.debug("attempting restore move source=%s destination=%s",
                  source, destination)
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
