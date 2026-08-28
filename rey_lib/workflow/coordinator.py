"""Shared workflow coordinator — stacks an app's internal function calls.

A workflow is a named, ordered series of internal function calls owned by one
app (SGC_rey_workflow_internal_function_call_model). This coordinator is the
shared mechanic every Rey app uses: it reads a resolved workflow
(``tokens`` / ``processes`` / ``steps``), resolves workflow-local tokens, builds
effective per-step config (process defaults deep-merged with the step's
override), and dispatches each step to the app-provided process-handler
registry.

It owns no app domain logic. It calls only handlers the app registered by
process name (never arbitrary Python from YAML), dispatches by ``process`` (never
from human ``label``), and performs no cross-app orchestration — that is
pipeline territory.

Public API
----------
StepOutcome     One step's recorded outcome (id, label, process, status, ...).
WorkflowRun     Ordered step outcomes plus overall status.
run_workflow    Stack a workflow's step function calls per its YAML config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from rey_lib.config.config_utils import record_config_file_references
from rey_lib.errors.error_utils import build_safe_error_payload
from rey_lib.logs import (
    bind_step,
    bind_run,
    clear_step,
    clear_run,
    get_logger,
    finalize_run_log,
    log_run_complete,
    log_run_start,
    log_error,
    log_step_failure,
    log_step_end,
    log_step_start,
)
from rey_lib.workflow.engine import RunContext, WorkflowError

__all__ = ["StepOutcome", "WorkflowRun", "run_workflow", "ProcessHandler"]

_logger = get_logger(__name__)

# Workflow configuration this engine no longer honours. Declaring one is a hard
# error, never a silently ignored key (SGC_Log_Run_Rollback).
_RETIRED_WORKFLOW_KEYS = ("restore_mappings",)

# A process handler: (ctx, run_log, effective_config, run_context) -> result-or-None.
# The result may be any object exposing ``status``/``detail``; None means "ok".
#
# The run log is passed rather than reached through ctx or the run context: a
# handler that writes records is a run-log writer, and a writer takes its owner.
ProcessHandler = Callable[[Any, Any, dict[str, Any], RunContext], Any]


@dataclass
class StepOutcome:
    """One workflow step's recorded outcome."""

    id: str
    label: str
    process: str
    status: str                       # "ok" | "skipped" | "failed"
    detail: str = ""
    artifacts: list[Any] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class WorkflowRun:
    """The result of coordinating a workflow's steps."""

    name: str
    status: str                       # "success" | "failed"
    outcomes: list[StepOutcome] = field(default_factory=list)
    context: Optional[RunContext] = None  # final run context (metadata + data)


def _finalize_run(ctx: Any, run_log: Any) -> None:
    """Finalize the run log unless a parent pipeline owns finalization.

    A pipeline step's ctx carries pipeline run identity in ``ctx.runtime`` (stamped
    by pipeline_coordinator); when present, the pipeline owns the single run-level
    finalization, so this embedded workflow must not finalize the shared pipeline
    run. A standalone workflow run has no pipeline identity and finalizes as before.
    """
    if _get(getattr(ctx, "runtime", None), "pipeline_run_id"):
        return
    # Whether there is a durable log to finalize is the run log's own fact; the
    # context used to answer it, which is the ownership this migration removed.
    try:
        run_log.path()
    except Exception:  # noqa: BLE001 — nothing durable to finalize.
        return
    finalize_run_log(run_log)


def _refused_disabled_workflow(
    ctx: Any, run_log,
    name: str,
    *,
    apply: bool,
    metadata: Optional[dict[str, Any]],
) -> "WorkflowRun":
    """Record a disabled workflow as a refused run and return it.

    The attempt is still evidence worth keeping — someone asked for this
    workflow and it did not run — so it goes through the normal run result
    path: a RUN_START, a terminal RUN_COMPLETE, and a finalized run. No step
    is resolved and no handler is consulted.
    """
    message = (
        f"workflow '{name}' is disabled and was not executed. Set "
        f"'enabled: true' on the workflow definition to run it."
    )
    run = WorkflowRun(
        name=name,
        status="refused",
        context=RunContext(
            apply=apply, metadata=dict(metadata or {}), data={"ctx": ctx}
        ),
    )
    try:
        ctx.workflow_name = name
    except (AttributeError, TypeError):
        pass
    run_log.set_nest_level("workflow")
    log_run_start(run_log, workflow=name, apply=apply)
    bind_run(run_log)
    _logger.warning("%s", message)
    log_run_complete(run_log, "refused", message=message)
    _finalize_run(ctx, run_log)
    clear_run()
    return run


