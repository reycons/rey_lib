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

from rey_lib.logs import get_logger
from rey_lib.workflow.engine import RunContext, WorkflowError

__all__ = ["StepOutcome", "WorkflowRun", "run_workflow", "ProcessHandler"]

_logger = get_logger(__name__)

# A process handler: (ctx, effective_config, run_context) -> result-or-None.
# The result may be any object exposing ``status``/``detail``; None means "ok".
ProcessHandler = Callable[[Any, dict[str, Any], RunContext], Any]


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


def run_workflow(
    ctx: Any,
    workflow: Any,
    registry: Mapping[str, ProcessHandler],
    *,
    apply: bool = True,
    only: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> WorkflowRun:
    """Stack the workflow's step function calls per its YAML config.

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
        true is skipped.
    only : str | None
        When set, run only the step whose ``id`` matches (single-step).
    metadata : dict | None
        Shared run metadata seeded into the run context.

    Returns
    -------
    WorkflowRun
        Ordered step outcomes and overall status ("failed" stops the run).

    Raises
    ------
    WorkflowError
        On a malformed workflow: a step missing ``id``/``process``, a step
        calling a process not defined under ``processes``, or a process with no
        registered handler in this app (fail closed — never guess).
    """
    name = str(_get(workflow, "name", "") or "")
    tokens = _resolve_tokens(_get(workflow, "tokens"))
    processes = _to_mapping(_get(workflow, "processes"))
    steps = _as_list(_get(workflow, "steps"))

    run_ctx = RunContext(apply=apply, metadata=dict(metadata or {}), data={"ctx": ctx})
    run = WorkflowRun(name=name, status="success", context=run_ctx)

    for index, step in enumerate(steps):
        step_id = str(_get(step, "id", "") or "")
        if not step_id:
            raise WorkflowError(
                f"workflow '{name}': step {index} is missing required 'id'."
            )
        label = str(_get(step, "label", "") or step_id)
        process = str(_get(step, "process", "") or "")
        if not process:
            raise WorkflowError(
                f"workflow '{name}': step '{step_id}' is missing required 'process'."
            )
        if only is not None and step_id != only:
            continue
        if process not in processes:
            raise WorkflowError(
                f"workflow '{name}': step '{step_id}' calls undefined process "
                f"'{process}'. Define it under workflow.processes."
            )
        handler = registry.get(process)
        if handler is None:
            raise WorkflowError(
                f"workflow '{name}': process '{process}' has no registered handler "
                f"in this app."
            )

        effective = _expand_config(
            _deep_merge(_to_mapping(processes.get(process)),
                        _to_mapping(_get(step, "config"))),
            tokens,
        )

        if not apply and bool(effective.get("apply_only")):
            run.outcomes.append(
                StepOutcome(step_id, label, process, "skipped", "dry-run")
            )
            _logger.info("workflow '%s' step '%s' skipped (dry-run).", name, step_id)
            continue

        try:
            result = handler(ctx, effective, run_ctx)
        except Exception as exc:  # noqa: BLE001 — fail closed: record and stop
            run.outcomes.append(
                StepOutcome(step_id, label, process, "failed", error=str(exc))
            )
            run.status = "failed"
            _logger.error("workflow '%s' step '%s' failed: %s", name, step_id, exc)
            return run

        status = str(getattr(result, "status", "ok")) if result is not None else "ok"
        detail = str(getattr(result, "detail", "")) if result is not None else ""
        artifacts = list(getattr(result, "artifacts", []) or []) if result is not None else []
        run.outcomes.append(StepOutcome(step_id, label, process, status, detail, artifacts))
        if status == "failed":
            run.status = "failed"
            return run

    return run


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
