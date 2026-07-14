"""Restore pipeline inputs from one append-only execution log.

This module is the single owner of pipeline-reset planning and execution.  UI
callers supply a selected run log (or ask for the latest pipeline run); they do
not interpret file history or perform file operations themselves.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rey_lib.files.file_utils import move_file, run_artifact_path
from rey_lib.logs import (
    bind_run,
    clear_run,
    discover_runs,
    log_run_complete,
    log_run_record,
    log_run_start,
    log_run_summary,
    read_run_log_sections,
    resolve_run_identity,
)


def latest_pipeline_run(log_root: Path | str, pipeline_name: str) -> str | None:
    """Return the newest execution log explicitly owned by ``pipeline_name``."""
    wanted = str(pipeline_name or "")
    for run in discover_runs(log_root, limit=0):
        if str(run.get("pipeline") or "") == wanted:
            path = str(run.get("run_log_path") or "")
            if path:
                return path
    return None


def preview_pipeline_reset_from_run(run_log: Path | str) -> dict[str, Any]:
    """Return an evidence-based reset plan without changing the filesystem."""
    source_log, records = _source_records(run_log)
    run_identity = _run_identity(records)
    inputs, not_restorable = _restore_candidates(records)
    moves: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = list(not_restorable)

    for item in inputs:
        source = Path(item["source_path"])
        destination = Path(item["destination_path"])
        base = {
            "file": destination.name,
            "source_path": str(source),
            "dest_path": str(destination),
            "destination_path": str(destination),
        }
        if source == destination:
            skipped.append({**base, "reason": "input already at original location"})
        elif destination.exists():
            skipped.append({**base, "reason": "destination conflict"})
        elif not source.exists() or not source.is_file():
            skipped.append({**base, "reason": "recoverable input source is missing"})
        else:
            moves.append({**base, "would_overwrite": False})

    return {
        "run_id": run_identity["run_id"],
        "pipeline_name": run_identity["pipeline_name"],
        "source_run_log": str(source_log),
        "moves": moves,
        "move_count": len(moves),
        "overwrite_count": 0,
        "skipped": skipped,
        "skipped_count": len(skipped),
        # Compatibility fields for the existing Console response shape. Pipeline
        # reset no longer deletes generated output artifacts.
        "deletes": [],
        "delete_count": 0,
    }


def reset_pipeline_from_run(
    run_log: Path | str,
    *,
    reason: str = "",
    audit_log_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Restore recoverable inputs from ``run_log`` and write a reset run log.

    Each input is handled independently. Missing sources and destination
    conflicts are skipped; an I/O failure is recorded and remaining inputs
    continue. Generated outputs are never considered or removed.
    """
    plan = preview_pipeline_reset_from_run(run_log)
    audit_ctx = _audit_context(plan, audit_log_dir)
    log_run_start(
        audit_ctx,
        operation="pipeline_reset",
        source_run_id=plan["run_id"],
        source_run_log=plan["source_run_log"],
        source_pipeline_name=plan["pipeline_name"],
        reason=str(reason or ""),
    )

    restored: list[dict[str, str]] = []
    skipped = list(plan["skipped"])
    failed: list[dict[str, str]] = []
    bind_run(audit_ctx)
    try:
        for item in plan["moves"]:
            source = Path(item["source_path"])
            destination = Path(item["destination_path"])
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                moved = move_file(source, destination.parent, dest_name=destination.name)
                result = {
                    "file": destination.name,
                    "from": str(source),
                    "to": str(moved),
                }
                restored.append(result)
                log_run_record(
                    audit_ctx, "PIPELINE_RESET_FILE", status="restored", **result,
                )
            except OSError as exc:
                result = {
                    "file": destination.name,
                    "source_path": str(source),
                    "destination_path": str(destination),
                    "error": str(exc),
                }
                failed.append(result)
                log_run_record(
                    audit_ctx, "PIPELINE_RESET_FILE", status="failed", **result,
                )
    finally:
        clear_run()

    summary = {
        "operation": "pipeline_reset",
        "source_run_id": plan["run_id"],
        "source_run_log": plan["source_run_log"],
        "source_pipeline_name": plan["pipeline_name"],
        "restored_count": len(restored),
        "skipped_count": len(skipped),
        "failed_count": len(failed),
    }
    log_run_summary(audit_ctx, summary)
    log_run_complete(
        audit_ctx,
        "partial" if failed else "success",
        **summary,
    )
    return {
        **summary,
        "pipeline_name": plan["pipeline_name"],
        "reason": str(reason or ""),
        "audit_log_path": str(audit_ctx.run_log_path),
        "restored": restored,
        "skipped": skipped,
        "failed": failed,
        # Existing Console clients use these names. They map directly to the new
        # structured reset result while migration remains in progress.
        "moved": restored,
        "moved_count": len(restored),
        "errors": failed,
        "error_count": len(failed),
        "deleted": [],
        "deleted_count": 0,
    }


