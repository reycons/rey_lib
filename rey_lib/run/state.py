"""
What a run currently is, derived from its own evidence.

Deriving progress, the current step, metrics and status is the same work whatever
kind of execution produced the run, so it lives here once and every reporting
surface composes it. This module is deliberately identity-free and stateless: a
caller passes the run, its projected log sections and an ordered plan, and
assembles its own answer around these derivations.

These are plain functions, not a base class. Reporting surfaces share behaviour,
not an inheritance hierarchy.

What is *not* here, and must not arrive: anything that decides how a run is
shown. Which viewer draws a log stream, what a payload looks like on a screen and
where it is placed are presentation, owned by the surface that presents. This
module reports a log as a reference -- a path and nothing about how to display it
-- and stops there.

Moved from the Console's runner_state, which had grown to hold both run state and
Console presentation. The split is on ownership rather than on the old file
boundary.
"""

from __future__ import annotations

from typing import Any

from rey_lib.formatting import duration_label
from rey_lib.logs import read_run_log_sections

__all__ = [
    "SUPPORTED_STATES",
    "current_step",
    "execution_records",
    "execution_state",
    "log_reference",
    "metrics",
    "plan_app",
    "plan_index",
    "progress",
    "run_sections",
    "scoped_plan",
    "status_of",
]

# The states a run may report; anything else collapses to "unknown".
SUPPORTED_STATES = {"running", "paused", "completed", "failed", "aborted", "unknown"}


def run_sections(run_log_path: str | None) -> dict[str, Any]:
    """Return run-log section projections for a path, or empty when absent.

    Parameters
    ----------
    run_log_path : str | None
        The resolved run log path. Resolving it is the caller's job: linking an
        active run to its log is run-kind specific.

    Returns
    -------
    dict[str, Any]
        The helper-projected sections, or an empty mapping.
    """
    if not run_log_path:
        return {}
    return read_run_log_sections(run_log_path).get("sections", {})