def _pre_execution_failure(
    ctx: Any, run_log,
    run: "WorkflowRun",
    *,
    step_id: str,
    label: str,
    process: str,
    sequence: int,
    message: str,
) -> "WorkflowError":
    """Record a resolution failure as normal run evidence, then hand it back.

    A workflow that fails between RUN_START and its first step would otherwise
    leave a run log with no outcome at all. The failure is written through the
    same records an in-step failure uses, the run is finalized and unbound, and
    the caller raises the returned error so the existing contract is unchanged.
    """
    failure_id = log_step_failure(run_log,
        failed_step_id=step_id,
        failed_step_name=label or step_id,
        message=message,
        error_type="WorkflowError",
        error_message=message,
        sanitized_exception=message,
        failed_step_sequence=sequence,
        process=process,
    )
    run.outcomes.append(
        StepOutcome(step_id, label, process, "failed", error=message)
    )
    run.status = "failed"
    _logger.error("%s", message)
    log_run_complete(run_log,
        "failed",
        message=message,
        failure_record_id=failure_id,
        failed_step_id=step_id,
        failed_step_name=label or step_id,
        failure_message=message,
    )
    _finalize_run(ctx, run_log)
    clear_run()
    return WorkflowError(message)


def run_workflow(
    ctx: Any, run_log,
    workflow: Any,
    registry: Mapping[str, ProcessHandler],
    *,
    apply: bool = True,
    only: Optional[str] = None,
    step: Optional[str] = None,
    from_step: Optional[str] = None,
    to_step: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> WorkflowRun:
    """Stack the workflow's step function calls per its YAML config.

    Optionally run a single step or an ordered inclusive range instead of the
    whole workflow. Selection resolves against the workflow's ordered step list
    and is deterministic and fail-closed (SGC_Rey_Lib_Shared_Workflow_Step_Execution).

    Parameters
    ----------
    ctx : Any
        Resolved application context (the handlers' domain input).
    workflow : Any
        Resolved workflow exposing ``name`` and optional ``tokens``,
        ``processes`` (name -> default config), and ``steps`` (each with
        ``id``, ``label``, ``process``, optional ``config``).
    registry : Mapping[str, ProcessHandler]
        App-owned ``process name -> handler``. YAML may only call these names.
    apply : bool
        False for dry-run; a step whose effective config sets ``apply_only``
        true is skipped. Dry-run is expressed by ``apply=False`` — this SGC adds
        no new dry-run semantics and applies the existing rules to the selected
        step set only.
    only : str | None
        Legacy single-step selector (by ``id``). Treated as ``step`` when set.
    step : str | None
        Run exactly the one step matching this identifier. Cannot be combined
        with ``from_step``/``to_step``.
    from_step : str | None
        Run from the matching step through the end of the workflow.
    to_step : str | None
        Run from the start of the workflow through the matching step. Combined
        with ``from_step`` this is the inclusive ordered range.
    metadata : dict | None
        Shared run metadata seeded into the run context.

    Step identifiers resolve against each step's ``id``, ``label``, or
    ``process``. An identifier matching no step, or more than one step, fails
    closed with a clear error. Only the selected/executed steps appear in the
    returned outcomes.

    Returns
    -------
    WorkflowRun
        Ordered step outcomes and overall status ("failed" stops the run).

    Raises
    ------
    WorkflowError
        On a malformed workflow (a step missing ``id``/``process``, an undefined
        process, or an unregistered handler) or an invalid/unresolvable step
        selection — always fail closed, never guess.
    """
    name = str(_get(workflow, "name", "") or "")
    # Being switched off is a governed outcome, not a fault: it is reported as
    # a run with a terminal status like any other result, so the caller exits
    # non-zero without a traceback. Absent means enabled; only an explicit
    # false refuses, so a definition that omits the key is unaffected. This is
    # decided before the definition is parsed, so a disabled workflow is never
    # rejected for some unrelated defect in steps it will not run.
    if _get(workflow, "enabled", True) is False:
        return _refused_disabled_workflow(ctx, run_log, name, apply=apply, metadata=metadata)
    tokens = _resolve_tokens(_get(workflow, "tokens"))
    processes = _to_mapping(_get(workflow, "processes"))
    steps = _as_list(_get(workflow, "steps"))

    # Extract and structurally validate every step's identity up front so step
    # selection can resolve deterministically against the ordered list.
    step_views: list[tuple[str, str, str]] = []
    for index, step_def in enumerate(steps):
        step_id = str(_get(step_def, "id", "") or "")
        if not step_id:
            raise WorkflowError(
                f"workflow '{name}': step {index} is missing required 'id'."
            )
        label = str(_get(step_def, "label", "") or step_id)
        process = str(_get(step_def, "process", "") or "")
        if not process:
            raise WorkflowError(
                f"workflow '{name}': step '{step_id}' is missing required 'process'."
            )
        step_views.append((step_id, label, process))

    # Configuration this engine no longer honours is a hard error, never a
    # silently ignored key: leaving it parseable implies it is still supported
    # and invites new code to depend on it. Rollback is manifest-driven and
    # selects source_file_mutation records, so folder-based restore mappings
    # have no consumer (SGC_Log_Run_Rollback).
    for retired in _RETIRED_WORKFLOW_KEYS:
        if _get(workflow, retired) is not None:
            raise WorkflowError(
                f"workflow '{name}' declares retired key '{retired}'. Rollback "
                "is manifest-driven; remove this key from the workflow "
                "definition."
            )

    # ``only`` is the legacy single-step-by-id parameter; treat it as ``step``.
    if only is not None and step is None:
        step = only
    selected = _select_steps(
        step_views, name, step=step, from_step=from_step, to_step=to_step
    )

    run_ctx = RunContext(apply=apply, metadata=dict(metadata or {}), data={"ctx": ctx})
    run = WorkflowRun(name=name, status="success", context=run_ctx)

    # Append-only run logging (SGC_Rey_Workflow_Pipeline_Automatic_Control_Batch_Logging):
    # emit a RUN_START, per-step STEP_START/STEP_END, and a RUN_COMPLETE + deterministic
    # RUN_SUMMARY through the log_utils authority. Record emission is fail-safe and
    # never alters workflow behavior. run_id is established by the logging layer; all
    # records share it and RUN_START carries the workflow name.
    try:
        ctx.workflow_name = name
    except (AttributeError, TypeError):
        # Some tests intentionally pass bare object() as ctx; proceed without
        # mutating ctx when it cannot accept dynamic attributes.
        pass
    # Workflow semantic base -> level 4: a workflow executes inside an app, so it
    # sits below the app base (SGC_Rey_Log_Nest_Level_Hierarchy_Correction).
    run_log.set_nest_level("workflow")
    log_run_start(run_log, workflow=name, apply=apply)
    # Bind this run log so file_utils records file operations through it
    # (SGC_Rey_File_Utils_Ambient_Run_Log_File_Recording); cleared at every exit.
    bind_run(run_log)
    # Record the config files that fed the effective ctx from provenance
    # (SGC_Rey_Config_Utils_Run_Log_Config_File_Recording).
    record_config_file_references(ctx, run_log)

    sequence = 0
    for index, step_def in enumerate(steps):
        if index not in selected:
            continue
        step_id, label, process = step_views[index]
        if process not in processes:
            raise _pre_execution_failure(
                ctx,
                run_log, run,
                step_id=step_id,
                label=label,
                process=process,
                sequence=sequence + 1,
                message=(
                    f"workflow '{name}': step '{step_id}' calls undefined process "
                    f"'{process}'. Define it under workflow.processes."
                ),
            )
        handler = registry.get(process)
        if handler is None:
            raise _pre_execution_failure(
                ctx,
                run_log, run,
                step_id=step_id,
                label=label,
                process=process,
                sequence=sequence + 1,
                message=(
                    f"workflow '{name}': process '{process}' has no registered "
                    f"handler in this app."
                ),
            )

        effective = _expand_config(
            _deep_merge(_to_mapping(processes.get(process)),
                        _to_mapping(_get(step_def, "config"))),
            tokens,
        )

        sequence += 1
        step_name = label or step_id
        bind_step(
            step_id=step_id,
            step_name=step_name,
            step_sequence=sequence,
            workflow_name=name,
        )
        try:
            # Workflow-step semantic boundary -> level 5, one per sequential in-process
            # step dispatch (SGC_Rey_Log_Nest_Level_Hierarchy_Correction). The descent
            # from workflow (4) parents each step under the workflow record.
            run_log.set_nest_level("workflow_step")
            log_step_start(run_log, step_name, sequence, step_type=process, step_id=step_id
            )
            # STEP_START anchors the workflow-step scope. Handler, lifecycle, file,
            # and result evidence belongs one level beneath that durable record.
            run_log.enter()

            if not apply and bool(effective.get("apply_only")):
                log_step_end(run_log, step_name, "skipped", message="dry-run")
                run.outcomes.append(
                    StepOutcome(step_id, label, process, "skipped", "dry-run")
                )
                _logger.info("workflow '%s' step '%s' skipped (dry-run).", name, step_id)
                continue

            try:
                result = handler(ctx, run_log, effective, run_ctx)
            except Exception as exc:  # noqa: BLE001 — fail closed: record and stop
                error_payload = build_safe_error_payload(
                    exc,
                    message=f"workflow '{name}' step '{step_id}' failed",
                    failed_step_id=step_id,
                    failed_step_name=step_name,
                    failed_step_sequence=sequence,
                )
                error_record = log_error(run_log, **error_payload)
                # The text, not the payload -- see build_error_record_payload.
                failure_message = str(error_record.get("message") or "")
                failure_id = str(error_record.get("error_id") or "")
                failure_id = log_step_failure(run_log,
                    failed_step_id=step_id,
                    failed_step_name=step_name,
                    message=failure_message,
                    failure_record_id=failure_id,
                    error_id=failure_id,
                    failed_step_sequence=sequence,
                )
                log_step_end(run_log, step_name, "failed", message=failure_message)
                run.outcomes.append(
                    StepOutcome(step_id, label, process, "failed", error=failure_message)
                )
                run.status = "failed"
                _logger.error("workflow '%s' step '%s' failed: %s", name, step_id, exc)
                run_log.set_nest_level("workflow")
                log_run_complete(run_log,
                    "failed",
                    message=failure_message,
                    failure_record_id=failure_id,
                    failed_step_id=step_id,
                    failed_step_name=step_name,
                    failure_message=failure_message,
                )
                _finalize_run(ctx, run_log)
                clear_run()
                return run

            status = str(getattr(result, "status", "ok")) if result is not None else "ok"
            detail = str(getattr(result, "detail", "")) if result is not None else ""
            artifacts = list(getattr(result, "artifacts", []) or []) if result is not None else []
            log_step_end(run_log, step_name, status, message=detail)
            run.outcomes.append(StepOutcome(step_id, label, process, status, detail, artifacts))
            if status == "failed":
                run.status = "failed"
                failure_message = detail or f"workflow step '{step_id}' failed."
                failure_id = log_step_failure(run_log,
                    failed_step_id=step_id,
                    failed_step_name=step_name,
                    message=failure_message,
                    error_type="WorkflowStepFailed",
                    error_message=failure_message,
                    sanitized_exception=failure_message,
                    failed_step_sequence=sequence,
                )
                run_log.set_nest_level("workflow")
                log_run_complete(run_log,
                    "failed",
                    message=failure_message,
                    failure_record_id=failure_id,
                    failed_step_id=step_id,
                    failed_step_name=step_name,
                    failure_message=failure_message,
                )
                _finalize_run(ctx, run_log)
                clear_run()
                return run
        finally:
            clear_step()

    run_log.set_nest_level("workflow")
    log_run_complete(run_log, "success")
    _finalize_run(ctx, run_log)
    clear_run()
    return run


def _workflow_execution_details(
    run: "WorkflowRun",
    *,
    apply: bool,
    only: Optional[str],
    step: Optional[str],
    from_step: Optional[str],
    to_step: Optional[str],
) -> dict[str, Any]:
    """Build workflow execution_details (domain facts only) for the shared summary.

    Contributes only workflow-specific facts — execution mode, step selection, and
    per-step outcomes — namespaced under ``workflow``. Common fields (status, counts,
    timestamps) are derived by rey_lib.logs from the completed log, not repeated here
    (SGC_Rey_Lib_Log_Summary_Framework_And_Run_Summary).
    """
    steps: list[dict[str, Any]] = []
    for outcome in run.outcomes:
        entry: dict[str, Any] = {
            "id": outcome.id,
            "label": outcome.label,
            "process": outcome.process,
            "status": outcome.status,
        }
        if outcome.detail:
            entry["detail"] = outcome.detail
        steps.append(entry)
    return {
        "kind": "workflow",
        "workflow": {
            "mode": "apply" if apply else "dry_run",
            "selection": {
                "only": only,
                "step": step,
                "from_step": from_step,
                "to_step": to_step,
            },
            "steps": steps,
        },
    }


def _select_steps(
    step_views: list[tuple[str, str, str]],
    name: str,
    *,
    step: Optional[str],
    from_step: Optional[str],
    to_step: Optional[str],
) -> set[int]:
    """Resolve the set of step indices to execute (fail closed).

    ``step`` selects one step; ``from_step`` runs to the end; ``to_step`` runs
    from the start; ``from_step`` + ``to_step`` is the inclusive ordered range.
    ``step`` cannot combine with a range, and a reversed range fails closed. With
    no selector, every step runs.
    """
    total = len(step_views)
    if step is not None and (from_step is not None or to_step is not None):
        raise WorkflowError(
            f"workflow '{name}': 'step' cannot be combined with 'from_step'/'to_step'."
        )
    if step is None and from_step is None and to_step is None:
        return set(range(total))

    if step is not None:
        return {_resolve_step_index(step_views, name, step)}

    start = _resolve_step_index(step_views, name, from_step) if from_step is not None else 0
    end = _resolve_step_index(step_views, name, to_step) if to_step is not None else total - 1
    if start > end:
        raise WorkflowError(
            f"workflow '{name}': from_step '{from_step}' resolves after to_step "
            f"'{to_step}' (empty range)."
        )
    return set(range(start, end + 1))


def _resolve_step_index(
    step_views: list[tuple[str, str, str]], name: str, identifier: str
) -> int:
    """Return the one step index matching ``identifier`` by id, label, or process.

    Fails closed: raises on no match, and on an ambiguous match (more than one
    step). No fuzzy matching.
    """
    matches = [
        index
        for index, view in enumerate(step_views)
        if identifier in view  # view == (id, label, process)
    ]
    if not matches:
        raise WorkflowError(
            f"workflow '{name}': no step matches identifier '{identifier}'."
        )
    if len(matches) > 1:
        raise WorkflowError(
            f"workflow '{name}': step identifier '{identifier}' is ambiguous "
            f"(matches {len(matches)} steps)."
        )
    return matches[0]


# ---------------------------------------------------------------------------
# Token resolution and effective config
# ---------------------------------------------------------------------------

def _resolve_tokens(tokens_cfg: Any) -> dict[str, str]:
    """Resolve workflow-local tokens, allowing one token to reference another.

    Global path tokens (``{data}``, ``{llmcontracts}``, ...) and runtime
    placeholders (``{engine}``, ``{database}``, ...) are left intact for the
    downstream ctx path resolver / render step — only workflow-local token
    names are substituted here.
    """
    raw = {str(k): str(v) for k, v in _to_mapping(tokens_cfg).items()}
    resolved = dict(raw)
    for _ in range(len(raw) + 1):
        changed = False
        for key, value in list(resolved.items()):
            expanded = _expand_str(value, resolved)
            if expanded != value:
                resolved[key] = expanded
                changed = True
        if not changed:
            break
    return resolved


def _expand_str(text: str, tokens: Mapping[str, str]) -> str:
    """Substitute ``{name}`` for each workflow-local token; leave others intact."""
    for key, value in tokens.items():
        placeholder = "{" + key + "}"
        if placeholder in text:
            text = text.replace(placeholder, value)
    return text


def _expand_config(value: Any, tokens: Mapping[str, str]) -> Any:
    """Recursively expand workflow-local tokens in config string values."""
    if isinstance(value, str):
        return _expand_str(value, tokens)
    if isinstance(value, dict):
        return {key: _expand_config(item, tokens) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_config(item, tokens) for item in value]
    return value


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Return base merged with override; nested dicts merge, else override wins."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Mapping/attribute access helpers (accept dict- or Namespace-like inputs)
# ---------------------------------------------------------------------------

def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Return obj[key] / obj.key, or default."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _to_mapping(obj: Any) -> dict[str, Any]:
    """Return a plain dict view of a dict- or Namespace-like object."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    if hasattr(obj, "items"):
        try:
            return dict(obj.items())
        except Exception:  # noqa: BLE001 — fall through to attribute view
            pass
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return {}


def _as_list(obj: Any) -> list[Any]:
    """Coerce a value to a list (None -> [])."""
    if obj is None:
        return []
    if isinstance(obj, list):
        return obj
    return [obj]
