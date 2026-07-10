"""File, input, and config record helpers for shared run logs."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rey_lib.logs.record_enrichment import _CURRENT_RUN, log_run_record


def log_input_discovered(ctx: Any, *, input_name: str = "", path: str = "",
                         pattern: str = "", source_config: str = "",
                         exists: bool | None = None,
                         safe_to_preview: bool | None = None,
                         **fields: Any) -> None:
    """Append INPUT_DISCOVERED evidence for input file discovery."""
    payload: dict[str, Any] = {
        "input_name": input_name,
        "path": str(path),
        "pattern": pattern,
        "source_config": source_config,
        **fields,
    }
    if exists is not None:
        payload["exists"] = bool(exists)
    if safe_to_preview is not None:
        payload["safe_to_preview"] = bool(safe_to_preview)
    log_run_record(ctx, "INPUT_DISCOVERED", **payload)


def log_input_file_reference(ctx: Any, path: str, *, file_role: str = "",
                             display_name: str = "", consumed_by_step: str = "",
                             **fields: Any) -> None:
    """Append an INPUT_FILE_REFERENCE record (files/input_files) for a consumed input.

    Input files are files the run reads/consumes (source data, inbound files). They
    are not artifacts unless the run also writes a new run-owned output copy.
    """
    log_run_record(
        ctx, "INPUT_FILE_REFERENCE",
        path=str(path), display_name=display_name or Path(str(path)).name,
        file_role=file_role, source="runtime", consumed_by_step=consumed_by_step,
        **fields,
    )


def log_config_file_reference(ctx: Any, path: str, *, file_role: str = "",
                              display_name: str = "", consumed_by_step: str = "",
                              config_name: str = "", config_type: str = "",
                              exists: bool | None = None,
                              safe_to_preview: bool | None = None,
                              **fields: Any) -> None:
    """Append a CONFIG_FILE_REFERENCE record (files/config_files) for a run config file.

    Config files define or influence the run (workflow/pipeline/app YAML, contracts,
    templates). They are recorded from resolved config/provenance so the console
    reads them from the log rather than rescanning YAML or the filesystem.
    """
    config_path = Path(str(path))
    role = file_role or str(fields.get("config_role") or "")
    cfg_type = config_type or str(fields.get("configuration_layer") or role or "config")
    cfg_name = config_name or display_name or config_path.name
    payload = {
        "path": str(path),
        "display_name": display_name or cfg_name,
        "file_role": role,
        "config_name": cfg_name,
        "config_type": cfg_type,
        "source": fields.pop("source", "config_provenance"),
        "consumed_by_step": consumed_by_step,
        **fields,
    }
    if exists is None:
        exists = config_path.exists()
    payload["exists"] = bool(exists)
    if safe_to_preview is None:
        safe_to_preview = True
    payload["safe_to_preview"] = bool(safe_to_preview)
    if "config_hash" in payload and "hash" not in payload:
        payload["hash"] = payload["config_hash"]
    log_run_record(
        ctx, "CONFIG_FILE_REFERENCE",
        **payload,
    )


def log_config_file_manifest(ctx: Any, files: list[dict[str, Any]]) -> None:
    """Append the consolidated CONFIG_FILE_MANIFEST record (files/config_files)."""
    log_run_record(ctx, "CONFIG_FILE_MANIFEST", files=files)


def log_file_operation(ctx: Any, operation: str, *, source_path: str = "",
                       target_path: str = "", status: str = "success",
                       step_id: str = "", **fields: Any) -> None:
    """Append a FILE_OPERATION execution record for a file movement/operation.

    File movement (move/copy/rename/read/delete) is execution history, not artifact
    inventory. These records carry enough detail (from/to/status) to support
    rollback/recovery analysis derived from the append-only log rather than state
    files.
    """
    destination_path = str(fields.pop("destination_path", "") or target_path)
    current_path = str(fields.pop("current_path", "") or target_path or source_path)
    file_metadata = _file_evidence_metadata(current_path)
    payload: dict[str, Any] = {
        "operation": operation,
        "source_path": str(source_path),
        "target_path": str(target_path),
        "destination_path": destination_path,
        "current_path": current_path,
        "status": status,
        **file_metadata,
        **fields,
    }
    if step_id:
        payload["step_id"] = step_id
    log_run_record(ctx, "FILE_OPERATION", **payload)


def record_file_operation(operation: str, *, source_path: str = "",
                          target_path: str = "", status: str = "success",
                          **fields: Any) -> None:
    """Append a FILE_OPERATION to the bound run log, or no-op if no run is bound.

    Called by file_utils after a file operation; emission is fail-safe and never
    raises into the caller (a logging failure must not break a file operation).
    """
    run = _CURRENT_RUN["run"]
    if run is None:
        return
    try:
        log_file_operation(
            run, operation, source_path=source_path, target_path=target_path,
            status=status, **fields,
        )
    except Exception as exc:  # noqa: BLE001 — recording must never break a file op.
        logging.getLogger(__name__).warning(
            "run log: could not record file operation '%s': %s", operation, exc
        )


def _file_evidence_metadata(path: str) -> dict[str, Any]:
    """Return direct metadata for a referenced path without reading content."""
    if not path:
        return {}
    try:
        file_path = Path(path).expanduser()
        if not file_path.exists():
            return {"exists": False}
        if not file_path.is_file():
            return {"exists": True}
        stat = file_path.stat()
        return {
            "exists": True,
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(
                stat.st_mtime, timezone.utc
            ).isoformat(),
        }
    except OSError:
        return {}
