"""Run-log evidence projection and read-only view helpers."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from rey_lib.logs.record_enrichment import (
    EXECUTION_RECORD_TYPES,
    FILES_RECORD_SUBGROUP,
    RUN_RESULT_RECORD_TYPES,
)


def log_file_metadata(path: Path, jsonl_stems: set[str] | None = None) -> dict[str, Any]:
    """Return JSONL-authority metadata for one log file path."""
    log_type = "jsonl" if path.suffix == ".jsonl" else "readable"
    return {
        "log_type": log_type,
        "authoritative": log_type == "jsonl",
        "derived": log_type != "jsonl",
        "derived_from": _derived_jsonl_path(path, jsonl_stems or set()),
    }


def read_jsonl_records(
    path: Path,
    content: str,
    *,
    filters: dict[str, str] | None = None,
    max_records: int = 250,
    truncated_file: bool = False,
) -> dict[str, Any]:
    """Parse and filter authoritative JSONL log records."""
    if path.suffix != ".jsonl":
        return {
            "path": str(path),
            "records": [],
            "records_matched": 0,
            "records_returned": 0,
            "truncated_file": truncated_file,
            "parse_errors": [],
            "error": "Structured log records are available only for JSONL logs.",
            **log_file_metadata(path),
        }

    selected_filters = filters or {}
    records: list[dict[str, Any]] = []
    parse_errors: list[str] = []

    for line_number, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            parse_errors.append(f"line {line_number}: {exc}")
            continue
        if _record_matches(record, selected_filters):
            records.append(record)

    limited_records = records[:max_records]
    return {
        "path": str(path),
        "records": limited_records,
        "records_matched": len(records),
        "records_returned": len(limited_records),
        "truncated_file": truncated_file,
        "parse_errors": parse_errors,
        "rendered_text": format_jsonl_records(limited_records),
        **log_file_metadata(path),
    }


_JSONL_EVENT_COLUMNS = [
    {"id": "timestamp", "label": "Time", "type": "datetime", "filter": True},
    {"id": "level", "label": "Level", "type": "text", "filter": True},
    {"id": "event", "label": "Event", "type": "text", "filter": True},
    {"id": "step", "label": "Step", "type": "text", "filter": True},
    {"id": "status", "label": "Status", "type": "text", "filter": True},
    {"id": "message", "label": "Message", "type": "text", "filter": True},
    {"id": "source_line", "label": "Source Line", "type": "number", "filter": True},
]


_SECRET_KEY_RE = re.compile(
    r"(secret|password|passwd|token|api[_-]?key|access[_-]?key|"
    r"credential|connection[_-]?string|private[_-]?key)",
    re.IGNORECASE,
)


def build_jsonl_event_table(
    *,
    raw_text: str | None = None,
    records: list[dict[str, Any]] | None = None,
    include_raw: bool = False,
) -> dict[str, Any]:
    """Return a UI-safe normalized table package for JSONL execution records.

    This helper owns durable JSONL log/event normalization. It parses one JSON
    object per line when raw text is supplied, preserves source line/index, keeps
    malformed-line errors local to the package, and redacts secret-like values
    before rows are returned to the console.
    """
    parsed_records: list[tuple[int, dict[str, Any]]] = []
    parse_errors: list[dict[str, Any]] = []

    if records is not None:
      for index, record in enumerate(records):
          if isinstance(record, dict):
              parsed_records.append((index, record))
    elif raw_text:
        for line_number, line in enumerate(str(raw_text).splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                parse_errors.append({
                    "line": line_number,
                    "message": str(exc),
                })
                continue
            if isinstance(record, dict):
                parsed_records.append((line_number, record))
            else:
                parse_errors.append({
                    "line": line_number,
                    "message": "JSONL line is not an object.",
                })

    rows: list[dict[str, Any]] = []
    error_count = 0
    warning_count = 0
    safe_records: list[dict[str, Any]] = []
    for row_index, (source_line, record) in enumerate(parsed_records):
        safe_record = _redact_jsonl_value(record)
        safe_records.append(safe_record)
        level = _first_text(safe_record, "level", "levelname", "severity")
        event = _first_text(safe_record, "record_type", "event_type", "event", "type")
        status = _first_text(safe_record, "status")
        if str(level).upper() == "ERROR" or str(event).upper() == "ERROR" or str(status).lower() == "error":
            error_count += 1
        if str(level).upper() in {"WARNING", "WARN"} or str(event).upper() in {"WARNING", "WARN"}:
            warning_count += 1
        rows.append({
            "id": str(safe_record.get("id") or safe_record.get("record_id") or f"jsonl:{source_line}:{row_index}"),
            "timestamp": _first_text(safe_record, "timestamp", "time", "created_at"),
            "level": level,
            "event": event,
            "step": _first_text(safe_record, "step_name", "step", "process", "process_name"),
            "status": status,
            "message": _first_text(safe_record, "message", "msg", "detail", "error"),
            "source_line": source_line,
            "raw_index": source_line,
        })

    result: dict[str, Any] = {
        "columns": list(_JSONL_EVENT_COLUMNS),
        "rows": rows,
        "summary": {
            "record_count": len(rows),
            "error_count": error_count,
            "warning_count": warning_count,
        },
        "parse_errors": parse_errors,
    }
    if include_raw:
        result["raw_text"] = (
            str(raw_text) if raw_text is not None
            else "\n".join(json.dumps(record, ensure_ascii=False) for record in safe_records)
        )
    return result


def _redact_jsonl_value(value: Any) -> Any:
    """Return a copy of value with secret-like keys masked."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            redacted[key_text] = "[REDACTED]" if _SECRET_KEY_RE.search(key_text) else _redact_jsonl_value(item)
        return redacted
    if isinstance(value, list):
        return [_redact_jsonl_value(item) for item in value]
    return value


def _first_text(record: dict[str, Any], *keys: str) -> str:
    """Return the first present record value as text, or empty string."""
    for key in keys:
        value = record.get(key)
        if value is not None:
            if isinstance(value, (dict, list)):
                return json.dumps(value, ensure_ascii=False, sort_keys=True)
            return str(value)
    return ""


