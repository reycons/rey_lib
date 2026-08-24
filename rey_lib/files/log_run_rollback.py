"""Manifest-authoritative rollback for one execution log.

The engine is deliberately neutral to workflows, pipelines, applications, and
installation-specific folder conventions. It selects immutable
``source_file_mutation`` records by ``evidence.run_log_file`` and delegates
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

from rey_lib.files.file_utils import delete_file, run_artifact_path
from rey_lib.files.jsonl import JsonlReadError, read_jsonl_file, write_jsonl_file
from rey_lib.run import establish_run_identity
from rey_lib.logs import (
    FileManifestError,
    ProfileLibraryError,
    bind_run,
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
        run_log_file: str | None,
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
        elif run_log_file is not None or run_log_id is not None:
            raise ValueError(
                "The pre-run-log mutation-evidence phase cannot carry a committed "
                "run-log reference."
            )

        super().__init__(message)
        self.phase = normalized_phase
        self.run_log_file = run_log_file
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
        run_log_file: str,
    ) -> SourceFileMutationEvidenceResult:
        value = _positive_int(manifest_record_id, "manifest_record_id")
        instance = int.__new__(cls, value)
        instance.run_log_id = _positive_int(
            run_log_id,
            "run_log_id",
        )
        instance.run_log_file = _run_log_name(run_log_file)
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
    run_log_file: str,
    run_log_id: int,
    application_name: str = "",
    file_id: str = "",
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
    evidence_file = _run_log_name(run_log_file)
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
    if _path_text(file_id):
        # Flat identity leads the record, so file_id is placed before the
        # objects rather than appended after them.
        record = {"file_id": _path_text(file_id), **record}
    record["evidence"] = {
        "run_log_file": evidence_file,
        "run_log_id": evidence_id,
    }
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


def serialize_source_file_rollback(
    *,
    original_record_id: int,
    phase: str,
    status: str,
    run_log_file: str,
    run_log_id: int,
    attempt_record_id: int | None = None,
    application_name: str = "",
    reason: str = "",
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Build one canonical rollback-evidence manifest record.

    The record references the compensated lifecycle record rather than
    duplicating its data, so it carries no ``file`` section. ``rollback``
    carries the reference plus the linkage that makes compensation
    idempotent: which phase this record is, and which attempt a final record
    concludes. An attempt record has no attempt of its own to reference, so
    that field is omitted rather than recorded null.
    """
    normalized_phase = _non_empty(phase, "phase")
    if normalized_phase not in {"attempt", "final"}:
        raise LogRunRollbackError(
            "source_file_rollback.phase must be 'attempt' or 'final'."
        )
    normalized_status = _non_empty(status, "status")

    rollback_object: dict[str, Any] = {
        "original_record_id": _positive_int(
            original_record_id, "rollback.original_record_id"
        ),
        "phase": normalized_phase,
    }
    if attempt_record_id is not None:
        rollback_object["attempt_record_id"] = _positive_int(
            attempt_record_id, "rollback.attempt_record_id"
        )

    record: dict[str, Any] = {
        "recorded_at": recorded_at or _timestamp(),
        "record_type": ROLLBACK_RECORD_TYPE,
        "status": normalized_status,
        "evidence": {
            "run_log_file": _run_log_name(run_log_file),
            "run_log_id": _positive_int(
                run_log_id, "run_log_id"
            ),
        },
        "rollback": rollback_object,
    }
    if _path_text(reason):
        record["result"] = {"reason": _path_text(reason)}
    record["producer"] = {"application": str(application_name or "")}
    return record


def serialize_run_rollback(
    *,
    rollback_run_id: str,
    original_run_id: str,
    status: str,
    records_removed: int,
    filesystem_operations_reversed: int,
    affected_file_ids: Sequence[str] = (),
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
        "affected_file_ids": [str(value) for value in affected_file_ids],
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
    ctx: Any, run_log,
    *,
    action: str,
    status: str,
    source_path: Path | str = "",
    destination_path: Path | str = "",
    recovery_path: Path | str = "",
    previous_version_path: Path | str = "",
    application_name: str = "",
    file_id: str = "",
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
            run_log_file=None,
            run_log_id=None,
        )

    run_log_file: str | None = None
    try:
        run_log_file = _context_run_log_name(ctx)
        record = serialize_source_file_mutation(
            action=normalized_action,
            status=normalized_status,
            source_path=source_path,
            destination_path=destination_path,
            recovery_path=recovery_path,
            previous_version_path=previous_version_path,
            run_log_file=run_log_file,
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
            run_log_file=run_log_file,
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
            run_log_file=run_log_file,
            run_log_id=run_log_id,
        ) from exc


