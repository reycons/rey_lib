"""Semantic governed-file routing built on canonical mutation primitives."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from rey_lib.files.file_utils import move_file
from rey_lib.files.governed_file import FileId, governed_file_id
from rey_lib.logs import get_logger
from rey_lib.files.log_run_rollback import (
    SourceFileMutationEvidenceError,
    SourceFileMutationEvidenceFailurePhase,
    log_source_file_mutation,
)

_logger = get_logger(__name__)

__all__ = [
    "CollisionPolicy",
    "FileRoutingContext",
    "FileRoutingError",
    "FileRoutingEvidenceError",
    "FileRoutingResult",
    "FileRoutingRole",
    "FileRoutingRollbackInformation",
    "GovernedFileReference",
    "move_to_archive",
    "move_to_failed",
    "move_to_kickouts",
    "move_to_processing",
]

_ROUTE_TOKEN = re.compile(r"<([^<>]+)>")
_ACTION = "move"
_MESSAGES = {
    "processing": "Governed file moved to processing.",
    "kickouts": "Governed file moved to kickouts.",
    "failed": "Governed file moved to failed.",
    "archive": "Governed file moved to archive.",
}


class FileRoutingRole(str, Enum):
    """Fixed semantic destinations exposed by the routing API."""

    PROCESSING = "processing"
    KICKOUTS = "kickouts"
    FAILED = "failed"
    ARCHIVE = "archive"


class CollisionPolicy(str, Enum):
    """Collision behavior supported by the current move primitive."""

    OVERWRITE = "overwrite"


@dataclass(frozen=True)
class GovernedFileReference:
    """Identity and current location of exactly one governed file."""

    file_id: FileId
    current_path: Path
    classification: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        file_id = governed_file_id(self.file_id, subject="a governed file reference")
        if not isinstance(self.current_path, (str, Path)):
            raise ValueError("current_path must be a path.")
        if self.classification is not None and not isinstance(
            self.classification, Mapping
        ):
            raise ValueError("classification must be a mapping or None.")
        object.__setattr__(self, "file_id", file_id)
        object.__setattr__(
            self,
            "current_path",
            Path(self.current_path).expanduser().resolve(),
        )
        object.__setattr__(
            self,
            "classification",
            deepcopy(self.classification) if self.classification is not None else None,
        )


@dataclass(frozen=True)
class FileRoutingContext:
    """Operation-scoped routes, run state, and evidence inputs."""

    state_ctx: Any
    run_log: Any
    application_name: str
    #: The operation this routing serves. Routing knows destinations, never
    #: which operation asked for one, so the caller supplies it and the
    #: mutation states it.
    operation: str
    routes: Mapping[FileRoutingRole, str | Path | None]
    governed_roots: tuple[Path, ...]
    dry_run: bool = False
    destination_name: str | None = None
    collision_policy: CollisionPolicy = CollisionPolicy.OVERWRITE
    file_operation_metadata: Mapping[str, Any] | None = None
    mutation_run_log_fields: Mapping[str, Any] | None = None
    pipeline_name: str | None = None
    workflow_name: str | None = None

    def __post_init__(self) -> None:
        application_name = (
            self.application_name.strip()
            if isinstance(self.application_name, str)
            else ""
        )
        if not application_name:
            raise ValueError("application_name must be a non-empty string.")
        if not isinstance(self.routes, Mapping):
            raise ValueError("routes must be a mapping.")
        if not self.governed_roots:
            raise ValueError("governed_roots must contain at least one path.")
        try:
            collision_policy = CollisionPolicy(self.collision_policy)
        except ValueError as exc:
            raise ValueError("collision_policy must be 'overwrite'.") from exc

        object.__setattr__(self, "application_name", application_name)
        object.__setattr__(self, "routes", dict(self.routes))
        object.__setattr__(
            self,
            "governed_roots",
            tuple(Path(root).expanduser().resolve() for root in self.governed_roots),
        )
        object.__setattr__(self, "collision_policy", collision_policy)
        object.__setattr__(
            self,
            "file_operation_metadata",
            deepcopy(self.file_operation_metadata)
            if self.file_operation_metadata is not None
            else None,
        )
        object.__setattr__(
            self,
            "mutation_run_log_fields",
            deepcopy(self.mutation_run_log_fields)
            if self.mutation_run_log_fields is not None
            else None,
        )


@dataclass(frozen=True)
class FileRoutingRollbackInformation:
    """Only the rollback facts acknowledged to the routing call."""

    canonical_action: str
    original_path: Path
    resulting_path: Path
    file_manifest_record_id: int | None
    canonical_rollback_acknowledged: bool


@dataclass(frozen=True)
class FileRoutingResult:
    """Normalized outcome of one semantic routing operation."""

    file_id: FileId
    source_role: str | None
    destination_role: str
    original_path: Path
    resulting_path: Path
    canonical_action: str
    status: str
    dry_run: bool
    filesystem_applied: bool
    complete_evidence_acknowledged: bool
    mutation_run_log_committed: bool
    mutation_run_log_id: int | None
    evidence_phase: SourceFileMutationEvidenceFailurePhase | None
    file_manifest_record_id: int | None
    collision_policy: CollisionPolicy
    destination_existed: bool
    rollback_information: FileRoutingRollbackInformation | None
    failure_reason: str | None


class FileRoutingError(Exception):
    """Raised when routing validation or physical mutation fails."""

    def __init__(self, message: str, result: FileRoutingResult) -> None:
        super().__init__(message)
        self.result = result


class FileRoutingEvidenceError(FileRoutingError):
    """Raised after a move when complete evidence was not acknowledged."""


def move_to_processing(
    ctx: FileRoutingContext,
    file: GovernedFileReference,
) -> FileRoutingResult:
    """Move one governed file to its configured processing route."""
    return _move_to_role(ctx, file, destination_role=FileRoutingRole.PROCESSING)


def move_to_kickouts(
    ctx: FileRoutingContext,
    file: GovernedFileReference,
) -> FileRoutingResult:
    """Move one governed file to its configured kickouts route."""
    return _move_to_role(ctx, file, destination_role=FileRoutingRole.KICKOUTS)


def move_to_failed(
    ctx: FileRoutingContext,
    file: GovernedFileReference,
) -> FileRoutingResult:
    """Move one governed file to its configured failed route."""
    return _move_to_role(ctx, file, destination_role=FileRoutingRole.FAILED)


def move_to_archive(
    ctx: FileRoutingContext,
    file: GovernedFileReference,
) -> FileRoutingResult:
    """Move one governed file to its configured archive route."""
    return _move_to_role(ctx, file, destination_role=FileRoutingRole.ARCHIVE)


def _record_failed_move(
    ctx: "FileRoutingContext",
    file: "GovernedFileReference",
    destination_role: FileRoutingRole,
    original_path: Path,
    exc: BaseException,
) -> None:
    """Record that a governed move ran and did not take effect.

    The operation is the caller's, as it is for a successful move; routing
    knows the destination and never infers who asked for one.

    Never raises. Failing to record a failure must not replace the original
    error with a logging one.
    """
    try:
        log_source_file_mutation(
            ctx.state_ctx,
            action=_ACTION,
            status="failed",
            source_path=original_path,
            application_name=ctx.application_name,
            file_id=file.file_id,
            classification=file.classification,
            operation=ctx.operation,
            # Same result as a success; status distinguishes them.
            reason="moved_to_" + destination_role.value,
            message=(
                f"Move to {destination_role.value} failed for "
                f"'{original_path.name}': {exc}"
            ),
        )
    except Exception:  # noqa: BLE001 -- the move error is the one to raise
        _logger.warning("Could not record the failed move mutation for '%s'",
                        original_path.name)


def _move_to_role(
    ctx: FileRoutingContext,
    file: GovernedFileReference,
    *,
    destination_role: FileRoutingRole,
) -> FileRoutingResult:
    """Apply invariant governed routing for one fixed semantic role."""
    original_path = file.current_path
    route = ctx.routes.get(destination_role)
    if not isinstance(route, (str, Path)) or not str(route).strip():
        _raise_routing_error(
            ctx,
            file,
            destination_role,
            original_path,
            f"The configured {destination_role.value} route must be a non-empty path.",
        )
    if ctx.destination_name is not None and (
        not isinstance(ctx.destination_name, str)
        or not ctx.destination_name.strip()
        or Path(ctx.destination_name).name != ctx.destination_name
        or ctx.destination_name in {".", ".."}
    ):
        _raise_routing_error(
            ctx,
            file,
            destination_role,
            original_path,
            "destination_name must be one non-empty filename, not a path.",
        )

    try:
        destination_dir = _resolve_route(route, file.classification)
    except (TypeError, ValueError) as exc:
        _raise_routing_error(
            ctx,
            file,
            destination_role,
            original_path,
            str(exc),
        )
    resulting_path = (
        destination_dir / (ctx.destination_name or original_path.name)
    ).resolve()
    for field, path in (
        ("source", original_path),
        ("destination route", destination_dir),
        ("resulting", resulting_path),
    ):
        if not _inside_governed_roots(path, ctx.governed_roots):
            _raise_routing_error(
                ctx,
                file,
                destination_role,
                resulting_path,
                f"The {field} path is outside the configured governed roots: {path}",
            )

    if not original_path.is_file():
        _raise_routing_error(
            ctx,
            file,
            destination_role,
            resulting_path,
            f"Source file not found: {original_path}",
        )
    if original_path == resulting_path:
        return _result(
            ctx,
            file,
            destination_role,
            resulting_path,
            status="unchanged",
        )

    destination_existed = resulting_path.exists()
    if destination_existed and not resulting_path.is_file():
        _raise_routing_error(
            ctx,
            file,
            destination_role,
            resulting_path,
            f"Routing destination exists and is not a regular file: {resulting_path}",
            destination_existed=True,
        )
    if ctx.dry_run:
        return _result(
            ctx,
            file,
            destination_role,
            resulting_path,
            status="planned",
            destination_existed=destination_existed,
        )

    try:
        moved_path = move_file(
            original_path,
            destination_dir,
            ctx.destination_name,
            state_ctx=ctx.state_ctx,
            run_log=ctx.run_log,
            app=ctx.application_name,
            pipeline=ctx.pipeline_name,
            reason=destination_role.value,
            original_source=original_path,
            metadata=(
                dict(ctx.file_operation_metadata)
                if ctx.file_operation_metadata is not None
                else None
            ),
        )
    except OSError as exc:
        # The move itself failed, before any evidence was written. This is the
        # site: the filesystem operation is known not to have happened and no
        # mutation exists yet. The later except catches the *evidence* write
        # failing, which nothing can record.
        _record_failed_move(ctx, file, destination_role, original_path, exc)
        result = _result(
            ctx,
            file,
            destination_role,
            resulting_path,
            status="failed",
            destination_existed=destination_existed,
            failure_reason=str(exc),
        )
        raise FileRoutingError(str(exc), result) from exc

    rollback = FileRoutingRollbackInformation(
        canonical_action=_ACTION,
        original_path=original_path,
        resulting_path=moved_path,
        file_manifest_record_id=None,
        canonical_rollback_acknowledged=False,
    )
    try:
        manifest_record_id = log_source_file_mutation(
            ctx.state_ctx,
            action=_ACTION,
            status="success",
            source_path=original_path,
            destination_path=moved_path,
            application_name=ctx.application_name,
            file_id=file.file_id,
            classification=file.classification,
            # The destination is routing's; the operation is the caller's.
            # This layer serves whichever operation invoked it and must not
            # infer one from the role it was asked for.
            operation=ctx.operation,
            reason="moved_to_" + destination_role.value,
            message=_MESSAGES[destination_role.value],
            run_log_fields=(
                dict(ctx.mutation_run_log_fields)
                if ctx.mutation_run_log_fields is not None
                else None
            ),
        )
    except SourceFileMutationEvidenceError as exc:
        result = _result(
            ctx,
            file,
            destination_role,
            moved_path,
            status="failed",
            filesystem_applied=True,
            mutation_run_log_committed=exc.run_log_committed,
            mutation_run_log_id=exc.run_log_id,
            evidence_phase=exc.phase,
            destination_existed=destination_existed,
            rollback_information=rollback,
            failure_reason=str(exc),
        )
        raise FileRoutingEvidenceError(str(exc), result) from exc

    acknowledged_rollback = FileRoutingRollbackInformation(
        canonical_action=_ACTION,
        original_path=original_path,
        resulting_path=moved_path,
        file_manifest_record_id=manifest_record_id,
        canonical_rollback_acknowledged=True,
    )
    return _result(
        ctx,
        file,
        destination_role,
        moved_path,
        status="moved",
        filesystem_applied=True,
        complete_evidence_acknowledged=True,
        mutation_run_log_committed=True,
        file_manifest_record_id=manifest_record_id,
        destination_existed=destination_existed,
        rollback_information=acknowledged_rollback,
    )


def _resolve_route(
    route: str | Path,
    classification: Mapping[str, Any] | None,
) -> Path:
    text = str(route).strip()
    values: Mapping[str, Any] = {"classification": classification}

    def replace(match: re.Match[str]) -> str:
        address = match.group(1).strip()
        current: Any = values
        for part in address.split("."):
            if not isinstance(current, Mapping) or part not in current:
                raise ValueError(
                    f"Routing destination cannot resolve placeholder '<{address}>'."
                )
            current = current[part]
        if current is None or isinstance(current, (Mapping, list, tuple, set)):
            raise ValueError(
                f"Routing destination placeholder '<{address}>' must resolve "
                "to one non-empty scalar value."
            )
        resolved = str(current).strip()
        if not resolved:
            raise ValueError(
                f"Routing destination placeholder '<{address}>' must resolve "
                "to one non-empty scalar value."
            )
        return resolved

    return Path(_ROUTE_TOKEN.sub(replace, text)).expanduser().resolve()


def _inside_governed_roots(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _raise_routing_error(
    ctx: FileRoutingContext,
    file: GovernedFileReference,
    destination_role: FileRoutingRole,
    resulting_path: Path,
    message: str,
    *,
    destination_existed: bool = False,
) -> None:
    result = _result(
        ctx,
        file,
        destination_role,
        resulting_path,
        status="failed",
        destination_existed=destination_existed,
        failure_reason=message,
    )
    raise FileRoutingError(message, result)


def _result(
    ctx: FileRoutingContext,
    file: GovernedFileReference,
    destination_role: FileRoutingRole,
    resulting_path: Path,
    *,
    status: str,
    filesystem_applied: bool = False,
    complete_evidence_acknowledged: bool = False,
    mutation_run_log_committed: bool = False,
    mutation_run_log_id: int | None = None,
    evidence_phase: SourceFileMutationEvidenceFailurePhase | None = None,
    file_manifest_record_id: int | None = None,
    destination_existed: bool = False,
    rollback_information: FileRoutingRollbackInformation | None = None,
    failure_reason: str | None = None,
) -> FileRoutingResult:
    return FileRoutingResult(
        file_id=file.file_id,
        source_role=None,
        destination_role=destination_role.value,
        original_path=file.current_path,
        resulting_path=resulting_path,
        canonical_action=_ACTION,
        status=status,
        dry_run=ctx.dry_run,
        filesystem_applied=filesystem_applied,
        complete_evidence_acknowledged=complete_evidence_acknowledged,
        mutation_run_log_committed=mutation_run_log_committed,
        mutation_run_log_id=mutation_run_log_id,
        evidence_phase=evidence_phase,
        file_manifest_record_id=file_manifest_record_id,
        collision_policy=ctx.collision_policy,
        destination_existed=destination_existed,
        rollback_information=rollback_information,
        failure_reason=failure_reason,
    )
