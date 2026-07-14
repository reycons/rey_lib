"""Deterministic RESULTS_SUMMARY builder.

Builds the canonical run results document as a deterministic projection of a single
completed run log (one pipeline JSONL + canonical run_id). It replaces the earlier
RUN_SUMMARY record: the framework writes this as a pretty-printed
``<name>.<run_timestamp>.results.json`` file (not a JSONL record). The execution
JSONL remains the authoritative event log; this is a projection of it.

All sections are populated deterministically from the log evidence: run / execution /
step_results, plus item_results (keyed by input file), grouped artifacts with lineage,
validations, warnings (attributed to items only with a resolvable file key), and
failure diagnostics with verbatim error output. Optional fields (analysis_id, attempts,
exact expected-output name) appear only when a structured record supplies them — never
invented (SGC_Rey_Lib_Results_Summary_Diagnostic_Package_Correction).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rey_lib.logs.run_summary import (
    _elapsed_ms,
    _failed_step_ids,
    _rtype,
    _status,
    _step_counts,
)

RESULTS_SUMMARY_SCHEMA_VERSION = 1

_STEP_START = "STEP_START"
_STEP_END = "STEP_END"
_STEP_FAILURE = "STEP_FAILURE"
_RUN_COMPLETE = "RUN_COMPLETE"
_ERROR = "ERROR"
_WARNING = "WARNING"
_APP_EXECUTION = "APP_EXECUTION"
_VALIDATION_RESULT = "VALIDATION_RESULT"
_INPUT_FILE_REFERENCE = "INPUT_FILE_REFERENCE"
_ARTIFACT_REFERENCE = "ARTIFACT_REFERENCE"

_ANALYSIS_INPUT_ROLE = "analysis_input"
_ANALYSIS_RESULT_VALIDATION = "analysis_result"

# Backend artifact types that represent run context (rendered under execution_context).
_CONTEXT_ARTIFACT_TYPES = frozenset({"ctx_snapshot", "context"})
_DIAGNOSTIC_ARTIFACT_TYPES = frozenset({"diagnostic", "error_dump", "log"})
# Artifact types that are an item's produced output (the loader YAML / raw LLM result).
_OUTPUT_ARTIFACT_TYPES = frozenset({"llm_result", "raw_output", "loader_config"})
_RESULT_ARTIFACT_TYPE = "analysis_result"
_CONTEXT_RESULT_TYPE = "analysis_context"


def build_results_summary(
    sections: dict[str, Any],
    identity: dict[str, Any],
    records: list[dict[str, Any]],
    execution_details: dict[str, Any] | None,
    *,
    timestamp: str,
) -> dict[str, Any]:
    """Return the canonical RESULTS_SUMMARY document (deterministic; no LLM).

    Parameters
    ----------
    sections, identity, records
        The ``read_run_log_sections`` / ``_run_log_identity`` projections and raw
        records of one completed run log.
    execution_details
        Execution-layer facts (workflow/pipeline domain), used to enrich step_results.
    timestamp
        The results document creation time (the only non-deterministic field).
    """
    execution_records = sections.get("execution", {}).get("records", [])
    counts = _step_counts(execution_records)
    failed_ids = _failed_step_ids(execution_records)
    step_results = _step_results(execution_records, execution_details)
    items = _item_results(records)
    warnings, item_warnings = _partition_warnings(records)
    _attach_item_warnings(items, item_warnings)
    artifacts = _artifact_groups(records, items)
    # Partial success is detected at step OR item level: a failed step whose items
    # (or sibling steps) include a success is a partial failure, not a total failure.
    item_succeeded = any(str(i.get("status")) == "success" for i in items)
    partial_success = bool(failed_ids) and (counts["succeeded"] > 0 or item_succeeded)

    return {
        "record_type": "RESULTS_SUMMARY",
        "record_group": "results",
        "record_schema_version": RESULTS_SUMMARY_SCHEMA_VERSION,
        "run_id": identity["run_id"],
        "run_timestamp": identity["run_timestamp"],
        "timestamp": timestamp,
        "pipeline_name": identity["pipeline"] or identity["workflow"] or identity["app"],
        "status": identity["status"],
        "run": {
            "app": identity["app"],
            "execution_kind": _execution_kind(identity),
            "started_at": identity["run_started_at"],
            "ended_at": identity["run_completed_at"],
            "duration_ms": _elapsed_ms(identity["run_started_at"], identity["run_completed_at"]),
            "steps_total": counts["total"],
            "steps_succeeded": counts["succeeded"],
            "steps_failed": counts["failed"],
            "steps_warning": _warning_step_count(step_results),
            "steps_skipped": counts["skipped"],
            "steps_pending": 0,
        },
        "execution": {
            "outcome": _outcome(identity["status"], partial_success),
            "failed_step_ids": failed_ids,
            "partial_success": partial_success,
        },
        "step_results": step_results,
        "item_results": items,
        "validations": _validations(records),
        "warnings": warnings,
        "artifacts": artifacts,
        "diagnostics": _diagnostics(records),
    }


def _execution_kind(identity: dict[str, Any]) -> str:
    if identity.get("pipeline"):
        return "pipeline"
    if identity.get("workflow"):
        return "workflow"
    return "app"


def _outcome(status: str, partial_success: bool) -> str:
    """Classify the run outcome: success, partial_failure, or failed."""
    if str(status).lower() == "success":
        return "success"
    return "partial_failure" if partial_success else "failed"


def _warning_step_count(step_results: list[dict[str, Any]]) -> int:
    return sum(1 for s in step_results if s.get("status") == "warning")


def _step_results(
    execution_records: list[dict[str, Any]],
    execution_details: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Project one entry per executed step, in execution (sequence) order.

    Identity/sequence/duration come from the log's STEP_START/STEP_END records;
    app / operation / exit_code enrichment comes from execution_details when present
    (never fabricated). Steps are correlated by step id/name.
    """
    starts: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in execution_records:
        if _rtype(record) != _STEP_START:
            continue
        key = str(record.get("step_id") or record.get("step_name") or "")
        starts[key] = record
        order.append(key)
    ends = {str(r.get("step_name") or r.get("step_id") or ""): r
            for r in execution_records if _rtype(r) == _STEP_END}

    enrich = _execution_detail_steps(execution_details)

    results: list[dict[str, Any]] = []
    for key in order:
        start = starts.get(key, {})
        end = ends.get(key, {})
        detail = enrich.get(key, {})
        entry: dict[str, Any] = {
            "step_id": key,
            "step_name": str(start.get("step_name") or key),
            "step_sequence": start.get("step_sequence"),
            "app": detail.get("app") or str(start.get("app") or ""),
            "operation": detail.get("operation") or str(start.get("step_type") or ""),
            "status": str(end.get("status") or detail.get("status") or ""),
            "duration_ms": end.get("duration_ms"),
        }
        if "exit_code" in detail:
            entry["exit_code"] = detail["exit_code"]
        results.append(entry)
    return results


