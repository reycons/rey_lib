"""Post-execution log-summary framework.

The single shared entry point that finalizes a completed run log after its terminal
record is durably written. It reads the completed JSONL log, runs the ordered summary
builders (RUN_SUMMARY first; future builders register in ``_SUMMARY_BUILDERS``), and
appends any missing summary records to the same log — deterministic, idempotent, and
safe to call more than once. The execution layer contributes only execution-specific
facts (``execution_details``); this framework owns the canonical record, validation,
sanitization, deterministic ordering, and idempotency
(SGC_Rey_Lib_Log_Summary_Framework_And_Run_Summary).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rey_lib.logs.execution_records import log_run_summary
from rey_lib.logs.record_enrichment import sanitize_log_value
from rey_lib.logs.run_summary import build_common_run_summary

# When a run exceeds this many steps, the per-step list is dropped from
# execution_details (the authoritative per-step detail stays in the log's STEP
# records) and a steps_truncated flag is set.
_MAX_EMBEDDED_STEPS = 250

_RUN_COMPLETE = "RUN_COMPLETE"
_RUN_SUMMARY = "RUN_SUMMARY"


def finalize_run_log(ctx: Any, execution_details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Finalize a completed run log by appending canonical summary records.

    Invoked once by each terminal execution path (app / workflow / pipeline) after
    the terminal RUN_COMPLETE record is durably written. Reads the completed log,
    invokes the ordered summary builders, and appends missing summary records to the
    same JSONL log. Idempotent: a section already present is left untouched. A builder
    failure is reported and never corrupts the existing log or blocks other builders.

    Parameters
    ----------
    ctx : Any
        The run context (exposes ``run_log_path`` and is the append target).
    execution_details : dict[str, Any] | None
        Execution-specific facts contributed by the execution layer (namespaced by
        ``kind``). ``None`` for plain apps.

    Returns
    -------
    dict[str, Any]
        A structured result: ``log_changed``, ``builders_invoked``,
        ``sections_generated``, ``sections_already_present``, ``records_appended``,
        ``sections_skipped``, ``failures``.
    """
    # Deferred import: evidence_projection imports from the logs package; importing it
    # at module top would create an import cycle for callers that load summary early.
    from rey_lib.logs.evidence_projection import _run_log_identity, read_run_log_sections

    result: dict[str, Any] = {
        "log_changed": False,
        "builders_invoked": [],
        "sections_generated": [],
        "sections_already_present": [],
        "records_appended": 0,
        "sections_skipped": [],
        "failures": [],
    }

    path = getattr(ctx, "run_log_path", None)
    if not path:
        result["sections_skipped"].append({"section": "*", "reason": "no_run_log_path"})
        return result
    try:
        payload = read_run_log_sections(path)
    except Exception as exc:  # noqa: BLE001 — finalization must never mask execution
        result["failures"].append({"section": "*", "error": str(exc)})
        return result

    records = payload["records"]
    sections = payload["sections"]

    # Only a completed run is finalized; an active/incomplete log has no terminal record.
    if not _has_record_type(records, _RUN_COMPLETE):
        result["sections_skipped"].append({"section": "*", "reason": "no_terminal_record"})
        return result

    identity = _run_log_identity(Path(payload["path"]), records, sections)

    for section_type, builder in _SUMMARY_BUILDERS:
        result["builders_invoked"].append(section_type)
        if _has_record_type(records, section_type):
            result["sections_already_present"].append(section_type)
            continue
        try:
            builder(
                ctx,
                sections=sections,
                identity=identity,
                records=records,
                execution_details=execution_details,
            )
        except Exception as exc:  # noqa: BLE001 — preserve the log, report, continue
            result["failures"].append({"section": section_type, "error": str(exc)})
            continue
        result["sections_generated"].append(section_type)
        result["records_appended"] += 1
        result["log_changed"] = True

    return result


def _has_record_type(records: list[dict[str, Any]], record_type: str) -> bool:
    """Return True when any record matches ``record_type`` (case-insensitive)."""
    target = record_type.upper()
    return any(str(r.get("record_type") or "").upper() == target for r in records)


def _build_run_summary(
    ctx: Any,
    *,
    sections: dict[str, Any],
    identity: dict[str, Any],
    records: list[dict[str, Any]],
    execution_details: dict[str, Any] | None,
) -> None:
    """Assemble and append the canonical RUN_SUMMARY (common + execution_details)."""
    summary = dict(build_common_run_summary(sections, identity, records))
    summary["execution_details"] = _normalize_execution_details(execution_details)
    log_run_summary(ctx, sanitize_log_value(summary))


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


# Ordered summary builders. RUN_SUMMARY is the first and only implemented builder;
# future builders (email_summary, llm_package, llm_result) register here without any
# change to the terminal-lifecycle integration point.
_SUMMARY_BUILDERS: list[tuple[str, Any]] = [(_RUN_SUMMARY, _build_run_summary)]