_RUN_EXECUTION_TYPES = {
    "RUN_START",
    "STEP_START",
    "STEP_END",
    "INFO",
    "WARNING",
    "ERROR",
    "RUN_COMPLETE",
}


_RUN_RESULT_TYPES = {
    "RUN_SUMMARY",
    "EMAIL_SUMMARY",
    "LLM_ANALYSIS_PACKAGE",
    "LLM_ANALYSIS_RESULT",
    # Per-analysis LLM evidence, recognized by type so it is projected into the
    # results section and correlatable to its analysis even when a caller does not
    # set record_group (SGC_Rey_Lib_Canonical_LLM_Package_And_Contract_Evidence).
    "LLM_CONTRACT",
    "LLM_CONTEXT",
}


_ARTIFACT_CREATE_EVENTS = {
    "created",
    "generated",
    "written",
    "exported",
    "reported",
}


_ARTIFACT_IGNORE_EVENTS = {
    "moved",
    "copied",
    "renamed",
    "read",
    "touched",
    "deleted",
}


_MOVE_OPERATIONS = {
    "move", "moved", "copy", "copied", "rename", "renamed",
    "archive", "archived", "redact", "redacted",
}


_APP_TO_PRODUCER = {
    "file_operator": "redactor",
    # Read-only compatibility for persisted logs written before the app rename.
    "file_redactor": "redactor",
    "rey_loader": "loader",
    "rey_analyzer": "analyzer",
    "rey_messaging": "messaging",
    "rey_console": "console",
    "pipeline_coordinator": "pipeline_coordinator",
}


_LINK_FIELDS = (
    "path", "source_path", "target_path", "destination_path",
    "current_path", "artifact_id", "correlation_id",
)


def _redact_secret_metadata(value: Any) -> Any:
    """Return metadata with secret-like values masked (recurses dicts/lists)."""
    if isinstance(value, dict):
        return {
            key: ("***redacted***" if _SECRET_KEY_RE.search(str(key))
                  else _redact_secret_metadata(val))
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_redact_secret_metadata(item) for item in value]
    return value


def _artifact_lineage(records: list[dict[str, Any]]) -> dict[str, str]:
    """Return a source_path -> target_path map from FILE_OPERATION move events."""
    moves: dict[str, str] = {}
    for record in records:
        if str(record.get("record_type") or "").upper() != "FILE_OPERATION":
            continue
        if str(record.get("operation") or "").lower() not in _MOVE_OPERATIONS:
            continue
        source = str(record.get("source_path") or "")
        target = str(record.get("target_path") or record.get("destination_path")
                     or record.get("current_path") or "")
        if source and target:
            moves[source] = target
    return moves


def _resolve_current_path(path: str, moves: dict[str, str]) -> str:
    """Follow the movement chain from ``path`` to its final known location."""
    current = path
    seen: set[str] = set()
    while current in moves and current not in seen:
        seen.add(current)
        current = moves[current]
    return current


def _related_records(
    records: list[dict[str, Any]], keys: set[str],
) -> tuple[list[str], list[int]]:
    """Return (record ids, 1-based source lines) of records grounded to ``keys``."""
    ids: list[str] = []
    lines: list[int] = []
    for line, record in enumerate(records, start=1):
        values = {str(record.get(field) or "") for field in _LINK_FIELDS}
        values.discard("")
        if not values & keys:
            continue
        lines.append(line)
        record_id = (record.get("record_id") or record.get("artifact_id")
                     or record.get("correlation_id"))
        if record_id:
            ids.append(str(record_id))
    return ids, lines


def _producer_for(record: dict[str, Any]) -> str:
    """Return the artifact's producer from its explicit tag, else its app, else unknown."""
    producer = str(record.get("producer") or "").strip()
    if producer:
        return producer
    return _APP_TO_PRODUCER.get(str(record.get("app") or "").strip(), "unknown")


def _artifact_source_records(records: list[dict[str, Any]]):
    """Yield the records that create artifacts (references + manifest entries)."""
    for record in records:
        record_type = str(record.get("record_type") or "").upper()
        if record_type == "ARTIFACT_REFERENCE":
            event = str(record.get("event") or "").lower()
            if event and event not in _ARTIFACT_CREATE_EVENTS:
                continue
            yield record
        elif record_type in ("ARTIFACT_MANIFEST", "RELEVANT_FILE_MANIFEST"):
            for entry in _manifest_files(record):
                yield {**entry, "record_type": "ARTIFACT_MANIFEST",
                       "producer": record.get("producer"), "app": record.get("app")}


def _merge_artifact(into: dict[str, Any], other: dict[str, Any]) -> None:
    """Merge a duplicate artifact for the same file into an existing entry."""
    for field in (
        "source_path", "artifact_type", "producer", "producing_step", "metadata", "id",
        "exists", "size_bytes", "modified_at", "hash", "sha256",
    ):
        if not into.get(field) and other.get(field):
            into[field] = other[field]
    if other.get("safe_to_preview") is False:
        into["safe_to_preview"] = False
    for field in ("related_log_record_ids", "related_source_lines"):
        merged = list(dict.fromkeys(into[field] + other[field]))
        into[field] = merged


def _apply_redacted_preference(artifacts: list[dict[str, Any]]) -> None:
    """Prefer redacted outputs over the originals they were produced from."""
    by_path: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        by_path.setdefault(artifact["path"], artifact)
        by_path.setdefault(artifact["current_path"], artifact)
    for artifact in artifacts:
        is_redacted = (artifact.get("artifact_type") == "redacted_file"
                       or artifact.get("producer") == "redactor")
        source = artifact.get("source_path") or ""
        if not is_redacted or not source:
            continue
        original = by_path.get(source)
        if original is not None and original is not artifact:
            original["preferred"] = False
            original["redacted_by"] = artifact["path"]