def _execution_detail_steps(
    execution_details: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Index execution_details steps by id/name for step enrichment (never fabricated)."""
    if not execution_details:
        return {}
    kind = str(execution_details.get("kind") or "").lower()
    domain = execution_details.get(kind) if isinstance(execution_details.get(kind), dict) else {}
    steps = domain.get("steps") if isinstance(domain, dict) else None
    if not isinstance(steps, list):
        return {}
    indexed: dict[str, dict[str, Any]] = {}
    for step in steps:
        if not isinstance(step, dict):
            continue
        key = str(step.get("id") or step.get("name") or "")
        if not key:
            continue
        entry: dict[str, Any] = {
            "app": str(step.get("app") or ""),
            "operation": str(step.get("process") or step.get("operation") or ""),
            "status": str(step.get("status") or ""),
        }
        if "exit_code" in step:
            entry["exit_code"] = step["exit_code"]
        indexed[key] = entry
    return indexed


def _validations(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project VALIDATION_RESULT records, in log order."""
    out: list[dict[str, Any]] = []
    for record in records:
        if _rtype(record) != _VALIDATION_RESULT:
            continue
        entry = {
            "validation_name": str(record.get("validation_name") or ""),
            "status": str(record.get("status") or ""),
        }
        source = record.get("source_file") or record.get("current_file") or record.get("path")
        if source:
            entry["source_file"] = str(source)
        out.append(entry)
    return out


def _warnings(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project WARNING records (retry/extraction history), in log order."""
    out: list[dict[str, Any]] = []
    for record in records:
        if _rtype(record) != _WARNING:
            continue
        entry: dict[str, Any] = {"message": str(record.get("message") or "")}
        source = record.get("source_file") or record.get("current_file")
        if source:
            entry["source_file"] = str(source)
        if record.get("attempt") is not None:
            entry["attempt"] = record["attempt"]
        out.append(entry)
    return out


# Explicit marker appended by error_utils._diagnostic_summary when it bounds text.
_TRUNCATION_MARKER = "...[truncated]"

# Error-bearing fields mapped to (stream, in-stream precedence). Lower precedence
# number wins when two fields of one stream carry proven-identical evidence. The
# tuple order is also the extraction order within a single record.
_ERROR_FIELDS: tuple[tuple[str, str, int], ...] = (
    ("stderr_summary", "stderr", 0),
    ("stdout_summary", "stdout", 0),
    ("full_error_output", "structured_error", 0),
    ("sanitized_exception", "structured_error", 1),
    ("error_message", "structured_error", 2),
    ("message", "structured_error", 3),
    ("sanitized_traceback", "traceback", 0),
    ("traceback_summary", "traceback", 1),
)

# Stream section order for the assembled string; index is the cross-stream rank
# (stderr_summary > stdout_summary > structured error > traceback).
_STREAM_SECTIONS: tuple[tuple[str, str], ...] = (
    ("stderr", "STDERR"),
    ("stdout", "STDOUT"),
    ("structured_error", "STRUCTURED ERROR"),
    ("traceback", "TRACEBACK"),
)
_STREAM_RANK = {stream: index for index, (stream, _label) in enumerate(_STREAM_SECTIONS)}


def _logical_failure_id(record: dict[str, Any], rtype: str,
                        known_error_ids: frozenset[str]) -> str:
    """Return the logical failure identity for an error-bearing record.

    Correlation is evidence-only: a STEP_FAILURE joins an ERROR when its
    ``failure_record_id`` matches that ERROR's ``error_id``; otherwise it falls back
    to a shared ``failed_step_id`` group. No fuzzy correlation (timestamps, message or
    traceback similarity) is ever attempted
    (SGC_Rey_Lib_Results_Summary_Diagnostic_Package_Correction).
    """
    if rtype == _ERROR:
        return str(record.get("error_id") or record.get("record_id") or "")
    if rtype == _STEP_FAILURE:
        failure_id = str(record.get("failure_record_id") or "")
        if failure_id and failure_id in known_error_ids:
            return failure_id
        step_id = str(record.get("failed_step_id") or "")
        if step_id:
            return f"step:{step_id}"
        return str(record.get("record_id") or failure_id or "")
    step_id = str(record.get("failed_step_id") or record.get("step_id") or "")
    return f"step:{step_id}" if step_id else ""


def _collect_error_blocks(records: list[dict[str, Any]],
                          known_error_ids: frozenset[str]) -> list[dict[str, Any]]:
    """Extract labelled error-evidence blocks from records in original log order.

    Each block records its stream, logical failure, and whether its own upstream
    evidence was already truncated. Truncation is asserted only from explicit signals:
    the ``output_truncated``/``truncated`` flags, the ``...[truncated]`` marker, or a
    ``traceback_summary`` standing in for an absent ``sanitized_traceback``.
    """
    blocks: list[dict[str, Any]] = []
    for record in records:
        rtype = _rtype(record)
        if rtype not in (_ERROR, _STEP_FAILURE, _APP_EXECUTION):
            continue
        record_id = str(record.get("record_id") or record.get("error_id")
                        or record.get("failure_record_id") or "")
        logical_id = _logical_failure_id(record, rtype, known_error_ids)
        flag_truncated = bool(record.get("output_truncated") or record.get("truncated"))
        has_full_traceback = bool(record.get("sanitized_traceback"))
        for field, stream, priority in _ERROR_FIELDS:
            value = record.get(field)
            if not value:
                continue
            text = str(value)
            truncated = False
            if stream in ("stderr", "stdout") and flag_truncated:
                truncated = True
            if text.rstrip().endswith(_TRUNCATION_MARKER):
                truncated = True
            if field == "traceback_summary" and not has_full_traceback:
                truncated = True
            blocks.append({
                "record_id": record_id,
                "record_type": rtype,
                "logical_failure_id": logical_id,
                "stream": stream,
                "priority": priority,
                "truncated": truncated,
                "text": text,
                "order": len(blocks),
            })
    return blocks


def _dedupe_error_blocks(
    blocks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Suppress proven-duplicate blocks, returning (kept-in-log-order, removed count).

    A block is removed only when it is an exact match or a strict contiguous substring
    of a retained block; partial overlaps, reordered text, and fuzzy similarity are
    never treated as duplicates. Processing longest-first (ties by stream precedence,
    then in-stream precedence, then log order) makes every subset see its superset
    already retained and lets the higher-precedence stream win exact matches.
    """
    ranked = sorted(
        blocks,
        key=lambda b: (-len(b["text"]), _STREAM_RANK[b["stream"]], b["priority"],
                       b["order"]),
    )
    kept: list[dict[str, Any]] = []
    removed = 0
    for block in ranked:
        if any(block["text"] in other["text"] for other in kept):
            removed += 1
            continue
        kept.append(block)
    kept.sort(key=lambda b: b["order"])
    return kept, removed


def _assemble_error_output(kept: list[dict[str, Any]]) -> str:
    """Join retained blocks into one labelled, deterministic string.

    Streams appear in a fixed section order with explicit headers so a downstream
    reader can distinguish stderr, stdout, structured errors, and tracebacks without
    consulting the source records. Blocks within a section stay in log order.
    """
    sections: list[str] = []
    for stream, label in _STREAM_SECTIONS:
        texts = [b["text"] for b in kept if b["stream"] == stream]
        if not texts:
            continue
        sections.append(f"===== {label} =====\n\n" + "\n\n".join(texts))
    return "\n\n".join(sections)


def _diagnostics(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble failure diagnostics with deduplicated, provenance-tracked evidence.

    Error text is gathered from every error-bearing record, correlated into logical
    failures, deduplicated (exact/subset only), and assembled into one labelled string
    plus per-block ``error_blocks`` metadata. Nothing is sanitized, reordered, or
    truncated by this builder; upstream truncation is reported honestly and an
    ``error_statistics`` inventory exposes how the package was assembled
    (SGC_Rey_Lib_Results_Summary_Diagnostic_Package_Correction).
    """
    complete = next((r for r in reversed(records) if _rtype(r) == _RUN_COMPLETE), {})
    known_error_ids = frozenset(
        str(r.get("error_id") or r.get("record_id") or "")
        for r in records if _rtype(r) == _ERROR
    ) - {""}
    failure_ids = [str(r.get("failure_record_id") or r.get("record_id") or "")
                   for r in records if _rtype(r) == _STEP_FAILURE]
    error_ids = [str(r.get("error_id") or r.get("record_id") or "")
                 for r in records if _rtype(r) == _ERROR]

    raw_blocks = _collect_error_blocks(records, known_error_ids)
    kept, removed = _dedupe_error_blocks(raw_blocks)

    logical_ids = {
        _logical_failure_id(r, _rtype(r), known_error_ids)
        for r in records if _rtype(r) in (_ERROR, _STEP_FAILURE)
    } - {""}
    truncated_source_ids: list[str] = []
    for block in kept:
        rid = block["record_id"]
        if block["truncated"] and rid and rid not in truncated_source_ids:
            truncated_source_ids.append(rid)

    error_blocks = [
        {"record_id": b["record_id"], "record_type": b["record_type"],
         "logical_failure_id": b["logical_failure_id"], "stream": b["stream"],
         "truncated": b["truncated"], "text": b["text"]}
        for b in kept
    ]
    statistics = {
        "logical_failures": len(logical_ids),
        "error_records": sum(1 for r in records if _rtype(r) == _ERROR),
        "step_failure_records": sum(1 for r in records if _rtype(r) == _STEP_FAILURE),
        "app_execution_records": sum(1 for r in records if _rtype(r) == _APP_EXECUTION),
        "duplicate_blocks_removed": removed,
        "stderr_blocks": sum(1 for b in kept if b["stream"] == "stderr"),
        "stdout_blocks": sum(1 for b in kept if b["stream"] == "stdout"),
        "structured_blocks": sum(1 for b in kept if b["stream"] == "structured_error"),
        "traceback_blocks": sum(1 for b in kept if b["stream"] == "traceback"),
    }

    diagnostics: dict[str, Any] = {
        "failed_step_id": str(complete.get("failed_step_id") or ""),
        "failed_step_name": str(complete.get("failed_step_name") or ""),
        "failure_record_ids": [i for i in failure_ids if i],
        "error_record_ids": [i for i in error_ids if i],
        "error_output_source": "application_subprocess" if kept else "none_recorded",
        "error_output_truncated": bool(truncated_source_ids),
        "error_blocks": error_blocks,
        "error_statistics": statistics,
        "full_error_output": _assemble_error_output(kept),
    }
    if truncated_source_ids:
        diagnostics["truncated_source_record_ids"] = truncated_source_ids
    return diagnostics


# --- Item correlation (evidence-gated; input file is the primary identity) -----

def _item_results(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build one item_result per distinct analyzer input file.

    Item identity resolves by precedence ARTIFACT_REFERENCE.source_path ->
    VALIDATION_RESULT.input_file -> INPUT_FILE_REFERENCE.path -> ctx.current_file.
    Absolute paths key exactly; a basename is a fallback and marks lineage unresolved
    when it is ambiguous. Only evidence-supported fields are populated; optional
    fields (analysis_id/attempts/expected name) stay absent unless a structured
    record supplies them (SGC_Rey_Lib_Results_Summary_Diagnostic_Package_Correction).
    """
    items: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def _ensure(path: str) -> dict[str, Any]:
        key = str(path or "")
        if key not in items:
            items[key] = {
                "input_path": key,
                "display_name": Path(key).name,
                "source_name": "",
                "analysis_name": "",
                "status": "",
                "result_path": None,
                "context_path": None,
                "output_path": None,
                "output_created": False,
                "producing_step": None,
                "lineage_resolved": True,
                "analysis_id_known": False,
                "warnings": [],
            }
            order.append(key)
        return items[key]

    for record in records:
        if _rtype(record) != _INPUT_FILE_REFERENCE:
            continue
        if str(record.get("file_role") or "") != _ANALYSIS_INPUT_ROLE:
            continue
        path = str(record.get("path") or "")
        if not path:
            continue
        item = _ensure(path)
        if record.get("display_name"):
            item["display_name"] = str(record["display_name"])
        if record.get("source_name"):
            item["source_name"] = str(record["source_name"])
        if record.get("analysis_name"):
            item["analysis_name"] = str(record["analysis_name"])

    basenames = _basename_index(order)

    for record in records:
        if _rtype(record) != _VALIDATION_RESULT:
            continue
        if str(record.get("validation_name") or "") != _ANALYSIS_RESULT_VALIDATION:
            continue
        key, resolved = _match_key(items, basenames, str(record.get("input_file") or ""), create=True)
        if key is None:
            continue
        item = _ensure(key)
        item["status"] = str(record.get("status") or item["status"])
        if not item["source_name"] and record.get("source_name"):
            item["source_name"] = str(record["source_name"])
        if not item["analysis_name"] and record.get("analysis_name"):
            item["analysis_name"] = str(record["analysis_name"])
        if not resolved:
            item["lineage_resolved"] = False
        _apply_analysis_id(item, record)

    for record in records:
        if _rtype(record) != _ARTIFACT_REFERENCE:
            continue
        source_path = str(record.get("source_path") or "")
        if not source_path:
            continue
        key, resolved = _match_key(items, basenames, source_path, create=False)
        if key is None or key not in items:
            continue
        item = items[key]
        atype = str(record.get("artifact_type") or "").lower()
        role = str(record.get("role") or "").lower()
        path = str(record.get("path") or "")
        if atype == _RESULT_ARTIFACT_TYPE:
            item["result_path"] = path
        elif atype == _CONTEXT_RESULT_TYPE:
            item["context_path"] = path
        elif atype in _OUTPUT_ARTIFACT_TYPES or role == "raw_output":
            item["output_path"] = path
            item["output_created"] = True
        producing_step = _producing_step(record)
        if producing_step and item["producing_step"] is None:
            item["producing_step"] = producing_step
        if not resolved:
            item["lineage_resolved"] = False
        _apply_analysis_id(item, record)

    result: list[dict[str, Any]] = []
    for key in order:
        item = items[key]
        if not item["status"]:
            item["status"] = "unknown"
        if item["producing_step"] is None:
            item["lineage_resolved"] = False
        result.append(item)
    return result


def _apply_analysis_id(item: dict[str, Any], record: dict[str, Any]) -> None:
    """Record analysis_id only when a structured field supplies it (never derived)."""
    analysis_id = record.get("analysis_id")
    if analysis_id and not item.get("analysis_id_known"):
        item["analysis_id"] = str(analysis_id)
        item["analysis_id_known"] = True


def _basename_index(paths: list[str]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for path in paths:
        index.setdefault(Path(path).name, []).append(path)
    return index


def _match_key(
    items: dict[str, dict[str, Any]],
    basenames: dict[str, list[str]],
    candidate: str,
    *,
    create: bool,
) -> tuple[str | None, bool]:
    """Resolve a candidate path to an item key. Returns (key_or_None, resolved).

    Exact absolute-path matches are resolved; a unique basename match is resolved; an
    ambiguous basename returns (None, False). An unseen path returns itself only when
    ``create`` is allowed (validations may introduce items; artifacts may not).
    """
    key = str(candidate or "")
    if not key:
        return None, True
    if key in items:
        return key, True
    matches = basenames.get(Path(key).name, [])
    if len(matches) == 1:
        return matches[0], True
    if len(matches) > 1:
        return None, False
    return (key, True) if create else (None, True)


def _producing_step(record: dict[str, Any]) -> str:
    return str(record.get("producing_step") or record.get("created_by_step")
               or record.get("step_name") or "")


# --- Warning attribution (deterministic; only with a resolvable file key) ------

def _partition_warnings(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Split WARNING records into run-level (unattributed) and item-level (by file)."""
    run_level: list[dict[str, Any]] = []
    item_level: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if _rtype(record) != _WARNING:
            continue
        entry: dict[str, Any] = {"message": str(record.get("message") or "")}
        if record.get("attempt") is not None:
            entry["attempt"] = record["attempt"]
        file_key = record.get("source_file") or record.get("current_file") or record.get("input_file")
        if file_key:
            item_level.setdefault(str(file_key), []).append({**entry, "attributed": True})
        else:
            run_level.append({**entry, "attributed": False})
    return run_level, item_level


def _attach_item_warnings(
    items: list[dict[str, Any]],
    item_warnings: dict[str, list[dict[str, Any]]],
) -> None:
    """Attach warnings to their item by exact path, then unique basename."""
    if not item_warnings:
        return
    by_base: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_base.setdefault(Path(item["input_path"]).name, []).append(item)
    for key, warns in item_warnings.items():
        target = next((i for i in items if i["input_path"] == key), None)
        if target is None:
            candidates = by_base.get(Path(key).name, [])
            target = candidates[0] if len(candidates) == 1 else None
        if target is not None:
            target["warnings"].extend(warns)


# --- Artifact grouping (with lineage) ------------------------------------------

def _artifact_groups(
    records: list[dict[str, Any]],
    items: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group run artifacts into inputs / created / failed_or_missing / diagnostics /
    execution_context, each with lineage. Deterministically ordered."""
    from rey_lib.logs.evidence_projection import normalize_artifacts

    created: list[dict[str, Any]] = []
    execution_context: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for artifact in normalize_artifacts(records):
        entry = _artifact_entry(artifact)
        atype = str(artifact.get("artifact_type") or "").lower()
        if atype in _CONTEXT_ARTIFACT_TYPES:
            execution_context.append(entry)
        elif atype in _DIAGNOSTIC_ARTIFACT_TYPES:
            diagnostics.append(entry)
        else:
            created.append(entry)

    return {
        "inputs": _sort_artifacts(_input_artifacts(records)),
        "created": _sort_artifacts(created),
        "failed_or_missing": _sort_artifacts(_missing_outputs(items)),
        "diagnostics": _sort_artifacts(diagnostics),
        "execution_context": _sort_artifacts(execution_context),
    }


def _artifact_entry(artifact: dict[str, Any]) -> dict[str, Any]:
    path = str(artifact.get("current_path") or artifact.get("path") or "")
    producing_step = str(artifact.get("producing_step") or "")
    source_path = str(artifact.get("source_path") or "")
    return {
        "path": path,
        "role": str(artifact.get("artifact_type") or artifact.get("role") or "") or "unknown",
        "status": str(artifact.get("status") or "") or "created",
        "source_path": source_path or None,
        "producing_app": str(artifact.get("producer") or ""),
        "producing_step": producing_step or None,
        "lineage_resolved": bool(producing_step) or bool(source_path),
    }


def _input_artifacts(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for record in records:
        if _rtype(record) != _INPUT_FILE_REFERENCE:
            continue
        path = str(record.get("path") or "")
        if not path:
            continue
        out.append({
            "path": path,
            "role": str(record.get("file_role") or "") or "input",
            "status": str(record.get("status") or "") or "referenced",
            "source_path": None,
            "producing_app": "",
            "producing_step": None,
            "lineage_resolved": True,
        })
    return out


def _missing_outputs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Represent failed items whose expected output was never created — no invention."""
    out: list[dict[str, Any]] = []
    for item in items:
        if str(item.get("status")) != "failed" or item.get("output_created"):
            continue
        expected = item.get("expected_output")
        out.append({
            "path": str(expected) if expected else None,
            "role": "expected_output",
            "status": "missing",
            "source_path": item["input_path"],
            "producing_app": "",
            "producing_step": item.get("producing_step"),
            "lineage_resolved": bool(item.get("lineage_resolved")),
            "expected_output_known": bool(expected),
        })
    return out


def _sort_artifacts(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(entries, key=lambda e: (str(e.get("role") or ""), str(e.get("path") or "")))
