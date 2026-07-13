"""Post-execution run-results finalization.

``create_results_summary`` is the single explicit utility that builds the canonical
RESULTS_SUMMARY from a completed JSONL run log and appends it as the final record of
that same log. It is called explicitly by the top-level application that owns the run
(a standalone app, or pipeline_coordinator / the workflow coordinator) after its final
RUN_COMPLETE — never automatically in generic app-operation finalization, so nested
pipeline sub-apps that share the owner's log do not create an early, incorrect summary.
rey_console calls the same utility on demand for a selected run log.

The execution layer contributes only execution-specific facts (``execution_details``);
this module owns building, the durable append, and idempotency
(SGC_Rey_Lib_Log_Summary_Framework_And_Run_Summary,
 SGC_Rey_Lib_Results_Summary_Diagnostic_Package_Correction,
 SGC_Rey_Lib_Write_Results_Summary_To_Run_Log,
 SGC_Rey_Lib_Explicit_Results_Summary_Creation).
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
_RESULTS_SUMMARY = "RESULTS_SUMMARY"


def create_results_summary(
    ctx: Any = None,
    *,
    log_path: str | Path | None = None,
    execution_details: dict[str, Any] | None = None,
    replace_existing: bool = False,
) -> dict[str, Any]:
    """Create (or recreate) the canonical RESULTS_SUMMARY for a completed run log.

    The explicit finalization utility. The top-level run-owning application calls it
    after its final RUN_COMPLETE; rey_console calls it on demand for a selected log.
    It reads the JSONL run log, excludes any prior RESULTS_SUMMARY from the source
    evidence, builds the deterministic summary with ``build_results_summary``, and
    appends exactly one RESULTS_SUMMARY as the final record — preserving every other
    record and its order. Diagnostic error output is preserved verbatim (never
    sanitized or truncated here).

    Parameters
    ----------
    ctx : Any
        The run context (exposes ``run_log_path``); used when ``log_path`` is omitted.
    log_path : str | Path | None
        An explicit run-log path (e.g. from rey_console for a selected run).
    execution_details : dict[str, Any] | None
        Execution-specific facts from the execution layer (namespaced by ``kind``);
        used to enrich step_results. ``None`` for plain apps.
    replace_existing : bool
        When False (default), an existing terminal RESULTS_SUMMARY is returned
        unchanged (no duplicate). When True, existing RESULTS_SUMMARY records are
        removed (every other record left unchanged and in order) and one freshly
        built RESULTS_SUMMARY is appended as the final record.

    Returns
    -------
    dict[str, Any]
        ``summary`` (the RESULTS_SUMMARY object or None), ``action``
        (``"created"`` / ``"existing"`` / ``"replaced"`` / None), ``log_path``,
        ``skipped``, ``failures``.
    """
    from rey_lib.logs.evidence_projection import (
        _run_log_identity,
        _run_log_sections,
        read_run_log_sections,
    )
    from rey_lib.files import primitive_file_io

    result: dict[str, Any] = {
        "summary": None,
        "action": None,
        "log_path": None,
        "skipped": [],
        "failures": [],
    }

    resolved = log_path or getattr(ctx, "run_log_path", None)
    if not resolved:
        result["skipped"].append("no_run_log_path")
        return result
    result["log_path"] = str(resolved)

    try:
        payload = read_run_log_sections(resolved)
    except Exception as exc:  # noqa: BLE001 — finalization must never mask execution
        result["failures"].append(str(exc))
        return result

    records = payload["records"]
    result["log_path"] = str(payload["path"])

    if not _has_record_type(records, _RUN_COMPLETE):
        result["skipped"].append("no_terminal_record")
        return result

    existing = [r for r in records if _is_record_type(r, _RESULTS_SUMMARY)]
    if existing and not replace_existing:
        # Default idempotency: an existing terminal summary is reused, never duplicated.
        result["summary"] = existing[-1]
        result["action"] = "existing"
        return result

    try:
        # Build from evidence with any prior RESULTS_SUMMARY excluded, so a stale
        # summary never feeds the newly built one.
        source_records = [r for r in records if not _is_record_type(r, _RESULTS_SUMMARY)]
        sections = _run_log_sections(source_records)
        identity = _run_log_identity(Path(payload["path"]), source_records, sections)
        summary = build_results_summary(
            sections, identity, source_records,
            _normalize_execution_details(execution_details),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        if existing:
            # replace_existing: rewrite the log without prior RESULTS_SUMMARY records
            # (every other record unchanged and in order), then the new summary last.
            _rewrite_run_log(payload["path"], source_records + [summary])
            result["action"] = "replaced"
        else:
            # Append the completed RESULTS_SUMMARY as the final JSONL record. The builder
            # returns a complete, self-describing record, appended verbatim through the
            # same primitive the run-log writer uses — no separate results file.
            primitive_file_io.append_jsonl(payload["path"], summary)
            result["action"] = "created"
    except Exception as exc:  # noqa: BLE001 — preserve the log; report; never raise
        result["failures"].append(str(exc))
        return result

    result["summary"] = summary
    return result


def _rewrite_run_log(path: str, records: list[dict[str, Any]]) -> None:
    """Atomically rewrite the run log as ``records`` (one JSON object per line)."""
    from rey_lib.files import primitive_file_io

    text = "".join(
        json.dumps(record, ensure_ascii=False, default=str) + "\n" for record in records
    )
    primitive_file_io.atomic_write_text(path, text)


def _is_record_type(record: dict[str, Any], record_type: str) -> bool:
    """Return True when ``record`` matches ``record_type`` (case-insensitive)."""
    return str(record.get("record_type") or "").upper() == record_type.upper()


def _has_record_type(records: list[dict[str, Any]], record_type: str) -> bool:
    """Return True when any record matches ``record_type`` (case-insensitive)."""
    return any(_is_record_type(r, record_type) for r in records)


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