def normalize_artifacts(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize run-log records into grounded, producer-tagged artifacts.

    Reads artifact-creating records (ARTIFACT_REFERENCE / manifest entries) and
    FILE_OPERATION movement events; returns one entry per artifact with its
    producer, type, movement-resolved ``current_path``, related log record
    ids/source lines, and secret-redacted metadata. Duplicate evidence for the
    same file is merged, and redacted outputs are preferred over their originals.
    The input is the parsed run-log record list (as returned by
    ``read_run_log_sections(...)["records"]``); no files are read.

    Parameters
    ----------
    records : list[dict[str, Any]]
        Parsed, ordered run-log records.

    Returns
    -------
    list[dict[str, Any]]
        Normalized artifact entries, in first-seen order.
    """
    records = list(records or [])
    moves = _artifact_lineage(records)
    by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for record in _artifact_source_records(records):
        path = str(record.get("path") or "")
        if not path:
            continue
        current = _resolve_current_path(path, moves)
        keys = {path, current}
        for field in ("source_path", "artifact_id", "correlation_id"):
            value = str(record.get(field) or "")
            if value:
                keys.add(value)
        ids, lines = _related_records(records, keys)
        artifact = {
            "id": str(record.get("artifact_id") or path),
            "label": str(record.get("display_name") or record.get("name")
                         or Path(path).name),
            "producer": _producer_for(record),
            # Producing step is typed evidence: ARTIFACT_REFERENCE records carry
            # created_by_step (step_name as a fallback where present). It is recovered
            # from the record here — never inferred from filenames, paths, or grouping.
            "producing_step": str(record.get("created_by_step")
                                  or record.get("step_name") or ""),
            "artifact_type": str(record.get("artifact_type")
                                 or record.get("artifact_role")
                                 or record.get("file_role") or ""),
            "path": path,
            "source_path": str(record.get("source_path") or ""),
            "current_path": current,
            "viewer_type": str(record.get("viewer_type") or "file"),
            "safe_to_preview": bool(record.get("safe_to_preview", True)),
            "preferred": True,
            "related_log_record_ids": ids,
            "related_source_lines": lines,
            "metadata": _redact_secret_metadata(record.get("metadata") or {}),
        }
        for field in ("exists", "size_bytes", "modified_at", "hash", "sha256"):
            if field in record:
                artifact[field] = record[field]
        key = current or path
        if key in by_key:
            _merge_artifact(by_key[key], artifact)
        else:
            by_key[key] = artifact
            order.append(key)

    artifacts = [by_key[key] for key in order]
    _apply_redacted_preference(artifacts)
    return artifacts


def build_artifact_manifest_entries(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the canonical operator-visible file inventory from declarations.

    Only explicit durable-file declarations participate. In particular,
    ``FILE_OPERATION`` is execution evidence and is never promoted into the
    manifest. Group ownership and viewer behavior pass through from producers;
    this projection does not derive either from paths, filenames, roles, or record
    types. Repeated declarations merge by canonical path while preserving the
    first declaration's position.
    """
    by_path: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for declaration in _manifest_declaration_records(records):
        path = str(declaration.get("path") or declaration.get("artifact_path") or "")
        artifact_group = str(declaration.get("artifact_group") or "").strip()
        if not path or not artifact_group:
            continue
        canonical = _canonical_artifact_path(path)
        entry = _manifest_entry(declaration, canonical, artifact_group)
        if canonical in by_path:
            _merge_manifest_entry(by_path[canonical], entry)
        else:
            by_path[canonical] = entry
            order.append(canonical)
    return [by_path[path] for path in order]


def _manifest_declaration_records(records: list[dict[str, Any]]):
    """Yield explicit inventory declarations, never file-operation evidence."""
    for record in records or []:
        record_type = str(record.get("record_type") or "").upper()
        if record_type == "ARTIFACT_REFERENCE":
            event = str(record.get("event") or "").lower()
            if event and event not in _ARTIFACT_CREATE_EVENTS:
                continue
            yield record
        elif record_type in ("INPUT_FILE_REFERENCE", "CONFIG_FILE_REFERENCE"):
            yield record
        elif record_type in ("CONFIG_FILE_MANIFEST", "RELEVANT_FILE_MANIFEST"):
            parent_fields = {
                "producing_app": record.get("producing_app") or record.get("app"),
                "producing_step": record.get("producing_step"),
            }
            for item in _manifest_files(record):
                yield {**parent_fields, **item}


def _canonical_artifact_path(path: str) -> str:
    """Return the stable canonical-path deduplication key."""
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return str(Path(path).expanduser().absolute())


def _manifest_entry(
    declaration: dict[str, Any], canonical_path: str, artifact_group: str,
) -> dict[str, Any]:
    """Project one producer declaration to the required manifest schema."""
    actions = declaration.get("actions") or []
    if isinstance(actions, str):
        actions = [actions]
    entry: dict[str, Any] = {
        "path": canonical_path,
        "display_name": str(
            declaration.get("display_name") or declaration.get("name")
            or Path(canonical_path).name
        ),
        "artifact_group": artifact_group,
        "file_role": str(
            declaration.get("file_role") or declaration.get("artifact_role")
            or declaration.get("role") or ""
        ),
        # Legacy aliases are compatibility-only; current producers emit the
        # authoritative producing_* fields directly.
        "producing_app": str(
            declaration.get("producing_app") or declaration.get("producer")
            or declaration.get("app") or "unknown"
        ),
        "producing_step": str(
            declaration.get("producing_step") or declaration.get("created_by_step")
            or declaration.get("consumed_by_step") or ""
        ),
        "status": str(declaration.get("status") or "unknown"),
        "actions": list(dict.fromkeys(str(action) for action in actions if action)),
        "exists": bool(declaration.get("exists", False)),
        "safe_to_preview": bool(declaration.get("safe_to_preview", False)),
        "size_bytes": int(declaration.get("size_bytes") or 0),
        "modified_at": str(declaration.get("modified_at") or ""),
    }
    optional_sources = {
        "source_path": ("source_path",),
        "operation": ("operation",),
        "mime_type": ("mime_type",),
        "extension": ("extension",),
        "preferred_viewer": ("preferred_viewer", "viewer_type"),
        "checksum": ("checksum", "sha256", "hash"),
        "lineage_resolved": ("lineage_resolved",),
        "temporary": ("temporary",),
        "retention": ("retention",),
        "metadata": ("metadata",),
        "restore_source_path": ("restore_source_path",),
        "restore_destination_path": ("restore_destination_path",),
        "restore_metadata": ("restore_metadata", "restore"),
    }
    for output_field, source_fields in optional_sources.items():
        value = next(
            (declaration.get(field) for field in source_fields if declaration.get(field) is not None),
            None,
        )
        if value not in (None, "", {}):
            entry[output_field] = (
                _redact_secret_metadata(value) if output_field == "metadata" else value
            )
    return entry


def _merge_manifest_entry(into: dict[str, Any], other: dict[str, Any]) -> None:
    """Merge a repeated canonical-path declaration deterministically."""
    # First declaration owns ordering and classification. Later declarations enrich
    # missing identity/lineage fields but never silently reclassify the artifact.
    for field in (
        "display_name", "file_role", "producing_app", "producing_step",
        "source_path", "operation", "mime_type", "extension",
        "preferred_viewer", "checksum", "lineage_resolved", "temporary",
        "retention", "metadata", "restore_source_path",
        "restore_destination_path", "restore_metadata",
    ):
        if (not into.get(field) or into.get(field) == "unknown") and other.get(field):
            into[field] = other[field]

    # State/capability metadata is allowed to become more current on a later explicit
    # declaration. A false safe_to_preview remains restrictive across all evidence.
    for field in ("status", "actions", "size_bytes", "modified_at"):
        if other.get(field) not in (None, "", []):
            into[field] = other[field]
    # False is meaningful current state, so presence rather than truthiness decides
    # whether a later explicit declaration updates it.
    if "exists" in other:
        into["exists"] = bool(other["exists"])
    into["safe_to_preview"] = bool(
        into.get("safe_to_preview", False) and other.get("safe_to_preview", False)
    )


def group_artifacts_by_producer(
    artifacts: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group normalized artifacts by their observed producer, in first-seen order."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for artifact in artifacts:
        groups.setdefault(artifact.get("producer") or "unknown", []).append(artifact)
    return groups


def read_run_log_sections(path: Path | str) -> dict[str, Any]:
    """Read an append-only run log and return section projections.

    The returned payload contains metadata and structured record projections only.
    File content preview belongs to file utilities, not log utilities.
    """
    log_path = Path(path).expanduser().resolve()
    records: list[dict[str, Any]] = []
    parse_errors: list[str] = []

    try:
        content = log_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "path": str(log_path),
            "exists": log_path.exists(),
            "records": [],
            "sections": _empty_run_sections(),
            "parse_errors": [str(exc)],
            **log_file_metadata(log_path),
        }

    for line_number, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            parse_errors.append(f"line {line_number}: {exc}")
            continue
        if isinstance(record, dict):
            records.append(record)

    return {
        "path": str(log_path),
        "exists": True,
        "records": records,
        "sections": _run_log_sections(records),
        "parse_errors": parse_errors,
        **log_file_metadata(log_path),
    }


def project_run_log(path: Path | str) -> dict[str, Any]:
    """Return a run-centered tree projection from one append-only run log."""
    sections_payload = read_run_log_sections(path)
    records = sections_payload["records"]
    sections = sections_payload["sections"]
    run = _run_log_identity(Path(sections_payload["path"]), records, sections)
    return {
        "run": run,
        "log": {
            "path": sections_payload["path"],
            "name": Path(sections_payload["path"]).name,
            "exists": sections_payload["exists"],
            "authoritative": sections_payload["authoritative"],
        },
        "sections": sections,
        "tree": _run_log_tree(run, sections),
        "parse_errors": sections_payload["parse_errors"],
    }


_RUN_SECTION_NAMES = (
    "execution",
    "input_files",
    "config_files",
    "file_operations",
    "artifacts",
    "results",
)


_RUN_FILE_SUBGROUPS = ("input_files", "config_files", "file_operations", "artifacts")


def run_summary(path: Path | str) -> dict[str, Any]:
    """Return one run's discovery summary — identity and counts, no raw records.

    This is the per-run row for run discovery (SGC_Rey_Run_Backend_Helper_API): the
    run identity, started/completed timestamps, status, warning/error counts, and
    the run-log path. It never returns raw log data.
    """
    payload = read_run_log_sections(path)
    identity = _run_log_identity(Path(payload["path"]), payload["records"], payload["sections"])
    return {
        "run_id": identity["run_id"],
        "run_timestamp": identity["run_timestamp"],
        "started_at": identity["run_started_at"],
        "completed_at": identity["run_completed_at"],
        "status": identity["status"],
        "warning_count": identity["warning_count"],
        "error_count": identity["error_count"],
        "app": identity["app"],
        "workflow": identity["workflow"],
        "pipeline": identity["pipeline"],
        "run_log_path": identity["log_path"],
    }


def discover_runs(log_dir: Path | str, *, limit: int = 50) -> list[dict[str, Any]]:
    """Discover recent runs under a log directory tree, newest first.

    Discovers run logs by file extension — ``*.jsonl`` and ``*.log`` — searched
    recursively beneath *log_dir*, so runs nested under a scope's log folder are
    found regardless of filename (there is no ``run_log*`` prefix convention:
    legacy ``run_log*`` files are discovered as ordinary ``*.jsonl``/``*.log``
    files). Only typed execution logs are kept, and every run's identity and
    ownership are derived exclusively from the parsed log records — never from the
    filename or directory name. Returns one lightweight summary per run
    (see :func:`run_summary`) — never raw log records. This is the run-discovery
    authority for the console backend (SGC_Rey_Console_Run_History_Log_Discovery_
    Correction, SGC_Rey_Run_Backend_Helper_API); the console must not scan
    directories itself.

    Parameters
    ----------
    log_dir : Path | str
        Root of a workflow/pipeline/app's run-log folder (searched recursively).
    limit : int
        Maximum number of runs to return (most recent first). 0 means no limit.

    Returns
    -------
    list[dict[str, Any]]
        Run summaries sorted by run_timestamp descending.
    """
    directory = Path(log_dir).expanduser()
    if not directory.is_dir():
        return []
    summaries: list[dict[str, Any]] = []
    run_log_paths = sorted(
        set(directory.rglob("*.jsonl")) | set(directory.rglob("*.log"))
    )
    for path in run_log_paths:
        payload = read_run_log_sections(path)
        if not _is_typed_run_log(payload["records"]):
            continue
        identity = _run_log_identity(Path(payload["path"]), payload["records"], payload["sections"])
        summaries.append({
            "run_id": identity["run_id"],
            "run_timestamp": identity["run_timestamp"],
            "started_at": identity["run_started_at"],
            "completed_at": identity["run_completed_at"],
            "status": identity["status"],
            "warning_count": identity["warning_count"],
            "error_count": identity["error_count"],
            "app": identity["app"],
            "workflow": identity["workflow"],
            "pipeline": identity["pipeline"],
            "run_log_path": identity["log_path"],
        })
    summaries.sort(key=lambda run: str(run.get("run_timestamp") or ""), reverse=True)
    return summaries[:limit] if limit else summaries


def get_run_section(path: Path | str, section: str) -> dict[str, Any]:
    """Return the records/files for one projected run section.

    ``section`` is one of execution, input_files, config_files, file_operations,
    artifacts, or results (SGC_Rey_Run_Backend_Helper_API). The projection comes
    from the append-only run log; no directory scan or filename inference occurs.

    Raises
    ------
    ValueError
        If ``section`` is not a known run section.
    """
    key = str(section or "").strip().lower()
    if key not in _RUN_SECTION_NAMES:
        raise ValueError(f"Unknown run section: {section!r}")
    sections = read_run_log_sections(path)["sections"]
    payload = sections["files"][key] if key in _RUN_FILE_SUBGROUPS else sections[key]
    return {"section": key, **payload}


def get_run_file_reference(path: Path | str, file_path: Path | str) -> dict[str, Any] | None:
    """Return the run-log reference entry for one file, or None if the run never used it.

    Looks the file up among the run's projected input/config/artifact/file-operation
    entries — the run log is the source of truth for which files belong to a run
    (SGC_Rey_Run_Backend_Helper_API). This returns the log-derived reference metadata
    (role, display name, owning section, actions); reading/previewing the file's
    contents is file_utils' responsibility, not this layer's.
    """
    targets = {str(file_path), str(Path(file_path).expanduser())}
    files = read_run_log_sections(path)["sections"]["files"]
    for key in _RUN_FILE_SUBGROUPS:
        for entry in files[key]["files"]:
            if str(entry.get("path")) in targets:
                return {"section": key, **entry}
    return None


def _empty_run_sections() -> dict[str, Any]:
    """Return the empty three-group projection (SGC_Rey_Log_Writer_Run_View_Groups)."""
    return {
        "execution": {"records": [], "count": 0},
        "files": {
            "input_files": {"records": [], "files": [], "count": 0},
            "config_files": {"records": [], "files": [], "count": 0},
            "file_operations": {"records": [], "files": [], "count": 0},
            "artifacts": {"records": [], "files": [], "count": 0},
            "count": 0,
        },
        "results": {"records": [], "count": 0},
    }


def _run_log_sections(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Project typed run-log records into execution/files/results groups.

    File movement (FILE_OPERATION) stays in execution and is additionally surfaced
    as a file-centric ``files.file_operations`` view. Only created/generated files
    become artifacts; moved/copied/read files never do.
    """
    sections = _empty_run_sections()
    files = sections["files"]
    # One canonical movement map for the whole run, reused for file-operation lineage
    # (never a second lineage algorithm).
    moves = _artifact_lineage(records)
    for record in records:
        record_type = str(record.get("record_type") or record.get("event_type") or "").upper()
        record_group = str(record.get("record_group") or "").lower()

        if record_type in _RUN_EXECUTION_TYPES or record_group == "execution":
            sections["execution"]["records"].append(record)

        if record_type in _RUN_RESULT_TYPES or record_group in ("results", "run_result"):
            sections["results"]["records"].append(record)

        if record_type == "INPUT_FILE_REFERENCE":
            files["input_files"]["records"].append(record)
            entry = _file_entry_from_record(record, "input")
            if entry:
                files["input_files"]["files"].append(entry)
        elif record_type in ("CONFIG_FILE_MANIFEST", "RELEVANT_FILE_MANIFEST"):
            files["config_files"]["records"].append(record)
            files["config_files"]["files"].extend(_manifest_files(record))
        elif record_type in ("CONFIG_FILE_REFERENCE", "RELEVANT_FILE"):
            files["config_files"]["records"].append(record)
            entry = _file_entry_from_record(record, "config")
            if entry:
                files["config_files"]["files"].append(entry)
        elif record_type == "FILE_OPERATION":
            files["file_operations"]["records"].append(record)
            files["file_operations"]["files"].append(
                _file_operation_entry(record, records, moves)
            )
        elif record_type == "ARTIFACT_MANIFEST":
            files["artifacts"]["records"].append(record)
            files["artifacts"]["files"].extend(_manifest_files(record))
        elif record_type == "ARTIFACT_REFERENCE":
            event = str(record.get("event") or "").lower()
            if event in _ARTIFACT_IGNORE_EVENTS:
                continue
            if event and event not in _ARTIFACT_CREATE_EVENTS:
                continue
            files["artifacts"]["records"].append(record)
            entry = _file_entry_from_record(record, "artifact")
            if entry:
                files["artifacts"]["files"].append(entry)

    sections["execution"]["count"] = len(sections["execution"]["records"])
    sections["results"]["count"] = len(sections["results"]["records"])
    total_files = 0
    for key in ("input_files", "config_files", "artifacts"):
        subgroup = files[key]
        subgroup["files"] = _dedupe_file_entries(subgroup["files"])
        subgroup["count"] = len(subgroup["files"])
        total_files += subgroup["count"]
    # File operations are deduped by stable evidence identity (repeated evidence for
    # the same operation collapses; genuinely distinct operations are preserved).
    files["file_operations"]["files"] = _dedupe_file_operations(
        files["file_operations"]["files"]
    )
    files["file_operations"]["count"] = len(files["file_operations"]["files"])
    total_files += len(files["file_operations"]["files"])
    files["count"] = total_files
    return sections


def _file_operation_id(record: dict[str, Any]) -> str:
    """Return a deterministic, stable id for a projected file operation.

    Derived from the operation's own typed evidence — run identity, step, operation
    type, and the RAW source/target/destination paths (never the lineage-resolved
    current path, so distinct moves in a chain stay distinct). The same source
    evidence always yields the same id; genuinely distinct operations differ. Never
    uses object identity, randomness, filenames, or position.
    """
    parts = [
        str(record.get("run_id") or ""),
        str(record.get("run_timestamp") or ""),
        str(record.get("step_name") or record.get("step_id") or ""),
        str(record.get("step_sequence") or ""),
        str(record.get("operation") or ""),
        str(record.get("source_path") or ""),
        str(record.get("target_path") or ""),
        str(record.get("destination_path") or ""),
    ]
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]
    ts = str(record.get("run_timestamp") or "")
    step = str(record.get("step_name") or record.get("step_id") or "step")
    seq = str(record.get("step_sequence") or "0")
    return f"fileop-{ts}-{step}-{seq}-{digest}"


def _file_operation_capabilities(current_path: str) -> dict[str, bool]:
    """Return capability flags for a file operation from its resolved physical path."""
    has_path = bool(current_path)
    return {"can_open": has_path, "can_copy_path": has_path}


# Action id -> capability flag that must be true to offer it.
_FILE_OP_ACTION_SPECS: tuple[tuple[str, str], ...] = (
    ("view", "can_open"),
    ("open_external", "can_open"),
    ("copy_path", "can_copy_path"),
)


def _file_operation_entry(
    record: dict[str, Any],
    records: list[dict[str, Any]],
    moves: dict[str, str],
) -> dict[str, Any]:
    """Return an enriched, file-centric row for a FILE_OPERATION execution record.

    Adds, from typed evidence only (SGC_Rey_Lib_File_Operation_Evidence_Backend_Projection):
    a stable operation id, the lineage-resolved current path across chained moves,
    related log record ids / source lines, viewer/open/copy capability flags (with
    capability-gated actions), and execution ownership metadata. Missing optional
    fields render as empty/None — never fabricated from filenames or paths.
    """
    source_path = str(record.get("source_path") or "")
    target_path = str(record.get("target_path") or "")
    destination_path = str(record.get("destination_path") or "")
    raw_path = target_path or source_path or str(record.get("path") or "")
    # Best current path: start from where this operation put the file (current ->
    # destination -> target -> source) then follow the movement chain to its end.
    endpoint = (str(record.get("current_path") or "") or destination_path
                or target_path or source_path)
    current_path = _resolve_current_path(endpoint, moves) if endpoint else ""

    keys = {source_path, target_path, destination_path, raw_path, endpoint, current_path}
    # Also ground related records by this operation's own correlation/artifact ids,
    # so correlated non-path records (e.g. STEP_END) are linked as evidence.
    for field in ("correlation_id", "artifact_id"):
        keys.add(str(record.get(field) or ""))
    keys.discard("")
    related_ids, related_lines = _related_records(records, keys)

    capabilities = _file_operation_capabilities(current_path)
    display_source = current_path or raw_path
    return {
        "id": _file_operation_id(record),
        "path": raw_path,
        "display_name": Path(display_source).name if display_source else "",
        "operation": str(record.get("operation") or ""),
        "source_path": source_path,
        "target_path": target_path,
        "destination_path": destination_path,
        "current_path": current_path,
        "status": str(record.get("status") or ""),
        "viewer_type": str(record.get("viewer_type") or "file"),
        "producing_app": str(record.get("producer") or record.get("app") or ""),
        "pipeline_name": str(record.get("pipeline_name") or ""),
        "run_id": str(record.get("run_id") or ""),
        "run_timestamp": str(record.get("run_timestamp") or ""),
        "step_id": str(record.get("step_id") or ""),
        "step_name": str(record.get("step_name") or ""),
        "step_sequence": record.get("step_sequence"),
        "related_log_record_ids": related_ids,
        "related_source_lines": related_lines,
        "exists": record.get("exists"),
        "size_bytes": record.get("size_bytes"),
        "modified_at": str(record.get("modified_at") or ""),
        "capabilities": capabilities,
        "actions": [
            action for action, flag in _FILE_OP_ACTION_SPECS if capabilities.get(flag)
        ],
    }


def _dedupe_file_operations(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse repeated evidence for the same operation by stable id, keeping order.

    Deduplicates on the deterministic operation id (stable evidence identity), so
    duplicate rows for the same operation collapse while genuinely distinct
    operations — different type or source/target — are preserved.
    """
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for entry in entries:
        entry_id = str(entry.get("id") or "")
        if entry_id in seen:
            continue
        seen.add(entry_id)
        deduped.append(entry)
    return deduped


def _manifest_files(record: dict[str, Any]) -> list[dict[str, Any]]:
    raw = record.get("files") or record.get("artifacts") or []
    files = raw.values() if isinstance(raw, dict) else raw
    result: list[dict[str, Any]] = []
    for item in files:
        if isinstance(item, str):
            result.append({"path": item, "display_name": Path(item).name})
        elif isinstance(item, dict):
            path = str(item.get("path") or item.get("artifact_path") or "")
            if not path:
                continue
            entry = {
                "path": path,
                "display_name": str(item.get("display_name") or item.get("name") or Path(path).name),
                "file_role": str(item.get("file_role") or item.get("role") or ""),
                "status": str(item.get("status") or ""),
                "actions": item.get("actions") or ["view", "copy_path", "open_external"],
            }
            for key in (
                "config_name", "config_type", "source", "exists", "hash",
                "config_hash", "safe_to_preview", "size_bytes", "modified_at",
                "sha256", "artifact_group", "producing_app", "producing_step",
                "preferred_viewer", "viewer_type", "source_path", "operation",
                "mime_type", "extension", "checksum", "lineage_resolved",
                "temporary", "retention", "metadata",
            ):
                if key in item:
                    entry[key] = item[key]
            result.append(entry)
    return result


def _file_entry_from_record(record: dict[str, Any], default_role: str) -> dict[str, Any] | None:
    path = str(record.get("path") or record.get("file_path") or record.get("artifact_path") or "")
    if not path:
        return None
    entry = {
        "path": path,
        "display_name": str(record.get("display_name") or record.get("name") or Path(path).name),
        "file_role": str(record.get("file_role") or record.get("role")
                         or record.get("artifact_role") or default_role),
        "step_name": str(record.get("step_name") or ""),
        "status": str(record.get("status") or ""),
        "actions": ["view", "copy_path", "open_external"],
    }
    for key in (
        "config_name", "config_type", "source", "exists", "hash",
        "config_hash", "safe_to_preview", "size_bytes", "modified_at",
        "sha256",
    ):
        if key in record:
            entry[key] = record[key]
    return entry


def _dedupe_file_entries(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for file in files:
        path = str(file.get("path") or "")
        if path:
            rows[path] = file
    return sorted(rows.values(), key=lambda row: str(row.get("display_name") or row.get("path") or "").lower())


def _run_log_identity(path: Path, records: list[dict[str, Any]], sections: dict[str, Any]) -> dict[str, Any]:
    first = records[0] if records else {}
    complete = next(
        (
            record for record in reversed(records)
            if str(record.get("record_type") or "").upper() == "RUN_COMPLETE"
        ),
        {},
    )
    warning_count = sum(
        1 for record in records
        if str(record.get("record_type") or "").upper() == "WARNING"
    )
    error_count = sum(
        1 for record in records
        if str(record.get("record_type") or "").upper() == "ERROR"
    )
    return {
        "run_id": str(first.get("run_id") or ""),
        "run_timestamp": str(first.get("run_timestamp") or _timestamp_from_run_log_name(path)),
        "run_started_at": str(first.get("run_started_at") or first.get("timestamp") or ""),
        "run_completed_at": str(complete.get("timestamp") or ""),
        "status": str(complete.get("status") or ""),
        "warning_count": warning_count,
        "error_count": error_count,
        "app": str(first.get("app") or ""),
        "workflow": str(first.get("workflow") or first.get("workflow_name") or ""),
        "pipeline": str(first.get("pipeline") or first.get("pipeline_name") or ""),
        "log_path": str(path),
        "log_display_name": path.name,
        "execution_count": sections["execution"]["count"],
        "input_file_count": sections["files"]["input_files"]["count"],
        "config_file_count": sections["files"]["config_files"]["count"],
        "file_operation_count": sections["files"]["file_operations"]["count"],
        "artifact_count": sections["files"]["artifacts"]["count"],
        "file_count": sections["files"]["count"],
        "result_count": sections["results"]["count"],
    }


def _is_typed_run_log(records: list[dict[str, Any]]) -> bool:
    """Return True when parsed records match the shared run-log schema."""
    for record in records:
        if not isinstance(record, dict):
            continue
        record_type = str(record.get("record_type") or "").upper()
        if not record_type:
            continue
        if not (record.get("run_id") or record.get("run_timestamp")):
            continue
        if (
            record_type in EXECUTION_RECORD_TYPES
            or record_type in RUN_RESULT_RECORD_TYPES
            or record_type in FILES_RECORD_SUBGROUP
        ):
            return True
    return False


def _run_log_tree(run: dict[str, Any], sections: dict[str, Any]) -> dict[str, Any]:
    run_key = run.get("run_id") or run.get("run_timestamp") or run.get("log_display_name")
    files = sections["files"]
    return {
        "id": f"run:{run_key}",
        "label": str(run.get("run_timestamp") or run.get("log_display_name") or "Run"),
        "kind": "run",
        "status": str(run.get("status") or ""),
        "children": [
            {"id": f"run:{run_key}:execution", "label": "Execution", "kind": "execution", "count": sections["execution"]["count"], "children": []},
            {
                "id": f"run:{run_key}:files",
                "label": "Files",
                "kind": "files",
                "count": files["count"],
                "children": [
                    {
                        "id": f"run:{run_key}:input-files",
                        "label": "Input Files",
                        "kind": "input_files",
                        "count": files["input_files"]["count"],
                        "children": [_file_tree_node(file, "input_file") for file in files["input_files"]["files"]],
                    },
                    {
                        "id": f"run:{run_key}:config-files",
                        "label": "Config Files",
                        "kind": "config_files",
                        "count": files["config_files"]["count"],
                        "children": [_file_tree_node(file, "config_file") for file in files["config_files"]["files"]],
                    },
                    {
                        "id": f"run:{run_key}:artifacts",
                        "label": "Artifacts",
                        "kind": "artifacts",
                        "count": files["artifacts"]["count"],
                        "children": [_file_tree_node(file, "artifact") for file in files["artifacts"]["files"]],
                    },
                ],
            },
            {"id": f"run:{run_key}:results", "label": "Results", "kind": "results", "count": sections["results"]["count"], "children": []},
        ],
    }


def _file_tree_node(file: dict[str, Any], kind: str) -> dict[str, Any]:
    path = str(file.get("path") or "")
    return {
        "id": f"{kind}:{path}",
        "label": str(file.get("display_name") or Path(path).name),
        "kind": kind,
        "path": path,
        "status": str(file.get("status") or ""),
        "children": [],
    }


def _timestamp_from_run_log_name(path: Path) -> str:
    match = re.search(r"\.(\d{8}_\d{6})\.jsonl$", path.name)
    return match.group(1) if match else ""


def _run_records_for_view(source: Any) -> list[dict[str, Any]]:
    """Normalise a path or a record list into a list of run-log records."""
    if isinstance(source, (str, Path)):
        return read_run_log_sections(source)["records"]
    return [record for record in source if isinstance(record, dict)]


def _run_header_lines(records: list[dict[str, Any]]) -> list[str]:
    """Return human-readable run-identity header lines."""
    identity = _run_log_identity(Path(""), records, _run_log_sections(records))
    lines = [
        f"Run {identity['run_timestamp'] or identity['run_id'] or '(unknown)'}",
        f"  status:    {identity['status'] or 'in-progress'}",
        f"  run_id:    {identity['run_id']}",
        f"  started:   {identity['run_started_at']}",
        f"  completed: {identity['run_completed_at']}",
    ]
    for label, key in (("app", "app"), ("workflow", "workflow"), ("pipeline", "pipeline")):
        if identity[key]:
            lines.append(f"  {label}:{' ' * (9 - len(label))}{identity[key]}")
    if identity["warning_count"] or identity["error_count"]:
        lines.append(
            f"  warnings:  {identity['warning_count']}   errors: {identity['error_count']}"
        )
    return lines


def _render_file_block(label: str, subgroup: str, entries: list[dict[str, Any]]) -> str:
    """Render one files subgroup (input/config/artifacts/file-operations) as text."""
    if not entries:
        return f"{label} (0)\n  (none)"
    lines = [f"{label} ({len(entries)})"]
    for entry in entries:
        if subgroup == "file_operations":
            lines.append(
                f"  {entry.get('operation', '')}: {entry.get('source_path', '')}"
                f" -> {entry.get('target_path', '')} [{entry.get('status', '')}]"
            )
        else:
            role = str(entry.get("file_role") or "")
            suffix = f"  · {role}" if role else ""
            lines.append(f"  {entry.get('display_name') or entry.get('path')}{suffix}")
    return "\n".join(lines)


def render_execution_view(source: Any) -> str:
    """Render the execution audit trail from a JSONL run log (or records)."""
    sections = _run_log_sections(_run_records_for_view(source))
    return format_jsonl_records(sections["execution"]["records"]) or "(no execution records)"


def render_files_view(source: Any) -> str:
    """Render the Files group (input/config/file-operations/artifacts) as text."""
    files = _run_log_sections(_run_records_for_view(source))["files"]
    blocks = [
        _render_file_block("Input Files", "input_files", files["input_files"]["files"]),
        _render_file_block("Config Files", "config_files", files["config_files"]["files"]),
        _render_file_block("File Operations", "file_operations", files["file_operations"]["files"]),
        _render_file_block("Artifacts", "artifacts", files["artifacts"]["files"]),
    ]
    return "\n\n".join(blocks)


def render_results_view(source: Any) -> str:
    """Render the results records (summaries, analyses) from the run log."""
    sections = _run_log_sections(_run_records_for_view(source))
    return format_jsonl_records(sections["results"]["records"]) or "(no results)"


def render_summary_view(source: Any) -> str:
    """Render the run header plus the deterministic RUN_SUMMARY, if present."""
    records = _run_records_for_view(source)
    lines = list(_run_header_lines(records))
    summary = next(
        (record.get("summary") for record in reversed(records)
         if str(record.get("record_type") or "").upper() == "RUN_SUMMARY"
         and isinstance(record.get("summary"), dict)),
        None,
    )
    if summary:
        lines.append("Summary")
        lines.extend(f"  {key}: {value}" for key, value in summary.items())
    return "\n".join(lines)


def render_error_warning_view(source: Any) -> str:
    """Render only the WARNING/ERROR records from the run log."""
    records = _run_records_for_view(source)
    flagged = [
        record for record in records
        if str(record.get("record_type") or "").upper() in ("WARNING", "ERROR")
    ]
    return format_jsonl_records(flagged) if flagged else "(no warnings or errors)"


def render_run_view(source: Any) -> str:
    """Render the full human-readable run view (header + execution/files/results)."""
    records = _run_records_for_view(source)
    return "\n".join([
        *_run_header_lines(records),
        "",
        "== Execution ==",
        render_execution_view(records),
        "",
        "== Files ==",
        render_files_view(records),
        "",
        "== Results ==",
        render_results_view(records),
    ])


def format_jsonl_records(records: list[dict[str, Any]]) -> str:
    """Return a compact human-readable rendering of JSONL log records."""
    lines: list[str] = []
    for record in records:
        timestamp = str(record.get("timestamp") or record.get("asctime") or "")
        level = str(record.get("level") or record.get("levelname") or "").upper()
        source = str(record.get("source") or record.get("name") or "")
        message = str(record.get("message") or "")
        prefix = "  ".join(part for part in (timestamp, level, source) if part)
        lines.append(f"{prefix}  {message}" if prefix else message)

        details = _record_detail_lines(record)
        if details:
            lines.extend(f"  {line}" for line in details)
        lines.append("")

    return "\n".join(lines).rstrip()


def _record_detail_lines(record: dict[str, Any]) -> list[str]:
    """Return stable detail lines for non-envelope JSONL fields."""
    envelope = {
        "asctime",
        "created",
        "depth",
        "level",
        "levelname",
        "message",
        "name",
        "parent_sequence",
        "sequence",
        "source",
        "timestamp",
    }
    lines: list[str] = []
    for key in sorted(k for k in record if k not in envelope):
        value = record[key]
        if value in (None, "", [], {}):
            continue
        rendered = json.dumps(value, default=str, sort_keys=True)
        lines.append(f"{key}: {rendered}")
    return lines


def _derived_jsonl_path(path: Path, jsonl_stems: set[str]) -> str:
    """Return matching JSONL source path for a readable log when present."""
    if path.suffix == ".jsonl":
        return ""
    stem = path.with_suffix("").as_posix()
    return f"{stem}.jsonl" if stem in jsonl_stems else ""


def _split_filter_values(raw: str | None) -> set[str]:
    """Split a comma-separated filter value into a set of non-empty tokens.

    Supports multi-select record-type/record-group filters passed as a single
    comma-separated request parameter; a single value yields a one-element set.
    """
    if not raw:
        return set()
    return {token.strip() for token in str(raw).split(",") if token.strip()}


def _record_matches(record: dict[str, Any], filters: dict[str, str]) -> bool:
    """Return true when a JSONL record matches all requested filters."""
    if filters.get("errors_only") == "true":
        level = str(record.get("level", record.get("levelname", ""))).upper()
        if level not in {"ERROR", "CRITICAL"}:
            return False

    for key in ("level", "app", "pipeline_run_id", "pipeline_step_name", "batch_id", "file_name"):
        expected = filters.get(key)
        if expected and str(record.get(key, "")) != expected:
            return False

    for key in ("record_type", "record_group"):
        selected = _split_filter_values(filters.get(key))
        if selected and str(record.get(key, "")) not in selected:
            return False

    return True
