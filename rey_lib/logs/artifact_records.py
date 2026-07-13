"""Artifact record helpers for shared run logs."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

from rey_lib.logs.file_records import _file_declaration_metadata
from rey_lib.logs.record_enrichment import log_run_record


def log_artifact_reference(ctx: Any, path: str, *, role: str = "",
                           event: str = "created", created_by_step: str = "",
                           display_name: str = "", producer: str = "",
                           artifact_type: str = "", source_path: str = "",
                           artifact_group: str = "", producing_app: str = "",
                           producing_step: str = "", status: str = "",
                           actions: Iterable[str] | None = None,
                           viewer_type: str = "", safe_to_preview: bool | None = None,
                           **fields: Any) -> None:
    """Append an ARTIFACT_REFERENCE record (files/artifacts) for a created artifact.

    Only created/generated/written/exported/reported files are artifacts. Moved,
    copied, renamed, read, or deleted files are FILE_OPERATION execution records,
    not artifacts.

    Producers declare ``artifact_group`` as the broader Files-tree category and
    may provide ``producing_app``, ``producing_step``, source lineage, and viewer
    metadata. The shared projection passes those declarations through; it never
    classifies an artifact from its path, role, or filename.
    """
    extra: dict[str, Any] = {}
    if producer:
        extra["producer"] = producer
    if artifact_type:
        extra["artifact_type"] = artifact_type
    if source_path:
        extra["source_path"] = str(source_path)
    if viewer_type:
        extra["viewer_type"] = viewer_type
    declaration = _file_declaration_metadata(
        ctx, path, artifact_group=artifact_group,
        producing_app=producing_app or producer,
        producing_step=producing_step or created_by_step,
        status=status, actions=actions, viewer_type=viewer_type,
        safe_to_preview=safe_to_preview,
    )
    log_run_record(
        ctx, "ARTIFACT_REFERENCE",
        path=str(path), display_name=display_name or Path(str(path)).name,
        artifact_role=role, event=event, created_by_step=created_by_step,
        **declaration, **extra, **fields,
    )


def log_artifact_manifest(ctx: Any, artifacts: list[dict[str, Any]]) -> None:
    """Append the consolidated ARTIFACT_MANIFEST record (files/artifacts) at completion."""
    log_run_record(ctx, "ARTIFACT_MANIFEST", artifacts=artifacts)


def log_artifact_manifest_from_run_log(ctx: Any) -> None:
    """Append a consolidated ARTIFACT_MANIFEST built from this run's own records.

    Collects explicit input, config, and created-artifact declarations already
    recorded on this run's append-only log, then appends the single canonical
    ARTIFACT_MANIFEST. It never rescans directories, classifies from filenames, or
    promotes FILE_OPERATION execution evidence. Meant to run after RUN_COMPLETE;
    repeated finalization is idempotent and emission is fail-safe.
    """
    try:
        path = getattr(ctx, "run_log_path", None)
        if not path:
            return
        from rey_lib.logs.evidence_projection import (
            build_artifact_manifest_entries,
            read_run_log_sections,
        )

        payload = read_run_log_sections(path)
        records = payload["records"]
        if not any(str(record.get("record_type") or "").upper() == "RUN_COMPLETE"
                   for record in records):
            return
        if any(str(record.get("record_type") or "").upper() == "ARTIFACT_MANIFEST"
               for record in records):
            return
        artifacts = build_artifact_manifest_entries(records)
        # A completed run owns exactly one manifest, including when its inventory is
        # empty. This makes finalization idempotent and keeps cardinality independent
        # of whether the run happened to produce files.
        log_artifact_manifest(ctx, artifacts)
    except Exception as exc:  # noqa: BLE001 — logging must never mask execution.
        logging.getLogger(__name__).warning(
            "run log: could not append ARTIFACT_MANIFEST: %s", exc
        )
