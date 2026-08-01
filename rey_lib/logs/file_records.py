"""File, input, and config record helpers for shared run logs."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

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
                             producing_app: str = "", status: str = "",
                             actions: Iterable[str] | None = None,
                             safe_to_preview: bool | None = None,
                             **fields: Any) -> None:
    """Append an INPUT_FILE_REFERENCE record (files/input_files) for a consumed input.

    Input files are files the run reads/consumes (source data, inbound files). A
    reference declares an operator-visible input in the run-level file inventory;
    it does not claim that the run created the input.
    """
    declaration = _file_declaration_metadata(
        ctx, path, artifact_group="input_files",
        producing_app=producing_app, producing_step=consumed_by_step,
        status=status, actions=actions, safe_to_preview=safe_to_preview,
    )
    log_run_record(
        ctx, "INPUT_FILE_REFERENCE",
        path=str(path), display_name=display_name or Path(str(path)).name,
        file_role=file_role, source="runtime", consumed_by_step=consumed_by_step,
        **declaration, **fields,
    )


def log_config_file_reference(ctx: Any, path: str, *, file_role: str = "",
                              display_name: str = "", consumed_by_step: str = "",
                              config_name: str = "", config_type: str = "",
                              producing_app: str = "", status: str = "",
                              actions: Iterable[str] | None = None,
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
    declaration = _file_declaration_metadata(
        ctx, path, artifact_group="config_files",
        producing_app=producing_app, producing_step=consumed_by_step,
        status=status, actions=actions, exists=exists,
        safe_to_preview=safe_to_preview,
    )
    payload = {
        "path": str(path),
        "display_name": display_name or cfg_name,
        "file_role": role,
        "config_name": cfg_name,
        "config_type": cfg_type,
        "source": fields.pop("source", "config_provenance"),
        "consumed_by_step": consumed_by_step,
        **declaration,
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
    declared: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or item.get("artifact_path") or "")
        if not path:
            continue
        declared.append({
            **item,
            **_file_declaration_metadata(
                ctx, path, artifact_group="config_files",
                producing_app=str(item.get("producing_app") or ""),
                producing_step=str(item.get("producing_step") or ""),
                status=str(item.get("status") or ""),
                actions=item.get("actions"),
                exists=item.get("exists"),
                safe_to_preview=item.get("safe_to_preview"),
            ),
        })
    log_run_record(ctx, "CONFIG_FILE_MANIFEST", files=declared)


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


def _file_declaration_metadata(
    ctx: Any,
    path: str,
    *,
    artifact_group: str,
    producing_app: str = "",
    producing_step: str = "",
    status: str = "",
    actions: Iterable[str] | None = None,
    viewer_type: str = "",
    exists: bool | None = None,
    safe_to_preview: bool | None = None,
) -> dict[str, Any]:
    """Return complete, producer-grounded manifest metadata for one file.

    ``artifact_group`` is supplied by the semantic declaration API or its caller;
    this helper never derives ownership from a filename, path, role, or record type.
    File state is captured once at declaration time so downstream consumers need not
    inspect the filesystem or reconstruct viewer capabilities.
    """
    evidence = _file_evidence_metadata(str(path))
    resolved_exists = bool(evidence.get("exists", False)) if exists is None else bool(exists)
    safe = True if safe_to_preview is None else bool(safe_to_preview)
    app = str(
        producing_app
        or getattr(ctx, "owner_app_name", "")
        or getattr(ctx, "app_name", "")
        or getattr(ctx, "name", "")
        or "unknown"
    )
    step = str(
        producing_step
        or getattr(ctx, "pipeline_step_name", "")
        or getattr(ctx, "step_name", "")
        or ""
    )
    if actions is None:
        resolved_actions = ["copy_path"] if path else []
        if resolved_exists:
            resolved_actions.append("open_external")
        if resolved_exists and safe:
            resolved_actions.insert(0, "view")
    else:
        resolved_actions = list(dict.fromkeys(str(action) for action in actions if action))
    metadata: dict[str, Any] = {
        "artifact_group": str(artifact_group),
        "producing_app": app,
        "producing_step": step,
        "status": str(status or ("available" if exists else "missing")),
        "actions": resolved_actions,
        "exists": resolved_exists,
        "safe_to_preview": safe,
        "size_bytes": int(evidence.get("size_bytes") or 0),
        "modified_at": str(evidence.get("modified_at") or ""),
    }
    if viewer_type:
        metadata["preferred_viewer"] = str(viewer_type)
    return metadata
