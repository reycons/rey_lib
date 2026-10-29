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

#: The governed AI task that interprets a run log. No stage runs it since the
#: legacy AI was removed (#1025); running it on the live AI is #1284.
#:
#: **Permanent.** It is half of a standing invariant; the other half is the
#: ``log_interpretation`` task declared in AI settings, which answers it with a
#: profile and an instruction. Renaming or deleting either breaks the pairing
#: that gives LLM_PACKAGE -> LLM_INTERPRETATION a stable semantic owner, and
#: anything that interprets a run log resolves through this name rather than
#: inventing a second log-analysis semantic beside it.
LOG_INTERPRETATION_TASK = "log_interpretation"


def finalize_run_log(run_log: Any) -> dict[str, Any]:
    """Run the canonical post-run log processing sequence.

    Order: RESULTS_SUMMARY, then the package the log interpreter reads
    (RESULTS_SUMMARY -> LLM_PACKAGE). Neither fails the run: post-run processing
    runs after the run it describes has finished.

    The log interpretation that followed (LLM_PACKAGE -> LLM_INTERPRETATION) was
    removed with the legacy AI (#1025). No interpretation is attempted, so no
    interpretation or failure record is written and ``analysis`` is always
    ``None``. Running it on the live AI is #1284.
    """
    from rey_lib.logs.llm_package import create_llm_package

    run_log.enter_phase("summarize")
    result = create_results_summary(run_log)
    if result.get("summary") is None:
        return {**result, "package": None, "analysis": None}
    run_log.enter_phase("package")
    try:
        package = create_llm_package(
            run_log,
            source_record_type="RESULTS_SUMMARY",
            package_record_type="LLM_PACKAGE",
        )
    except Exception as exc:  # noqa: BLE001 — post-run processing must preserve the run
        return {**result, "package": None, "package_failures": [str(exc)],
                "analysis": None}
    return {**result, "package": package, "package_failures": [], "analysis": None}


def create_results_summary(
    run_log: Any,
    *,
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

    # None when the run writes no JSONL file: nothing to summarize from.
    path = run_log.path()
    resolved = str(path) if path is not None else None
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

    # A completed run owns exactly one RESULTS_SUMMARY. Finalization nests — the
    # shared coordinator finalizes a standalone workflow run and the run-owning
    # application finalizes again in its own lifecycle — so repeated calls must
    # return the summary already on the log rather than append a second one.
    existing = _last_record_of_type(records, _RESULTS_SUMMARY)
    if existing is not None:
        result["summary"] = existing
        result["skipped"].append("already_summarized")
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

        # There is no hierarchy state file to check for: the run log owns its
        # nesting in memory and control.run_log owns it durably.
        records_before = read_run_log_sections(payload["path"])["records"]
        summary_fields = dict(summary)
        summary_fields.pop("record_type", None)
        log_run_record(run_log, _RESULTS_SUMMARY, **summary_fields)
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


def _last_record_of_type(
    records: list[dict[str, Any]],
    record_type: str,
) -> dict[str, Any] | None:
    """Return the most recent record of ``record_type``, or None when absent."""
    for record in reversed(records or []):
        if _is_record_type(record, record_type):
            return record
    return None


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
