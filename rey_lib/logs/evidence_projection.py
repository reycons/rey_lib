"""Run-log evidence projection and read-only view helpers."""

from __future__ import annotations

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
        "source_path", "artifact_type", "producer", "metadata", "id",
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
            files["file_operations"]["files"].append(_file_operation_entry(record))
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
    # File operations are event-centric (one file may move several times), so they
    # are counted per operation rather than deduped by path.
    files["file_operations"]["count"] = len(files["file_operations"]["records"])
    total_files += len(files["file_operations"]["files"])
    files["count"] = total_files
    return sections


def _file_operation_entry(record: dict[str, Any]) -> dict[str, Any]:
    """Return a file-centric row for a FILE_OPERATION execution record."""
    path = str(record.get("target_path") or record.get("source_path")
               or record.get("path") or "")
    return {
        "path": path,
        "display_name": Path(path).name if path else "",
        "operation": str(record.get("operation") or ""),
        "source_path": str(record.get("source_path") or ""),
        "target_path": str(record.get("target_path") or ""),
        "destination_path": str(record.get("destination_path") or ""),
        "current_path": str(record.get("current_path") or ""),
        "status": str(record.get("status") or ""),
        "step_id": str(record.get("step_id") or ""),
        "exists": record.get("exists"),
        "size_bytes": record.get("size_bytes"),
        "modified_at": str(record.get("modified_at") or ""),
        "actions": ["view", "copy_path", "open_external"],
    }


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
                "sha256",
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

    return True
