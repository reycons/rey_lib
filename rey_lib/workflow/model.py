"""The canonical workflow objects: Workflow -> WorkflowProcess -> WorkflowStep.

A workflow is a named, installation-scoped definition. Its processes each bind
one application's published operation with process defaults, and each process
owns the steps that invoke it. Flow between processes is the workflow's;
execution belongs to the process and its steps. Scheduling is not modelled.

    Workflow
    └── WorkflowProcess   application, operation, defaults
        └── WorkflowStep  key, ordinal, label, overrides

**No application on Workflow.** Each process names its own application, so a
workflow is never assumed to belong to one. Today's files declare ownership once
at the file root; hydration copies that value onto every process and discards
it.

**No source layout.** These objects know nothing about files or rows. They are
built by the one hydration boundary, :mod:`rey_lib.workflow.hydration`; when the
definitions move to the database only that boundary's input changes.

Public API
----------
Workflow         One workflow: identity, installation, execution, tokens, processes.
WorkflowProcess  One process: application, operation, defaults, its steps.
WorkflowStep     One invocation of its process: key, ordinal, label, overrides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from rey_lib.workflow.engine import WorkflowError

__all__ = ["EXECUTION_DEFAULTS", "Workflow", "WorkflowProcess", "WorkflowStep"]

#: Engine vocabulary a process may carry, which is not part of any
#: application's published parameter contract.
_ENGINE_KEYS = frozenset({"implementation", "apply_only", "always_run", "enabled", "name"})

#: The execution contract (ADR-007) as a workflow that declares nothing has it:
#: runnable and dry-runnable, never step- or range-selectable by omission.
EXECUTION_DEFAULTS: tuple[tuple[str, bool], ...] = (
    ("full", True),
    ("dry_run", True),
    ("step", False),
    ("range", False),
)


@dataclass(frozen=True)
class WorkflowStep:
    """One invocation of its process.

    ``overrides`` is the step's ``config`` exactly as declared. A key that is
    absent, null, false, an empty mapping or an empty list stays what it was:
    the process applies it, so nothing here decides what any of them mean.
    """

    key: str
    ordinal: int
    label: str
    overrides: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowProcess:
    """One process: an application's operation, with defaults, and its steps.

    Several processes may bind the same operation with different defaults, and
    one process may be invoked by several steps.
    """

    name: str
    application: str
    operation: str
    defaults: Mapping[str, Any] = field(default_factory=dict)
    steps: tuple[WorkflowStep, ...] = ()

    def configuration(self, step: WorkflowStep) -> dict[str, Any]:
        """The process defaults with the step's overrides applied.

        Nested mappings merge; any other override -- null, false, an empty list
        -- replaces the default.
        """
        return _deep_merge(dict(self.defaults), dict(step.overrides))

    def apply_only(self, step: WorkflowStep) -> bool:
        """Whether the step is skipped in a dry run."""
        return bool(self.configuration(step).get("apply_only"))

    def always_run(self, step: WorkflowStep) -> bool:
        """Whether the step stays eligible after an earlier step failed."""
        return bool(self.configuration(step).get("always_run"))

    def effective(self, step: WorkflowStep, tokens: Mapping[str, str]) -> dict[str, Any]:
        """The configuration the operation receives for this step.

        The engine's own vocabulary is removed -- it says how a step is treated,
        not what the application asked for -- and workflow-local tokens are
        expanded.
        """
        declared = {
            key: value for key, value in self.configuration(step).items()
            if key not in _ENGINE_KEYS
        }
        return _expand_config(declared, tokens)


@dataclass(frozen=True)
class Workflow:
    """One installation-scoped workflow.

    ``tokens`` are the declarations as written; :meth:`resolved_tokens` resolves
    them.
    """

    name: str
    installation: str
    enabled: bool = True
    execution: Mapping[str, bool] = field(default_factory=lambda: dict(EXECUTION_DEFAULTS))
    tokens: Mapping[str, Any] = field(default_factory=dict)
    processes: tuple[WorkflowProcess, ...] = ()

    def ordered_steps(self) -> tuple[tuple[WorkflowProcess, WorkflowStep], ...]:
        """Every process's steps, each with its process, in ordinal order."""
        return tuple(sorted(
            ((process, step) for process in self.processes for step in process.steps),
            key=lambda pair: pair[1].ordinal,
        ))

    def resolved_tokens(self) -> dict[str, str]:
        """Workflow-local tokens, allowing one token to reference another.

        Global path tokens (``{data}``, ...) and runtime placeholders
        (``{engine}``, ``{database}``, ...) are left intact for the downstream
        ctx path resolver / render step -- only workflow-local token names are
        substituted here.
        """
        raw = {str(k): str(v) for k, v in self.tokens.items()}
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

    def select(
        self,
        *,
        step: Optional[str] = None,
        from_step: Optional[str] = None,
        to_step: Optional[str] = None,
    ) -> set[int]:
        """The ordinals to execute (fail closed).

        ``step`` selects one step; ``from_step`` runs to the end; ``to_step``
        runs from the start; ``from_step`` + ``to_step`` is the inclusive
        ordered range. ``step`` cannot combine with a range, and a reversed
        range fails closed. With no selector, every step runs. An identifier
        matches a step's key, label or process name.
        """
        ordered = self.ordered_steps()
        if step is not None and (from_step is not None or to_step is not None):
            raise WorkflowError(
                f"workflow '{self.name}': 'step' cannot be combined with "
                "'from_step'/'to_step'."
            )
        if step is None and from_step is None and to_step is None:
            return {one.ordinal for _process, one in ordered}

        if step is not None:
            return {ordered[self._index(ordered, step)][1].ordinal}

        start = self._index(ordered, from_step) if from_step is not None else 0
        end = self._index(ordered, to_step) if to_step is not None else len(ordered) - 1
        if start > end:
            raise WorkflowError(
                f"workflow '{self.name}': from_step '{from_step}' resolves after "
                f"to_step '{to_step}' (empty range)."
            )
        return {one.ordinal for _process, one in ordered[start:end + 1]}

    def _index(
        self,
        ordered: tuple[tuple[WorkflowProcess, WorkflowStep], ...],
        identifier: str,
    ) -> int:
        """The one position matching ``identifier``; no match or several refuse."""
        matches = [
            index for index, (process, one) in enumerate(ordered)
            if identifier in (one.key, one.label, process.name)
        ]
        if not matches:
            raise WorkflowError(
                f"workflow '{self.name}': no step matches identifier '{identifier}'."
            )
        if len(matches) > 1:
            raise WorkflowError(
                f"workflow '{self.name}': step identifier '{identifier}' is "
                f"ambiguous (matches {len(matches)} steps)."
            )
        return matches[0]


# ---------------------------------------------------------------------------
# Token expansion and merge
# ---------------------------------------------------------------------------

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