def _source_records(run_log: Path | str) -> tuple[Path, list[dict[str, Any]]]:
    path = Path(run_log).expanduser().resolve()
    payload = read_run_log_sections(path)
    records = list(payload.get("records") or [])
    if not path.is_file() or not records:
        raise ValueError(f"Execution log is missing or empty: {path}")
    return path, records


def _run_identity(records: list[dict[str, Any]]) -> dict[str, str]:
    first = records[0] if records else {}
    return {
        "run_id": str(first.get("run_id") or ""),
        "pipeline_name": str(first.get("pipeline_name") or first.get("pipeline") or ""),
    }


def _restore_candidates(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Select restore candidates from this run's move FILE_OPERATION records,
    constrained by the single PIPELINE_RESTORE_POLICY.

    INPUT_FILE_REFERENCE and ARTIFACT_MANIFEST remain valid provenance/display
    records but are not authoritative for restore planning. A file is a candidate
    only when its latest tracked location (reconstructed from this run's move
    records) lies beneath a restore rule's ``from`` folder; it is then restored to
    that rule's ``to`` folder under the same basename. Files whose latest location
    matches no rule are reported, not restored.
    """
    rules = _restore_rules(records)
    latest_locations = _movement_chains(records)

    recoverable: list[dict[str, str]] = []
    not_restorable: list[dict[str, str]] = []
    for origin in sorted(latest_locations):
        location = latest_locations[origin]
        name = Path(location).name
        rule = _match_restore_rule(location, rules)
        if rule is None:
            not_restorable.append({
                "file": name,
                "reason": "not restorable: no matching restore rule",
            })
            continue
        recoverable.append({
            "source_path": location,
            "destination_path": str(Path(rule["to"]) / name),
        })
    return recoverable, not_restorable


def _restore_rules(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return resolved from/to rules from the single PIPELINE_RESTORE_POLICY record."""
    for record in records:
        if str(record.get("record_type") or "").upper() == "PIPELINE_RESTORE_POLICY":
            rules: list[dict[str, str]] = []
            for rule in record.get("restore_rules") or []:
                if not isinstance(rule, dict):
                    continue
                source = _canonical(rule.get("from"))
                target = _canonical(rule.get("to"))
                if source and target:
                    rules.append({"from": source, "to": target})
            return rules
    return []


def _movement_chains(records: list[dict[str, Any]]) -> dict[str, str]:
    """Reconstruct each tracked file's latest absolute location from this run's move
    FILE_OPERATION records, in chronological (append) order.

    Consumes the production file_utils move schema unchanged (``operation``/``action``,
    ``source_abs``, ``destination_abs``, ``original_source_abs``). Moves are linked by
    location continuity, so a multi-hop chain resolves to one latest location per
    origin file.
    """
    latest: dict[str, str] = {}
    location_origin: dict[str, str] = {}
    for record in records:
        if str(record.get("record_type") or "").upper() != "FILE_OPERATION":
            continue
        operation = str(record.get("operation") or record.get("action") or "").lower()
        if operation != "move":
            continue
        source = _canonical(record.get("source_abs"))
        destination = _canonical(record.get("destination_abs"))
        if not source or not destination:
            continue
        origin = (location_origin.pop(source, None)
                  or _canonical(record.get("original_source_abs"))
                  or source)
        latest[origin] = destination
        location_origin[destination] = origin
    return latest


def _match_restore_rule(
    location: str, rules: list[dict[str, str]]
) -> dict[str, str] | None:
    """Return the first rule whose ``from`` folder contains ``location`` (recursive)."""
    target = Path(location)
    for rule in rules:
        try:
            target.relative_to(Path(rule["from"]))
        except ValueError:
            continue
        return rule
    return None


def _canonical(value: Any) -> str:
    text = str(value or "").strip()
    return str(Path(text).expanduser().resolve()) if text else ""


def _audit_context(plan: dict[str, Any], audit_log_dir: Path | str | None) -> SimpleNamespace:
    directory = Path(audit_log_dir or Path(plan["source_run_log"]).parent).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    ctx = SimpleNamespace(
        app_name="pipeline_reset",
    )
    resolve_run_identity(ctx)
    ctx.run_log_path = str(
        run_artifact_path(directory, "pipeline_reset", ctx.run_timestamp, "jsonl")
    )
    return ctx
