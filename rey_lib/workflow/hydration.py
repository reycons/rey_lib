"""The one hydration boundary of the workflow objects.

Reads a workflow's source representation and constructs the canonical object
graph -- ``Workflow`` -> ``WorkflowProcess`` -> ``WorkflowStep``. Nothing
downstream knows what the source was: the objects, the coordinator and every
caller see only the graph.

    source representation  ->  hydrate_workflow  ->  Workflow

Today the source is the entry the configuration load resolved, with fragment
files already deep-merged into one definition. Fragment merging stays in that
load and is not this module's. When the definitions move to the database, the
input becomes ``workflow_flat_vw`` and nothing else changes.

Everything that knows the file layout is here. The file declares its
application once at the root, a process binds ``implementation``, and a step
carries ``config``. Each of these is read and translated, never passed on.

Public API
----------
hydrate_workflow    A source entry -> its canonical Workflow.
"""

from __future__ import annotations

from typing import Any

from rey_lib.logs import AppLogger
from rey_lib.workflow.engine import WorkflowError
from rey_lib.workflow.model import (
    EXECUTION_DEFAULTS,
    Workflow,
    WorkflowProcess,
    WorkflowStep,
)

__all__ = ["hydrate_workflow"]

_logger = AppLogger(__name__)

# Workflow configuration the engine no longer honours. Declaring one is a hard
# error, never a silently ignored key (SGC_Log_Run_Rollback).
_RETIRED_WORKFLOW_KEYS = ("restore_mappings",)


def hydrate_workflow(entry: Any, *, installation: str) -> Workflow:
    """Construct the canonical Workflow from its source entry.

    A disabled workflow is hydrated as identity only: being switched off is
    decided before the definition is read, so it is never refused for a defect
    in steps it will not run.

    The file's root application is copied onto every process, which is how
    today's files declare ownership. The Workflow itself holds no application.

    Args:
        entry: One resolved workflow entry (a mapping or a Namespace).
        installation: The installation the workflow belongs to.

    Returns:
        The Workflow, its processes, and each process's steps.

    Raises:
        WorkflowError: A step without ``id`` or ``process``, a step naming a
            process the workflow does not define, or a retired key.
    """
    name = str(_get(entry, "name", "") or "")
    _logger.debug("workflow: hydrating %r of installation %s", name, installation)
    execution = _declared_execution(entry)
    if _get(entry, "enabled", True) is False:
        return Workflow(name=name, installation=installation, enabled=False,
                        execution=execution)

    application = str(_get(entry, "app", "") or "")
    declared = _to_mapping(_get(entry, "processes"))
    children: dict[str, list[WorkflowStep]] = {key: [] for key in declared}
    for index, step_def in enumerate(_as_list(_get(entry, "steps"))):
        key = str(_get(step_def, "id", "") or "")
        if not key:
            raise WorkflowError(
                f"workflow '{name}': step {index} is missing required 'id'."
            )
        process = str(_get(step_def, "process", "") or "")
        if not process:
            raise WorkflowError(
                f"workflow '{name}': step '{key}' is missing required 'process'."
            )
        if process not in children:
            raise WorkflowError(
                f"workflow '{name}': step '{key}' calls undefined process "
                f"'{process}'. Define it under workflow.processes."
            )
        children[process].append(WorkflowStep(
            key=key,
            ordinal=index + 1,
            label=str(_get(step_def, "label", "") or key),
            overrides=_to_mapping(_get(step_def, "config")),
        ))

    # Configuration the engine no longer honours is a hard error, never a
    # silently ignored key: leaving it parseable implies it is still supported
    # and invites new code to depend on it. Rollback is manifest-driven and
    # selects source_file_mutation records, so folder-based restore mappings
    # have no consumer (SGC_Log_Run_Rollback).
    for retired in _RETIRED_WORKFLOW_KEYS:
        if _get(entry, retired) is not None:
            raise WorkflowError(
                f"workflow '{name}' declares retired key '{retired}'. Rollback "
                "is manifest-driven; remove this key from the workflow "
                "definition."
            )

    processes = []
    for process, value in declared.items():
        defaults = _to_mapping(value)
        processes.append(WorkflowProcess(
            name=str(process),
            application=application,
            operation=str(defaults.get("implementation") or ""),
            defaults=defaults,
            steps=tuple(children[process]),
        ))
    return Workflow(
        name=name,
        installation=installation,
        enabled=True,
        execution=execution,
        tokens=_to_mapping(_get(entry, "tokens")),
        processes=tuple(processes),
    )


def _declared_execution(entry: Any) -> dict[str, bool]:
    """The execution contract an entry declares, defaults applied (ADR-007).

    A workflow defaults to full-run and dry-run enabled but step/range
    disabled, so unsafe partial execution is never enabled by omission.
    """
    raw = _to_mapping(_get(entry, "execution"))
    return {key: bool(raw.get(key, default)) for key, default in EXECUTION_DEFAULTS}


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
        except (TypeError, ValueError):
            # Not a mapping's items after all; fall through to the attribute view.
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
