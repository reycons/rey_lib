"""Manifest-authoritative rollback for one execution log.

The engine is deliberately neutral to workflows, pipelines, applications, and
installation-specific folder conventions. It selects immutable
``source_file_mutation`` records by ``evidence.run_log_file`` and delegates
operation-specific behavior to the compensation registry.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from rey_lib.files.file_utils import run_artifact_path
from rey_lib.files.jsonl import JsonlReadError
from rey_lib.logs import (
    FileManifestError,
    bind_run,
    clear_run,
    file_manifest_session,
    log_run_complete,
    log_run_record,
    log_run_start,
    log_run_summary,
    resolve_run_identity,
)

MUTATION_RECORD_TYPE = "source_file_mutation"
ROLLBACK_RECORD_TYPE = "source_file_rollback"
SCHEMA_VERSION = "1.0"


class LogRunRollbackError(Exception):
    """Raised when a rollback request cannot be planned or governed safely."""


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
    run_log_record_id: int,
    application_name: str = "",
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Build one authoritative filesystem-mutation manifest record."""
    normalized_action = _non_empty(action, "action")
    normalized_status = _non_empty(status, "status")
    evidence_file = _run_log_name(run_log_file)
    evidence_id = _positive_int(run_log_record_id, "run_log_record_id")
    return {
        "record_type": MUTATION_RECORD_TYPE,
        "schema_version": SCHEMA_VERSION,
        "action": normalized_action,
        "status": normalized_status,
        "source_path": _path_text(source_path),
        "destination_path": _path_text(destination_path),
        "recovery_path": _path_text(recovery_path),
        "previous_version_path": _path_text(previous_version_path),
        "application_name": str(application_name or ""),
        "evidence": {
            "run_log_file": evidence_file,
            "run_log_record_id": evidence_id,
        },
        "recorded_at": recorded_at or _timestamp(),
    }


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
    audit_ctx = _audit_context(ctx, source_path, audit_log_dir)
    log_run_start(
        audit_ctx,
        operation="log_run_rollback",
        original_run_log_file=selected_file,
        reason=str(reason or ""),
    )

    succeeded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    bind_run(audit_ctx)
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
                        original=original,
                        compensation=compensation,
                        phase="attempt",
                        status="attempted",
                        reason=str(reason or ""),
                    )
                    _append_rollback_evidence(
                        session,
                        audit_ctx,
                        original=original,
                        compensation=compensation,
                        phase="final",
                        status="failure",
                        attempt_record_id=attempt_id,
                        failure_reason=str(issue["reason"]),
                        reason=str(reason or ""),
                    )
                except (FileManifestError, LogRunRollbackError):
                    continue
            for candidate in plan["candidates"]:
                original = candidate["original_record"]
                compensation = _COMPENSATIONS[candidate["action"]]
                attempt_id: int | None = None
                try:
                    attempt_id = _append_rollback_evidence(
                        session,
                        audit_ctx,
                        original=original,
                        compensation=compensation,
                        phase="attempt",
                        status="attempted",
                        reason=str(reason or ""),
                    )
                except (FileManifestError, LogRunRollbackError) as exc:
                    failed.append(_failed_result(original, str(exc)))
                    continue

                try:
                    result = compensation.execute(original)
                except Exception as exc:  # noqa: BLE001 - best effort is contractual.
                    failure = _failed_result(original, str(exc))
                    failed.append(failure)
                    try:
                        _append_rollback_evidence(
                            session,
                            audit_ctx,
                            original=original,
                            compensation=compensation,
                            phase="final",
                            status="failure",
                            attempt_record_id=attempt_id,
                            failure_reason=str(exc),
                            reason=str(reason or ""),
                        )
                    except (FileManifestError, LogRunRollbackError):
                        pass
                    continue

                success = {
                    "original_manifest_record_id": original["record_id"],
                    "action": original["action"],
                    "compensating_action": compensation.compensating_action,
                    **result,
                }
                try:
                    _append_rollback_evidence(
                        session,
                        audit_ctx,
                        original=original,
                        compensation=compensation,
                        phase="final",
                        status="success",
                        attempt_record_id=attempt_id,
                        reason=str(reason or ""),
                    )
                except (FileManifestError, LogRunRollbackError) as exc:
                    failed.append(_failed_result(original, str(exc)))
                    continue
                succeeded.append(success)
    except (FileManifestError, JsonlReadError, OSError, ValueError) as exc:
        raise LogRunRollbackError(str(exc)) from exc
    finally:
        clear_run()

    status = _aggregate_status(len(succeeded), len(failed))
    summary = {
        "operation": "log_run_rollback",
        "original_run_log_file": selected_file,
        "status": status,
        "succeeded_count": len(succeeded),
        "failed_count": len(failed),
        "unsupported_count": plan["unsupported_count"],
        "non_recoverable_count": plan["non_recoverable_count"],
        "already_compensated_count": plan["already_compensated_count"],
        "indeterminate_count": plan["indeterminate_count"],
    }
    log_run_summary(audit_ctx, summary)
    log_run_complete(
        audit_ctx,
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
        source_id = _optional_positive_int(
            record.get("original_manifest_record_id")
        )
        if source_id is None:
            continue
        phase = record.get("phase")
        status = record.get("status")
        if phase == "attempt" and status == "attempted":
            attempt_id = _optional_positive_int(record.get("record_id"))
            if attempt_id is not None:
                attempts[attempt_id] = source_id
        if phase == "final":
            attempt_id = _optional_positive_int(
                record.get("rollback_attempt_record_id")
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
        if _is_selected_mutation(record, run_log_file)
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


def _is_selected_mutation(record: Mapping[str, Any], run_log_file: str) -> bool:
    if record.get("record_type") != MUTATION_RECORD_TYPE:
        return False
    if record.get("status") != "success":
        return False
    if _optional_positive_int(record.get("record_id")) is None:
        return False
    action = record.get("action")
    if not isinstance(action, str) or not action.strip():
        return False
    evidence = record.get("evidence")
    return (
        isinstance(evidence, Mapping)
        and evidence.get("run_log_file") == run_log_file
    )


def _validate_mutation_record(record: Mapping[str, Any]) -> None:
    """Reject malformed authoritative mutations before planning any work."""
    _positive_int(record.get("record_id"), "source_file_mutation.record_id")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise LogRunRollbackError(
            "source_file_mutation.schema_version must be '1.0'."
        )
    _non_empty(record.get("action"), "source_file_mutation.action")
    _non_empty(record.get("status"), "source_file_mutation.status")
    for field in (
        "source_path",
        "destination_path",
        "recovery_path",
        "previous_version_path",
    ):
        if not isinstance(record.get(field), str):
            raise LogRunRollbackError(
                f"source_file_mutation.{field} must be a string."
            )
    evidence = record.get("evidence")
    if not isinstance(evidence, Mapping):
        raise LogRunRollbackError(
            "source_file_mutation.evidence must be an object."
        )
    _run_log_name(evidence.get("run_log_file"))
    _positive_int(
        evidence.get("run_log_record_id"),
        "source_file_mutation.evidence.run_log_record_id",
    )


def _validate_rollback_record(record: Mapping[str, Any]) -> None:
    """Reject malformed linkage that could undermine idempotency."""
    _positive_int(record.get("record_id"), "source_file_rollback.record_id")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise LogRunRollbackError(
            "source_file_rollback.schema_version must be '1.0'."
        )
    _positive_int(
        record.get("original_manifest_record_id"),
        "source_file_rollback.original_manifest_record_id",
    )
    phase = record.get("phase")
    status = record.get("status")
    if phase not in {"attempt", "final"}:
        raise LogRunRollbackError(
            "source_file_rollback.phase must be 'attempt' or 'final'."
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
            record.get("rollback_attempt_record_id"),
            "source_file_rollback.rollback_attempt_record_id",
        )
    _run_log_name(record.get("original_run_log_file"))
    _non_empty(record.get("original_action"), "source_file_rollback.original_action")
    _non_empty(
        record.get("compensating_action"),
        "source_file_rollback.compensating_action",
    )
    for field in (
        "source_path",
        "destination_path",
        "recovery_path",
        "previous_version_path",
        "failure_reason",
    ):
        if not isinstance(record.get(field), str):
            raise LogRunRollbackError(
                f"source_file_rollback.{field} must be a string."
            )
    evidence = record.get("evidence")
    if not isinstance(evidence, Mapping):
        raise LogRunRollbackError(
            "source_file_rollback.evidence must be an object."
        )
    _run_log_name(evidence.get("run_log_file"))
    _positive_int(
        evidence.get("run_log_record_id"),
        "source_file_rollback.evidence.run_log_record_id",
    )


def _append_rollback_evidence(
    session: Any,
    audit_ctx: Any,
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
        "original_action": str(original["action"]),
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
        audit_ctx,
        "SOURCE_FILE_ROLLBACK",
        **fields,
    )
    if run_record_id is None:
        raise LogRunRollbackError(
            "Rollback run-log evidence did not commit; no manifest evidence "
            "or filesystem compensation was performed."
        )
    record = {
        "record_type": ROLLBACK_RECORD_TYPE,
        "schema_version": SCHEMA_VERSION,
        **fields,
        "rollback_run_id": str(getattr(audit_ctx, "run_id", "") or ""),
        "application_name": "rey_lib",
        "evidence": {
            "run_log_file": Path(str(audit_ctx.run_log_path)).name,
            "run_log_record_id": run_record_id,
        },
        "recorded_at": _timestamp(),
    }
    if attempt_record_id is None:
        record.pop("rollback_attempt_record_id")
    return session.append(record)


def _validate_move(record: Mapping[str, Any]) -> str | None:
    return _require_paths(record, "source_path", "destination_path")


def _execute_move(record: Mapping[str, Any]) -> dict[str, Any]:
    current = Path(str(record["destination_path"]))
    original = Path(str(record["source_path"]))
    _move_exact(current, original)
    return {"from_path": str(current), "to_path": str(original)}


def _validate_create(record: Mapping[str, Any]) -> str | None:
    return _require_paths(record, "destination_path")


def _execute_create(record: Mapping[str, Any]) -> dict[str, Any]:
    created = Path(str(record["destination_path"]))
    if not created.is_file():
        raise FileNotFoundError(f"Created file is missing: {created}")
    created.unlink()
    return {"deleted_path": str(created)}


def _validate_delete(record: Mapping[str, Any]) -> str | None:
    return _require_paths(record, "source_path", "recovery_path")


def _execute_delete(record: Mapping[str, Any]) -> dict[str, Any]:
    recovery = Path(str(record["recovery_path"]))
    original = Path(str(record["source_path"]))
    _move_exact(recovery, original)
    return {"from_path": str(recovery), "to_path": str(original)}


def _validate_replace(record: Mapping[str, Any]) -> str | None:
    return _require_paths(record, "destination_path", "previous_version_path")


def _execute_replace(record: Mapping[str, Any]) -> dict[str, Any]:
    previous = Path(str(record["previous_version_path"]))
    destination = Path(str(record["destination_path"]))
    _move_exact(previous, destination)
    return {"from_path": str(previous), "to_path": str(destination)}


def _move_exact(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"Recovery source is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))


def _require_paths(
    record: Mapping[str, Any],
    *names: str,
) -> str | None:
    missing = [
        name
        for name in names
        if not isinstance(record.get(name), str) or not record.get(name)
    ]
    if missing:
        return f"missing recorded compensation field(s): {', '.join(missing)}"
    return None


def _audit_context(
    ctx: Any,
    source_run_log: Path,
    audit_log_dir: Path | str | None,
) -> SimpleNamespace:
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
    resolve_run_identity(audit_ctx)
    audit_ctx.run_log_path = str(
        run_artifact_path(
            directory,
            "log_run_rollback",
            audit_ctx.run_timestamp,
            "jsonl",
        )
    )
    return audit_ctx


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


def _path_text(value: Any) -> str:
    return str(value or "").strip()


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
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


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