def preview_log_run_rollback(
    ctx: Any,
    run_log_file: Path | str,
) -> dict[str, Any]:
    """Return the manifest-authoritative rollback plan without filesystem writes."""
    selected_file = _run_log_name(run_log_file)
    try:
        with file_manifest_session(ctx) as session:
            records = _read_required_manifest(session)
    except (FileManifestError, JsonlReadError, OSError, ValueError) as exc:
        raise LogRunRollbackError(str(exc)) from exc
    return _build_plan(records, selected_file)


def rollback_log_run(
    ctx: Any,
    run_log_file: Path | str,
    *,
    reason: str = "",
    audit_log_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Compensate one run's manifest mutations in reverse execution order."""
    source_path = Path(run_log_file).expanduser().resolve()
    selected_file = _run_log_name(source_path)
    audit_ctx, audit_run_log = _audit_context(ctx, source_path, audit_log_dir)
    log_run_start(
        audit_run_log,
        operation="log_run_rollback",
        original_run_log_file=selected_file,
        reason=str(reason or ""),
    )

    succeeded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    appended_rollback_record_ids: list[int] = []
    reversed_record_ids: set[int] = set()
    affected_file_ids: set[str] = set()
    filesystem_reversals = 0
    records_removed = 0
    run_is_clear = False
    started_at = _timestamp()
    bind_run(audit_run_log)
    try:
        with file_manifest_session(ctx) as session:
            records = _read_required_manifest(session)
            plan = _build_plan(records, selected_file)
            records_by_id = {
                record.get("record_id"): record for record in records
            }
            for issue in [*plan["unsupported"], *plan["non_recoverable"]]:
                original = records_by_id.get(
                    issue["original_manifest_record_id"]
                )
                if not isinstance(original, Mapping):
                    continue
                compensation = _COMPENSATIONS.get(str(issue["action"]))
                if compensation is None:
                    compensation = Compensation(
                        action=str(issue["action"]),
                        compensating_action="unavailable",
                        validate=lambda _record: str(issue["reason"]),
                        execute=lambda _record: {},
                    )
                failed.append(_failed_result(original, str(issue["reason"])))
                attempt_id: int | None = None
                try:
                    attempt_id = _append_rollback_evidence(
                        session,
                        audit_ctx,
                        audit_run_log,
                        original=original,
                        compensation=compensation,
                        phase="attempt",
                        status="attempted",
                        reason=str(reason or ""),
                    )
                    appended_rollback_record_ids.append(attempt_id)
                    final_id = _append_rollback_evidence(
                        session,
                        audit_ctx,
                        audit_run_log,
                        original=original,
                        compensation=compensation,
                        phase="final",
                        status="failure",
                        attempt_record_id=attempt_id,
                        failure_reason=str(issue["reason"]),
                        reason=str(reason or ""),
                    )
                    appended_rollback_record_ids.append(final_id)
                except (FileManifestError, LogRunRollbackError):
                    continue
            for candidate in plan["candidates"]:
                original = candidate["original_record"]
                compensation = _resolved_compensation(candidate)
                attempt_id: int | None = None
                try:
                    attempt_id = _append_rollback_evidence(
                        session,
                        audit_ctx,
                        audit_run_log,
                        original=original,
                        compensation=compensation,
                        phase="attempt",
                        status="attempted",
                        reason=str(reason or ""),
                    )
                    appended_rollback_record_ids.append(attempt_id)
                except (FileManifestError, LogRunRollbackError) as exc:
                    failed.append(_failed_result(original, str(exc)))
                    continue

                try:
                    result = compensation.execute(original)
                except Exception as exc:  # noqa: BLE001 - best effort is contractual.
                    failure = _failed_result(original, str(exc))
                    failed.append(failure)
                    try:
                        final_id = _append_rollback_evidence(
                            session,
                            audit_ctx,
                            audit_run_log,
                            original=original,
                            compensation=compensation,
                            phase="final",
                            status="failure",
                            attempt_record_id=attempt_id,
                            failure_reason=str(exc),
                            reason=str(reason or ""),
                        )
                        appended_rollback_record_ids.append(final_id)
                    except (FileManifestError, LogRunRollbackError):
                        pass
                    continue

                success = {
                    "original_manifest_record_id": original["record_id"],
                    "action": original.get("action", candidate["action"]),
                    "compensating_action": compensation.compensating_action,
                    **result,
                }
                try:
                    final_id = _append_rollback_evidence(
                        session,
                        audit_ctx,
                        audit_run_log,
                        original=original,
                        compensation=compensation,
                        phase="final",
                        status="success",
                        attempt_record_id=attempt_id,
                        reason=str(reason or ""),
                    )
                    appended_rollback_record_ids.append(final_id)
                except (FileManifestError, LogRunRollbackError) as exc:
                    # The compensation already happened. Failing to write a
                    # note about it cannot un-happen it, and abandoning the
                    # record here is what leaves a manifest describing files
                    # that are no longer there. The reversal stands and the
                    # bookkeeping failure is reported.
                    log_run_record(
                        audit_run_log,
                        "ERROR",
                        message=(
                            "Compensation succeeded but its evidence could not "
                            f"be appended: {exc}"
                        ),
                        original_manifest_record_id=int(original["record_id"]),
                        original_run_log_file=selected_file,
                    )
                succeeded.append(success)
                reversed_record_ids.add(int(original["record_id"]))
                if _is_filesystem_reversal(candidate, result):
                    filesystem_reversals += 1
                file_id = original.get("file_id")
                if isinstance(file_id, str) and file_id.strip():
                    affected_file_ids.add(file_id.strip())

            # Nothing is removed. A reversed mutation is marked reversed on its
            # own row, and a row marked reversed stops participating in current
            # state -- which is what the manifest describing current state
            # meant when records had to be deleted to achieve it. The history
            # is kept because it is history.
            #
            # This also removes the unbounded growth the deletion was defending
            # against: a retried rollback re-marks the same rows rather than
            # appending compensation evidence that then has to be reclaimed.
            if reversed_record_ids:
                _remove_profiles_for_run_log(ctx, selected_file)
                records_removed = 0
            # Whether anything of this run is left is read from the manifest,
            # not inferred from a status. A record can survive without being a
            # failure — an indeterminate attempt is neither reversed nor
            # failed — and a run log is the only way to select that run again.
            run_is_clear = not any(
                _record_run_log_file(record) == selected_file
                for record in session.read_records()
            )
    except (FileManifestError, JsonlReadError, OSError, ValueError) as exc:
        raise LogRunRollbackError(str(exc)) from exc
    finally:
        clear_run()

    status = _aggregate_status(len(succeeded), len(failed))
    rollback_run_log_file = Path(str(audit_ctx.run_log_path)).name
    run_rollback_record_id = _write_run_rollback_summary(
        ctx,
        audit_run_log, status=status,
        rollback_run_id=str(getattr(audit_ctx, "run_id", "") or rollback_run_log_file),
        original_run_id=selected_file,
        records_removed=records_removed,
        filesystem_operations_reversed=filesystem_reversals,
        affected_file_ids=sorted(affected_file_ids),
        started_at=started_at,
        failed=failed,
    )
    # The log is retired only when the run has nothing left in the manifest.
    # Anything still there — failed, unsupported, or indeterminate — needs this
    # log to be selected again, so it stays.
    deleted_run_log_files = (
        _delete_rolled_back_run_log(source_path)
        if status == "success" and run_is_clear
        else []
    )
    summary = {
        "operation": "log_run_rollback",
        "deleted_run_log_files": deleted_run_log_files,
        "original_run_log_file": selected_file,
        "rollback_run_log_file": rollback_run_log_file,
        "status": status,
        "records_removed": records_removed,
        "filesystem_operations_reversed": filesystem_reversals,
        "run_rollback_record_id": run_rollback_record_id,
        "succeeded_count": len(succeeded),
        "failed_count": len(failed),
        "unsupported_count": plan["unsupported_count"],
        "non_recoverable_count": plan["non_recoverable_count"],
        "already_compensated_count": plan["already_compensated_count"],
        "indeterminate_count": plan["indeterminate_count"],
        "appended_rollback_evidence_count": len(
            appended_rollback_record_ids
        ),
        "failures": failed,
    }
    log_run_summary(audit_run_log, summary)
    log_run_complete(
        audit_run_log,
        status,
        **{key: value for key, value in summary.items() if key != "status"},
    )
    return {
        **summary,
        "reason": str(reason or ""),
        "audit_log_path": str(audit_ctx.run_log_path),
        "succeeded": succeeded,
        "failed": failed,
    }


def _remove_profiles_for_run_log(ctx: Any, run_log_file: str) -> int:
    """Remove the canonical profiles this run log produced.

    Selection is the one the rest of rollback already uses: an exact
    ``evidence.run_log_file`` match. Matching on the profiled object's
    manifest row instead was the earlier mistake — a profile's object_id names
    what was profiled, not which run wrote the profile, so a rollback whose
    reversed rows happened not to be the profiled rows removed nothing at all.

    The caller already holds the manifest lock used by profile-library writers,
    so this performs the canonical JSONL rewrite directly without reacquiring
    that non-reentrant lock.
    """
    if not run_log_file:
        return 0
    try:
        target = resolve_profile_library_path(ctx)
    except ProfileLibraryError:
        return 0
    if not target.exists():
        return 0
    try:
        records = [dict(item.record) for item in read_jsonl_file(target)]
    except (JsonlReadError, OSError) as exc:
        raise LogRunRollbackError(
            f"Profile records could not be read from '{target}': {exc}"
        ) from exc

    retained: list[dict[str, Any]] = []
    removed = 0
    for record in records:
        header = record.get("header")
        if not isinstance(header, Mapping):
            raise LogRunRollbackError(
                "Stored profile record must contain a canonical header object."
            )
        evidence = header.get("evidence")
        if not isinstance(evidence, Mapping):
            raise LogRunRollbackError(
                "Stored profile header requires an evidence object naming the "
                "run log that produced it."
            )
        produced_by = str(evidence.get("run_log_file") or "").strip()
        if not produced_by:
            raise LogRunRollbackError(
                "Stored profile evidence requires a non-empty run_log_file."
            )
        if produced_by == run_log_file:
            removed += 1
        else:
            retained.append(record)
    if removed:
        try:
            write_jsonl_file(target, retained)
        except (OSError, TypeError, ValueError) as exc:
            raise LogRunRollbackError(
                f"Profile records could not be rewritten in '{target}': {exc}"
            ) from exc
    return removed


def _build_plan(
    records: list[dict[str, Any]],
    run_log_file: str,
) -> dict[str, Any]:
    successful: set[int] = set()
    attempts: dict[int, int] = {}
    finalized_attempts: set[int] = set()

    for record in records:
        if record.get("record_type") == MUTATION_RECORD_TYPE:
            _validate_mutation_record(record)
        if record.get("record_type") == ROLLBACK_RECORD_TYPE:
            _validate_rollback_record(record)
        if record.get("record_type") != ROLLBACK_RECORD_TYPE:
            continue
        rollback = record.get("rollback")
        if not isinstance(rollback, Mapping):
            continue
        source_id = _optional_positive_int(rollback.get("original_record_id"))
        if source_id is None:
            continue
        phase = rollback.get("phase")
        status = record.get("status")
        if phase == "attempt" and status == "attempted":
            attempt_id = _optional_positive_int(record.get("record_id"))
            if attempt_id is not None:
                attempts[attempt_id] = source_id
        if phase == "final":
            attempt_id = _optional_positive_int(
                rollback.get("attempt_record_id")
            )
            if attempt_id is not None:
                finalized_attempts.add(attempt_id)
            if status == "success":
                successful.add(source_id)

    indeterminate_ids = {
        source_id
        for attempt_id, source_id in attempts.items()
        if attempt_id not in finalized_attempts
    }
    candidates: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    non_recoverable: list[dict[str, Any]] = []
    already_compensated: list[int] = []
    indeterminate: list[int] = []

    mutations = [
        record
        for record in records
        if _is_selected_record(record, run_log_file)
    ]
    mutations.sort(key=lambda item: int(item["record_id"]), reverse=True)
    for record in mutations:
        record_id = int(record["record_id"])
        if record_id in successful:
            already_compensated.append(record_id)
            continue
        if record_id in indeterminate_ids:
            indeterminate.append(record_id)
            continue
        if record.get("record_type") != MUTATION_RECORD_TYPE:
            # Inventory and classification describe state rather than change
            # it, so removing the record is the whole inverse.
            candidates.append({
                "original_manifest_record_id": record_id,
                "action": "record_only",
                "compensating_action": "remove_record",
                "original_record": record,
            })
            continue
        if record.get("status") != "success":
            # The operation did not happen, so there is nothing to undo and
            # nothing to be careful about: compensating a failed create would
            # delete a file this run never wrote. Only the record goes.
            candidates.append({
                "original_manifest_record_id": record_id,
                "action": "record_only",
                "compensating_action": "remove_record",
                "original_record": record,
            })
            continue
        action = str(record["action"]).strip().lower()
        compensation = _COMPENSATIONS.get(action)
        if compensation is None:
            unsupported.append({
                "original_manifest_record_id": record_id,
                "action": action,
                "reason": "no registered compensating operation",
            })
            continue
        validation_error = compensation.validate(record)
        if validation_error:
            non_recoverable.append({
                "original_manifest_record_id": record_id,
                "action": action,
                "reason": validation_error,
            })
            continue
        candidates.append({
            "original_manifest_record_id": record_id,
            "action": action,
            "compensating_action": compensation.compensating_action,
            "original_record": record,
        })

    return {
        "run_log_file": run_log_file,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "unsupported": unsupported,
        "unsupported_count": len(unsupported),
        "non_recoverable": non_recoverable,
        "non_recoverable_count": len(non_recoverable),
        "already_compensated": already_compensated,
        "already_compensated_count": len(already_compensated),
        "indeterminate": indeterminate,
        "indeterminate_count": len(indeterminate),
    }


def _read_required_manifest(session: Any) -> list[dict[str, Any]]:
    """Strictly read the configured authority; rollback never invents an empty one."""
    if not session.path.is_file():
        raise LogRunRollbackError(
            f"File manifest does not exist: {session.path}"
        )
    return session.read_records()


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


def _record_run_log_file(record: Mapping[str, Any]) -> str:
    """Resolve the canonical run-log pointer."""
    evidence = record.get("evidence")
    if not isinstance(evidence, Mapping):
        return ""
    return _path_text(evidence.get("run_log_file"))


def _is_selected_mutation(record: Mapping[str, Any], run_log_file: str) -> bool:
    """Whether this run wrote the mutation, whatever its outcome was.

    A failed mutation is selected too. Its operation never happened, so there
    is nothing on disk to reverse — but the record it left behind is still one
    this run introduced, and rollback leaves no mutation records from the run.
    What it did decides how it is reversed, not whether it is selected.
    """
    if record.get("record_type") != MUTATION_RECORD_TYPE:
        return False
    if _optional_positive_int(record.get("record_id")) is None:
        return False
    action = record.get("action")
    if not isinstance(action, str) or not action.strip():
        return False
    return _record_run_log_file(record) == run_log_file


def _is_selected_record(record: Mapping[str, Any], run_log_file: str) -> bool:
    """Whether this run introduced the record, whatever its canonical type.

    Inventory and classification records have no filesystem effect, so their
    inverse is the removal of the record and nothing more. They are selected on
    the same evidence key as a mutation, because that key is what ties any
    manifest record to the run that wrote it.
    """
    if record.get("record_type") == MUTATION_RECORD_TYPE:
        return _is_selected_mutation(record, run_log_file)
    if record.get("record_type") not in _SELECTABLE_RECORD_TYPES:
        return False
    if _optional_positive_int(record.get("record_id")) is None:
        return False
    return _record_run_log_file(record) == run_log_file


def _is_run_owned_rollback(record: Mapping[str, Any], removed: set[int]) -> bool:
    """Whether this compensation record belongs to a record being removed.

    ``source_file_rollback`` references the mutation it compensated by
    ``rollback.original_record_id``, and the lifecycle projection only renders
    it while that mutation is present. Removing the mutation and leaving the
    reference behind would strand evidence that no consumer can resolve, so the
    compensation records for a rolled-back run leave with it.
    """
    if record.get("record_type") != ROLLBACK_RECORD_TYPE:
        return False
    rollback = record.get("rollback")
    if not isinstance(rollback, Mapping):
        return False
    return _optional_positive_int(rollback.get("original_record_id")) in removed


def _delete_rolled_back_run_log(source_path: Path) -> list[str]:
    """Delete a fully rolled-back run's log and its companions.

    The run's governed state is gone, so its log stops offering itself as
    something to inspect or roll back again. Its results summary and sequencing
    state go with it: neither means anything once the log they describe is not
    there.

    Only a rollback that reversed everything reaches this. A run that still has
    records keeps its log, because that log is what a retry selects by.
    """
    stem = source_path.name.split(".jsonl")[0]
    deleted: list[str] = []
    for companion in sorted(source_path.parent.glob(f"{stem}.*")):
        if companion.is_file() and delete_file(companion):
            deleted.append(companion.name)
    return deleted


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
    affected_file_ids: Sequence[str],
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
    _run_log_name(_record_run_log_file(record))
    _positive_int(
        evidence.get("run_log_id"),
        "source_file_mutation.evidence.run_log_id",
    )


def _validate_rollback_record(record: Mapping[str, Any]) -> None:
    """Reject malformed linkage that could undermine idempotency."""
    _positive_int(record.get("record_id"), "source_file_rollback.record_id")
    rollback = record.get("rollback")
    if not isinstance(rollback, Mapping):
        raise LogRunRollbackError(
            "source_file_rollback.rollback must be an object."
        )
    _positive_int(
        rollback.get("original_record_id"),
        "source_file_rollback.rollback.original_record_id",
    )
    phase = rollback.get("phase")
    status = record.get("status")
    if phase not in {"attempt", "final"}:
        raise LogRunRollbackError(
            "source_file_rollback.rollback.phase must be 'attempt' or 'final'."
        )
    if phase == "attempt" and status != "attempted":
        raise LogRunRollbackError(
            "A rollback attempt record must have status 'attempted'."
        )
    if phase == "final" and status not in {"success", "failure"}:
        raise LogRunRollbackError(
            "A final rollback record must have status 'success' or 'failure'."
        )
    if phase == "final":
        _positive_int(
            rollback.get("attempt_record_id"),
            "source_file_rollback.rollback.attempt_record_id",
        )
    evidence = record.get("evidence")
    if not isinstance(evidence, Mapping):
        raise LogRunRollbackError(
            "source_file_rollback.evidence must be an object."
        )
    _run_log_name(evidence.get("run_log_file"))
    _positive_int(
        evidence.get("run_log_id"),
        "source_file_rollback.evidence.run_log_id",
    )


def _append_rollback_evidence(
    session: Any,
    audit_ctx: Any,
    audit_run_log: Any,
    *,
    original: Mapping[str, Any],
    compensation: Compensation,
    phase: str,
    status: str,
    reason: str,
    attempt_record_id: int | None = None,
    failure_reason: str = "",
) -> int:
    fields = {
        "original_manifest_record_id": int(original["record_id"]),
        "original_run_log_file": str(original["evidence"]["run_log_file"]),
        # Inventory and classification records describe state rather than a
        # filesystem action, so they carry no action of their own. The
        # compensation names what is being done to them instead.
        "original_action": str(original.get("action") or compensation.action),
        "compensating_action": compensation.compensating_action,
        "phase": phase,
        "status": status,
        "source_path": _path_text(original.get("source_path")),
        "destination_path": _path_text(original.get("destination_path")),
        "recovery_path": _path_text(original.get("recovery_path")),
        "previous_version_path": _path_text(
            original.get("previous_version_path")
        ),
        "rollback_attempt_record_id": attempt_record_id,
        "reason": reason,
        "failure_reason": failure_reason,
    }
    run_record_id = log_run_record(
        audit_run_log,
        "SOURCE_FILE_ROLLBACK",
        **fields,
    )
    if run_record_id is None:
        raise LogRunRollbackError(
            "Rollback run-log evidence did not commit; no manifest evidence "
            "or filesystem compensation was performed."
        )
    # The record shape belongs to the serializer; this function gathers its
    # inputs and appends the result through the already-locked session.
    record = serialize_source_file_rollback(
        original_record_id=int(original["record_id"]),
        phase=phase,
        status=status,
        run_log_file=Path(str(audit_ctx.run_log_path)).name,
        run_log_id=run_record_id,
        attempt_record_id=attempt_record_id,
        application_name="rey_lib",
        reason=reason,
    )
    return session.append(record)


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


def _validate_delete(record: Mapping[str, Any]) -> str | None:
    return _require_paths(record, "original_path", "recovery_path")


def _execute_delete(record: Mapping[str, Any]) -> dict[str, Any]:
    recovery = Path(_record_path(record, "recovery_path"))
    original = Path(_record_path(record, "original_path"))
    outcome = _move_exact(recovery, original)
    return {
        "from_path": str(recovery),
        "to_path": str(original),
        "outcome": outcome,
    }


def _validate_replace(record: Mapping[str, Any]) -> str | None:
    return _require_paths(record, "current_path", "previous_version_path")


def _execute_replace(record: Mapping[str, Any]) -> dict[str, Any]:
    previous = Path(_record_path(record, "previous_version_path"))
    destination = Path(_record_path(record, "current_path"))
    outcome = _move_exact(previous, destination)
    return {
        "from_path": str(previous),
        "to_path": str(destination),
        "outcome": outcome,
    }


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


def _audit_context(
    ctx: Any,
    source_run_log: Path,
    audit_log_dir: Path | str | None,
) -> tuple[SimpleNamespace, Any]:
    directory = Path(
        audit_log_dir or source_run_log.parent
    ).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    audit_ctx = SimpleNamespace(
        app_name="log_run_rollback",
        paths=getattr(ctx, "paths", None),
        installation=getattr(ctx, "installation", ""),
        config_root=getattr(ctx, "config_root", ""),
    )
    # The audit belongs to the run that asked for the rollback. It used to mint
    # an identity of its own here, which is no longer possible and was never
    # right: identity comes from recording a run, and a rollback performed
    # during a run is part of that run rather than a second one.
    audit_ctx.run_id = getattr(ctx, "run_id", None)
    establish_run_identity(audit_ctx)
    audit_ctx.run_log_path = str(
        run_artifact_path(
            directory,
            "log_run_rollback",
            audit_ctx.run_timestamp,
            "jsonl",
        )
    )
    from rey_lib.logs.run_log import RunLog

    audit_run_log = RunLog(
        app="log_run_rollback",
        run_id=audit_ctx.run_id,
        run_timestamp=audit_ctx.run_timestamp,
        path=audit_ctx.run_log_path,
    )
    return audit_ctx, audit_run_log


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


def _run_log_name(value: Path | str) -> str:
    text = str(value or "").strip()
    if not text:
        raise LogRunRollbackError("run_log_file must be a non-empty path or filename.")
    return Path(text).name


def _context_run_log_name(ctx: Any) -> str:
    for field in ("run_log_path", "log_file"):
        value = getattr(ctx, field, None)
        if value:
            return _run_log_name(value)
    raise LogRunRollbackError(
        "Source-file mutation evidence committed without an addressable run-log "
        "filename."
    )


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
register_file_compensation(
    "delete",
    compensating_action="restore_recoverable_file",
    validate=_validate_delete,
    execute=_execute_delete,
)
register_file_compensation(
    "replace",
    compensating_action="restore_previous_version",
    validate=_validate_replace,
    execute=_execute_replace,
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
