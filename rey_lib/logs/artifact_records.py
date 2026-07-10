"""Artifact record helpers for shared run logs."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from rey_lib.logs.file_records import _file_evidence_metadata
from rey_lib.logs.record_enrichment import log_run_record


def log_artifact_reference(ctx: Any, path: str, *, role: str = "",
                           event: str = "created", created_by_step: str = "",
                           display_name: str = "", producer: str = "",
                           artifact_type: str = "", source_path: str = "",
                           viewer_type: str = "", safe_to_preview: bool | None = None,
                           **fields: Any) -> None:
    """Append an ARTIFACT_REFERENCE record (files/artifacts) for a created artifact.

    Only created/generated/written/exported/reported files are artifacts. Moved,
    copied, renamed, read, or deleted files are FILE_OPERATION execution records,
    not artifacts.

    Producers (redactor/loader/analyzer/…) should tag the artifact with a stable
    ``producer``, an ``artifact_type``, the originating ``source_path`` where one
    exists, and ``safe_to_preview`` so the console can group and safely preview
    artifacts from grounded evidence
    (SGC_Rey_Console_Run_Artifact_Evidence_And_File_Inspector). These fields are
    optional and only emitted when supplied, keeping older callers unchanged.
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
    if safe_to_preview is not None:
        extra["safe_to_preview"] = bool(safe_to_preview)
    file_metadata = _file_evidence_metadata(str(path))
    log_run_record(
        ctx, "ARTIFACT_REFERENCE",
        path=str(path), display_name=display_name or Path(str(path)).name,
        artifact_role=role, event=event, created_by_step=created_by_step,
        **file_metadata, **extra, **fields,
    )


def log_artifact_manifest(ctx: Any, artifacts: list[dict[str, Any]]) -> None:
    """Append the consolidated ARTIFACT_MANIFEST record (files/artifacts) at completion."""
    log_run_record(ctx, "ARTIFACT_MANIFEST", artifacts=artifacts)


def log_artifact_manifest_from_run_log(ctx: Any) -> None:
    """Append a consolidated ARTIFACT_MANIFEST built from this run's own records.

    Collects the artifacts already recorded on the append-only run log for this run
    and appends a single consolidated ARTIFACT_MANIFEST (files/artifacts). It reads
    only the run log — it never rescans directories or infers artifacts from
    filenames — and includes only files/artifacts entries, i.e. created/generated
    outputs. Moved, read, and copied files are FILE_OPERATION execution records and
    are never included (SGC_Rey_Run_Artifact_Naming_Convention). Meant to run at run
    completion, after RUN_COMPLETE/RUN_SUMMARY. Emission is fail-safe.
    """
    try:
        path = getattr(ctx, "run_log_path", None)
        if not path:
            return
        from rey_lib.logs.evidence_projection import read_run_log_sections

        artifacts = read_run_log_sections(path)["sections"]["files"]["artifacts"]["files"]
        if artifacts:
            log_artifact_manifest(ctx, artifacts)
    except Exception as exc:  # noqa: BLE001 — logging must never mask execution.
        logging.getLogger(__name__).warning(
            "run log: could not append ARTIFACT_MANIFEST: %s", exc
        )
