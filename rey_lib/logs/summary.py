"""Post-execution run-results finalization framework.

The single shared entry point that finalizes a completed run after its terminal
record is durably written. It reads the completed JSONL log (the authoritative event
log) and writes a deterministic, pretty-printed ``<name>.<run_timestamp>.results.json``
document — the canonical RESULTS_SUMMARY. This replaces the earlier RUN_SUMMARY
record: the results document is a projection of the log, not a JSONL record appended
to it.

The execution layer contributes only execution-specific facts (``execution_details``);
this framework owns building, the deterministic file output, and idempotency
(SGC_Rey_Lib_Log_Summary_Framework_And_Run_Summary,
 SGC_Rey_Lib_Results_Summary_Diagnostic_Package_Correction).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rey_lib.logs.results_summary import build_results_summary

# When a run exceeds this many steps, the per-step list in execution_details is
# dropped (the authoritative per-step detail stays in the log's STEP records) and a
# steps_truncated flag is set — a bound that never truncates diagnostic error output.
_MAX_EMBEDDED_STEPS = 250

_RUN_COMPLETE = "RUN_COMPLETE"


def finalize_run_log(ctx: Any, execution_details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Finalize a completed run by writing the canonical RESULTS_SUMMARY document.

    Invoked once by each terminal execution path (app / workflow / pipeline) after
    the terminal RUN_COMPLETE record is durably written. Reads the completed JSONL
    log and writes ``<name>.<run_timestamp>.results.json`` beside it — a deterministic
    projection. Diagnostic error output is preserved verbatim (never sanitized or
    truncated here).

    Parameters
    ----------
    ctx : Any
        The run context (exposes ``run_log_path``).
    execution_details : dict[str, Any] | None
        Execution-specific facts from the execution layer (namespaced by ``kind``);
        used to enrich step_results. ``None`` for plain apps.

    Returns
    -------
    dict[str, Any]
        ``results_written``, ``results_path``, ``skipped``, ``failures``.
    """
    from rey_lib.logs.evidence_projection import _run_log_identity, read_run_log_sections

    result: dict[str, Any] = {
        "results_written": False,
        "results_path": None,
        "skipped": [],
        "failures": [],
    }

    path = getattr(ctx, "run_log_path", None)
    if not path:
        result["skipped"].append("no_run_log_path")
        return result
    try:
        payload = read_run_log_sections(path)
    except Exception as exc:  # noqa: BLE001 — finalization must never mask execution
        result["failures"].append(str(exc))
        return result

    records = payload["records"]
    sections = payload["sections"]

    if not _has_record_type(records, _RUN_COMPLETE):
        result["skipped"].append("no_terminal_record")
        return result

    try:
        identity = _run_log_identity(Path(payload["path"]), records, sections)
        summary = build_results_summary(
            sections, identity, records, _normalize_execution_details(execution_details),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        results_path = _results_path(payload["path"])
        # Pretty-printed JSON document; default=str keeps it write-safe. Diagnostic
        # error output is written verbatim — no sanitization, no truncation.
        results_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001 — preserve the log; report; never raise
        result["failures"].append(str(exc))
        return result

    result["results_written"] = True
    result["results_path"] = str(results_path)
    return result


def _results_path(run_log_path: str) -> Path:
    """Return ``<name>.<run_timestamp>.results.json`` beside the run log."""
    log = Path(run_log_path)
    return log.parent / (log.stem + ".results.json")


def _has_record_type(records: list[dict[str, Any]], record_type: str) -> bool:
    """Return True when any record matches ``record_type`` (case-insensitive)."""
    target = record_type.upper()
    return any(str(r.get("record_type") or "").upper() == target for r in records)


def _normalize_execution_details(execution_details: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize caller execution_details: default app kind + per-step size cap."""
    if not execution_details:
        return {"kind": "app"}
    details = dict(execution_details)
    kind = str(details.get("kind") or "").strip().lower()
    if kind in ("workflow", "pipeline") and isinstance(details.get(kind), dict):
        details[kind] = _cap_embedded_steps(dict(details[kind]))
    return details


def _cap_embedded_steps(domain: dict[str, Any]) -> dict[str, Any]:
    """Drop the per-step list and flag truncation when it exceeds the embed limit."""
    steps = domain.get("steps")
    if isinstance(steps, list) and len(steps) > _MAX_EMBEDDED_STEPS:
        domain.pop("steps", None)
        domain["steps_truncated"] = True
    return domain
