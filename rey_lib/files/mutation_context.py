"""Governed lifecycle context for Loader source-file mutations.

The Loader resolves lifecycle identity and classification here before a
mutation producer reaches the shared serializer.  This module performs no
manifest selection, filename inference, or classification inference.

Copied from the legacy file_operator module (row 589, step 4); the legacy
module stays untouched as the reference.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from rey_lib.files.governed_file import FileId, governed_file_id
from rey_lib.errors.error_utils import AppError
from rey_lib.files import (
    log_source_file_mutation as _shared_log_source_file_mutation,
)
from rey_lib.logs.file_manifest import FileManifestError

__all__ = [
    "MutationContext",
    "MutationContextError",
    "build_mutation_context",
    "log_governed_source_file_mutation",
]


class MutationContextError(AppError):
    """Raised when a selected lifecycle record cannot govern a mutation."""


@dataclass(frozen=True)
class MutationContext:
    """Producer-neutral identity and optional governed classification."""

    file_id: FileId
    classification: Mapping[str, Any] | None = None
    #: The prior mutation this step consumed, as its selector returned it.
    #:
    #: Distinct from file_id, which says which governed file the mutation
    #: belongs to. This says which record it was produced from -- the row the
    #: step's file_selection procedure handed it. A file has many mutations and
    #: knowing the file does not say which of them was read.
    #:
    #: None for a producer that consumed no prior record. Inventory is the only
    #: one: it is the baseline, and there is nothing before it.
    source_record_id: int | None = None


def build_mutation_context(
    source_record: Mapping[str, Any],
) -> MutationContext:
    """Build mutation context from one already-selected lifecycle record."""
    if not isinstance(source_record, Mapping):
        raise MutationContextError(
            "Mutation context requires a selected lifecycle record mapping."
        )

    try:
        file_id = governed_file_id(source_record.get("file_id"),
                                   subject="a mutation context")
    except FileManifestError as exc:
        raise MutationContextError(str(exc)) from exc

    if "classification" not in source_record:
        classification = None
    else:
        supplied = source_record["classification"]
        if not isinstance(supplied, Mapping):
            raise MutationContextError(
                "Mutation context classification must be a mapping when present."
            )
        classification = deepcopy(dict(supplied))

    consumed = source_record.get("source_record_id")
    if consumed is not None:
        if isinstance(consumed, bool) or not isinstance(consumed, int) or consumed <= 0:
            raise MutationContextError(
                "A consumed source record is identified by a positive "
                f"file_mutation_id; {consumed!r} is not one."
            )

    return MutationContext(
        file_id=file_id,
        classification=classification,
        source_record_id=consumed,
    )


def log_governed_source_file_mutation(
    ctx: Any,
    mutation_context: MutationContext,
    *,
    action: str,
    status: str,
    source_path: Path | str = "",
    destination_path: Path | str = "",
    recovery_path: Path | str = "",
    previous_version_path: Path | str = "",
    application_name: str = "",
    conversion: Mapping[str, Any] | None = None,
    operation: str = "",
    reason: str = "",
    message: str = "",
    run_log_fields: Mapping[str, Any] | None = None,
    transform_id: int | None = None,
    transform_snapshot: Mapping[str, Any] | None = None,
    destination: Mapping[str, Any] | None = None,
) -> int:
    """Write one mutation using only previously resolved governed context."""
    if not isinstance(mutation_context, MutationContext):
        raise MutationContextError(
            "A governed MutationContext is required before writing a mutation."
        )
    return _shared_log_source_file_mutation(
        ctx,
        action=action,
        status=status,
        source_path=source_path,
        destination_path=destination_path,
        recovery_path=recovery_path,
        previous_version_path=previous_version_path,
        application_name=application_name,
        file_id=mutation_context.file_id,
        classification=mutation_context.classification,
        source_record_id=mutation_context.source_record_id,
        conversion=conversion,
        operation=operation,
        reason=reason,
        message=message,
        run_log_fields=run_log_fields,
        transform_id=transform_id,
        transform_snapshot=transform_snapshot,
        destination=destination,
    )
