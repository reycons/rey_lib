"""Post-execution run-results finalization.

``create_results_summary`` is the single explicit utility that builds the canonical
RESULTS_SUMMARY from a completed JSONL run log and appends it as the final record of
that same log. It is called explicitly by the top-level application that owns the run
(a standalone app, or pipeline_coordinator / the workflow coordinator) after its final
RUN_COMPLETE — never automatically in generic app-operation finalization, so nested
pipeline sub-apps that share the owner's log do not create an early, incorrect summary.

The execution layer contributes only execution-specific facts (``execution_details``);
this module owns building and the one-time durable append
(SGC_Rey_Lib_Log_Summary_Framework_And_Run_Summary,
 SGC_Rey_Lib_Results_Summary_Diagnostic_Package_Correction,
 SGC_Rey_Lib_Write_Results_Summary_To_Run_Log,
 SGC_Rey_Lib_Explicit_Results_Summary_Creation).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rey_lib.logs.results_summary import build_results_summary

# When a run exceeds this many steps, the per-step list in execution_details is
# dropped (the authoritative per-step detail stays in the log's STEP records) and a
# steps_truncated flag is set — a bound that never truncates diagnostic error output.
_MAX_EMBEDDED_STEPS = 250

_RUN_COMPLETE = "RUN_COMPLETE"
_RESULTS_SUMMARY = "RESULTS_SUMMARY"


def finalize_run_log(log_path: str | Path) -> dict[str, Any]:
    """Run the canonical post-run log processing sequence.

    Order: RESULTS_SUMMARY, then the log_interpreter stage
    (RESULTS_SUMMARY -> LLM_PACKAGE -> LLM_INTERPRETATION). The analysis honors its
    own ``fail_on_error`` setting.

    One LLM call produces one authoritative result record. LLM_INTERPRETATION
    carries the structured interpretation together with the rendered subject, html,
    and text, because the configured contract renders them in the same response —
    so no second email-generation stage exists to duplicate that work.
    """
    from rey_lib.logs.llm_package import create_llm_package, run_configured_log_analysis

    result = create_results_summary(log_path=log_path)
    if result.get("summary") is None:
        return {**result, "package": None, "analysis": None}
    try:
        package = create_llm_package(
            log_path,
            analysis_name="log_interpreter",
            source_record_type="RESULTS_SUMMARY",
            package_record_type="LLM_PACKAGE",
        )
    except Exception as exc:  # noqa: BLE001 — post-run processing must preserve the run
        return {**result, "package": None, "package_failures": [str(exc)],
                "analysis": None}
    analysis = run_configured_log_analysis(
        log_path, analysis_name="log_interpreter", package_record_type="LLM_PACKAGE",
    )

    return {**result, "package": package, "package_failures": [], "analysis": analysis}


def create_results_summary(
    ctx: Any = None,
    *,
    log_path: str | Path | None = None,
    execution_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the canonical RESULTS_SUMMARY for a completed run log.

    The explicit finalization utility. The top-level run-owning application calls it
    after its final RUN_COMPLETE.
    It reads the JSONL run log, builds the deterministic summary with
    ``build_results_summary``, and appends the immutable RESULTS_SUMMARY through the
    standard hierarchy-stamped writer. Diagnostic error output is preserved verbatim
    (never sanitized or truncated here).

    Parameters
    ----------
    ctx : Any
        The run context (exposes ``run_log_path``); used when ``log_path`` is omitted.
    log_path : str | Path | None
        An explicit completed run-log path.
    execution_details : dict[str, Any] | None
        Execution-specific facts from the execution layer (namespaced by ``kind``);
        used to enrich step_results. ``None`` for plain apps.
    Returns
    -------
    dict[str, Any]
        ``summary`` (the RESULTS_SUMMARY object or None), ``action``
        (``"created"`` / None), ``log_path``,
        ``skipped``, ``failures``.
    """
    from rey_lib.logs.evidence_projection import (
        _run_log_identity,
        _run_log_sections,
        read_run_log_sections,
    )
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

    try:
        source_records = records
        sections = _run_log_sections(source_records)
        identity = _run_log_identity(Path(payload["path"]), source_records, sections)
        summary = build_results_summary(
            sections, identity, source_records,
            _normalize_execution_details(execution_details),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        from rey_lib.logs.record_enrichment import log_run_record
        from rey_lib.logs.run_state import companion_path

        state_path = companion_path(str(payload["path"]))
        if not state_path.is_file():
            result["failures"].append(
                f"Authoritative hierarchy state not found: {state_path}"
            )
            return result

        # Limit the writer context to fields already present in the canonical Summary.
        # Optional app/workflow enrichment would otherwise alter the builder payload.
        # The shared hierarchy writer resolves state solely from run_log_path.
        write_ctx = SimpleNamespace(
            run_log_path=str(payload["path"]),
            run_id=str(summary["run_id"]),
            run_timestamp=str(summary["run_timestamp"]),
        )
        records_before = read_run_log_sections(payload["path"])["records"]
        summary_fields = dict(summary)
        summary_fields.pop("record_type", None)
        log_run_record(write_ctx, _RESULTS_SUMMARY, **summary_fields)
        records_after = read_run_log_sections(payload["path"])["records"]
        appended = records_after[len(records_before):]
        if len(appended) != 1 or not _is_record_type(appended[0], _RESULTS_SUMMARY):
            result["failures"].append(
                "Standard run-log writer did not append RESULTS_SUMMARY"
            )
            return result
        summary = appended[0]
        result["action"] = "created"
    except Exception as exc:  # noqa: BLE001 — preserve the log; report; never raise
        result["failures"].append(str(exc))
        return result

    result["summary"] = summary
    return result


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