def execution_records(sections: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the helper-projected execution records.

    Parameters
    ----------
    sections : dict[str, Any]
        Projected run-log sections.

    Returns
    -------
    list[dict[str, Any]]
        The execution records, or an empty list.
    """
    return list(sections.get("execution", {}).get("records", []))


def plan_app(plan: list[dict[str, Any]], name: str) -> str:
    """Return the plan app for a step name, or blank when unknown.

    Parameters
    ----------
    plan : list[dict[str, Any]]
        The ordered execution plan.
    name : str
        The step name to resolve.

    Returns
    -------
    str
        The owning app, or an empty string.
    """
    for step in plan:
        if str(step.get("name") or "") == name:
            return str(step.get("app") or "")
    return ""


def plan_index(plan: list[dict[str, Any]], name: str) -> int | None:
    """Return the 1-based plan position of a step name, or None when unknown.

    Parameters
    ----------
    plan : list[dict[str, Any]]
        The ordered execution plan.
    name : str
        The step name to resolve.

    Returns
    -------
    int | None
        The 1-based position, or None.
    """
    for step in plan:
        if str(step.get("name") or "") == name:
            return int(step.get("index") or 0) or None
    return None


def scoped_plan(
    plan: list[dict[str, Any]],
    execution_mode: str | None,
    step_id: str | None,
) -> list[dict[str, Any]]:
    """Narrow an ordered plan to the steps one execution will actually run.

    Progress is measured against what a run does, not against what the pipeline
    or workflow contains. Running to the fourth step of seven is four steps of
    work, and reporting seven overstates the total for the whole run and never
    reaches completion.

    Both runners derived their total from the full definition regardless of
    scope, so both reported the same wrong total. The narrowing is identical for
    each, so it is one function rather than two.

    An unrecognised mode, an absent step, or a step that is not in the plan all
    return the plan unchanged: narrowing on a guess would replace an overstated
    total with a wrong one.

    Parameters
    ----------
    plan : list[dict[str, Any]]
        The ordered execution plan.
    execution_mode : str | None
        One of full, step, from_step, to_step. None is treated as full.
    step_id : str | None
        The step the mode is relative to.

    Returns
    -------
    list[dict[str, Any]]
        The steps this execution will run, in order.
    """
    mode = str(execution_mode or "full").strip()
    step = str(step_id or "").strip()
    if mode == "full" or not step or not plan:
        return list(plan)
    position = next(
        (i for i, entry in enumerate(plan) if str(entry.get("name") or "") == step),
        None,
    )
    if position is None:
        return list(plan)
    if mode == "step":
        return [plan[position]]
    if mode == "from_step":
        return list(plan[position:])
    if mode == "to_step":
        return list(plan[: position + 1])
    return list(plan)


def progress(sections: dict[str, Any], plan: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive completed/total/percent using the plan for the total.

    Total comes from the ordered execution plan (backend truth), so it is known
    immediately -- even before the first step logs a STEP_START. When no plan is
    available the projected STEP_START count is used as a fallback. Completed is
    the projected STEP_END count, clamped to the plan length.

    Parameters
    ----------
    sections : dict[str, Any]
        Projected run-log sections.
    plan : list[dict[str, Any]]
        The ordered execution plan.

    Returns
    -------
    dict[str, Any]
        The progress block.
    """
    records = execution_records(sections)
    starts = [r for r in records if str(r.get("record_type")) == "STEP_START"]
    ends = [r for r in records if str(r.get("record_type")) == "STEP_END"]
    total = len(plan) if plan else len(starts)
    completed = min(len(ends), total) if total else len(ends)
    # A run advances the moment it DISPATCHES a step (writes STEP_START), not only
    # when a step finishes -- so the current position tracks dispatch and the bar
    # moves as soon as the next step begins.
    current = min(len(starts), total) if total else len(starts)
    remaining = max(total - completed, 0) if total else 0
    percent = round(100 * completed / total) if total else None
    return {
        "completed_steps": completed,
        "total_steps": total,
        "remaining_steps": remaining,
        "current_step_number": current,
        "percent": percent,
    }


def current_step(sections: dict[str, Any], plan: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the latest started step, resolved against the ordered plan.

    The log identifies which named step is active; the plan (backend truth)
    supplies that step's app and 1-based position. No frontend inference and no
    step count derived from the log.

    Parameters
    ----------
    sections : dict[str, Any]
        Projected run-log sections.
    plan : list[dict[str, Any]]
        The ordered execution plan.

    Returns
    -------
    dict[str, Any]
        The current-step block; empty fields when no step has started.
    """
    empty = {"name": "", "app": "", "status": "", "started_at": "",
             "elapsed": "", "index": None}
    records = execution_records(sections)
    starts = [r for r in records if str(r.get("record_type")) == "STEP_START"]
    if not starts:
        return empty
    last = starts[-1]
    name = str(last.get("step_name") or "")
    started = str(last.get("timestamp") or "")
    ends = [r for r in records if str(r.get("record_type")) == "STEP_END"]
    end = next((e for e in reversed(ends) if str(e.get("step_name")) == name), None)
    ended_after = end is not None and len(ends) >= len(starts)
    if ended_after:
        step_status = str(end.get("status") or "completed")
        elapsed = duration_label(started, str(end.get("timestamp") or ""))
    else:
        step_status = "running"
        elapsed = duration_label(started, "")
    return {"name": name, "app": plan_app(plan, name), "status": step_status,
            "started_at": started, "elapsed": elapsed,
            "index": plan_index(plan, name)}


def metrics(
    sections: dict[str, Any], summary: dict[str, Any], duration: str
) -> dict[str, Any]:
    """Compose run metrics from projected records, falling back to the run summary.

    Parameters
    ----------
    sections : dict[str, Any]
        Projected run-log sections.
    summary : dict[str, Any]
        The run's execution summary, used when no records are projected.
    duration : str
        The run duration label.

    Returns
    -------
    dict[str, Any]
        The metrics block.
    """
    records = execution_records(sections)
    if records:
        warnings = sum(1 for r in records if str(r.get("record_type")) == "WARNING")
        errors = sum(1 for r in records if str(r.get("record_type")) == "ERROR")
        artifacts = int(sections.get("files", {}).get("artifacts", {}).get("count", 0) or 0)
    else:
        warnings = int(summary.get("warnings") or 0)
        errors = int(summary.get("errors") or 0)
        artifacts = 0
    return {"artifacts": artifacts, "warnings": warnings,
            "errors": errors, "duration": duration}


def execution_state(run: dict[str, Any], state: str) -> dict[str, Any]:
    """Return the execution-state block for a run.

    Parameters
    ----------
    run : dict[str, Any]
        The run record from the run-console projection.
    state : str
        The already-normalized run state.

    Returns
    -------
    dict[str, Any]
        The execution-state block.
    """
    return {
        "status": state,
        "label": state.upper(),
        "message": str(run.get("status_message") or ""),
        "started_at": str(run.get("started_at") or "") or None,
        "ended_at": str(run.get("ended_at") or "") or None,
        "elapsed": str(run.get("duration") or ""),
    }


def log_reference(run_log_path: str | None) -> dict[str, Any]:
    """Return where the run log is, and nothing about how to show it.

    A run does not classify its own log. It reports the path; whoever presents it
    asks the viewer selector what that file is and routes the answer. Deciding
    here produced a decision nobody could complete -- no records, no navigator --
    because only the selector and the router know what a JSONL file is for.

    Named for what it is rather than for the surface that reads it: this is the
    log reference a run carries, and it is what a canonical run response projects
    as its log refs.

    Parameters
    ----------
    run_log_path : str | None
        The resolved run log path, or None when not yet resolved.

    Returns
    -------
    dict[str, Any]
        ``{"path": <path>}``, or an empty mapping when there is no log yet.
    """
    if not run_log_path:
        return {}
    return {"path": str(run_log_path)}


def status_of(raw: str) -> str:
    """Map an underlying run status to a supported run state.

    Parameters
    ----------
    raw : str
        The underlying run status.

    Returns
    -------
    str
        A member of ``SUPPORTED_STATES``.
    """
    value = raw.lower()
    if value == "unavailable":
        return "unknown"
    return value if value in SUPPORTED_STATES else "unknown"
