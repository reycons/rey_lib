"""WorkflowStep -- one ApplicationOperation instantiated in a workflow.

The leaf of the workflow object model (backlog 950). A step is one fully
configured ApplicationOperation: bound once, at construction, to an operation
its application publishes and to the implementation the application's approved
catalog holds for it, with its resolved settings (already composed by
hydration) and its declarations (``apply_only``, ``always_run``).

It is single-use and holds no execution state. The Workflow checks its contract
and decides when it can run, then creates a child Run for it; that Run executes
the step's bound operation and owns the outcome. A completed step is removed
from its Workflow's and process's active steps.

Built beside the recovered procedural engine (backlog 949); nothing here imports
it.

Public API
----------
WorkflowStep    A bound, configured operation instance.
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from rey_lib.config.applications import Application, ApplicationOperation
from rey_lib.config.inventory import to_plain_data
from rey_lib.config.operation_contract import contract_review
from rey_lib.errors.error_utils import AppError
from rey_lib.logs import AppLogger

if TYPE_CHECKING:
    from rey_lib.workflow.workflow_process import WorkflowProcess

__all__ = ["WorkflowStep"]

_logger = AppLogger(__name__)


class WorkflowStep:
    """One ApplicationOperation instantiated in a workflow, with its own settings.

    Attributes
    ----------
    id : str
        The step's authored identity.
    process_name : str
        The authored process the step was declared under.
    label : str
        The step's human label; the step name in run records.
    ordinal : int
        The step's position in its workflow's execution order.
    operation : ApplicationOperation
        The published operation this step instantiates.
    implementation : Callable
        The approved implementation bound to that operation.
    settings : dict
        This instance's configured settings, already composed.
    apply_only : bool
        Held for the Workflow: the step is not run in a dry run.
    always_run : bool
        Held for the Workflow: the step stays eligible after an earlier failure.
    process : WorkflowProcess | None
        The WorkflowProcess that owns this step; set by that process when it is
        constructed, None before.
    """

    def __init__(
        self,
        *,
        id: str,
        process_name: str,
        label: str,
        ordinal: int,
        application: Application,
        operation_name: str,
        approved: Mapping[str, Callable[..., Any]],
        settings: Mapping[str, Any],
        apply_only: bool = False,
        always_run: bool = False,
    ) -> None:
        """Bind the step to its published operation and approved implementation.

        Parameters
        ----------
        id, process_name, label, ordinal
            The step's authored identity and position.
        application : Application
            The application the step's process instantiates.
        operation_name : str
            The name of the operation, as the application publishes it.
        approved : Mapping[str, Callable]
            The application's approved implementation catalog.
        settings : Mapping[str, Any]
            The step's composed settings for this operation instance.
        apply_only, always_run : bool
            Engine flags, held for the Workflow.

        Raises
        ------
        AppError
            When the application does not publish the operation, or publishes
            it without approving an implementation for it.
        """
        if not id:
            raise AppError("a workflow step is missing required 'id'.")
        self.id = id
        self.process_name = process_name
        self.label = label or id
        self.ordinal = ordinal
        self.operation = _published_operation(application, operation_name, id)
        self.implementation = _approved_implementation(
            application, approved, operation_name, id
        )
        # Deep-copied: a handler mutating a nested setting must not reach the
        # mapping hydration passed in, or any other holder of it.
        self.settings: dict[str, Any] = deepcopy(dict(settings))
        self.apply_only = apply_only
        self.always_run = always_run
        self.process: Optional["WorkflowProcess"] = None

    @classmethod
    def hydrate(
        cls,
        row: Mapping[str, Any],
        *,
        application: Application,
        approved: Mapping[str, Callable[..., Any]],
    ) -> "WorkflowStep":
        """The step one governed row states (backlog 963).

        Takes the row as control.workflow_flat_vw shapes it, with its
        ``step_settings`` already resolved by the workflow's hydration; those
        are the operation's settings only and are passed through unchanged.

        ``apply_only`` and ``always_run`` are this step's orchestration
        properties, read from
        ``row.workflow_config.execution.steps[row.workflow_step_name]``. Both
        must be booleans; a missing or invalid value is refused, never filled
        from this constructor's defaults.

        Raises:
            AppError: Naming the step, when either control is missing or not a
                boolean, or when the constructor refuses the binding.
        """
        step_id = str(row.get("workflow_step_name") or "")
        execution = (row.get("workflow_config") or {}).get("execution") or {}
        controls = (execution.get("steps") or {}).get(step_id) or {}
        for name in ("apply_only", "always_run"):
            if not isinstance(controls.get(name), bool):
                raise AppError(
                    f"step '{step_id}': workflow_config.execution.steps states no "
                    f"boolean '{name}' for it."
                )
        return cls(
            id=step_id,
            process_name=str(row.get("configured_process_name") or ""),
            label=str(row.get("step_label") or ""),
            ordinal=int(row["step_ordinal"]),
            application=application,
            operation_name=str(row.get("application_operation_name") or ""),
            approved=approved,
            settings=row.get("step_settings") or {},
            apply_only=controls["apply_only"],
            always_run=controls["always_run"],
        )

    def contract_refusal(self, workflow_name: str) -> str:
        """Why this step's settings do not satisfy its operation's contract, or ''.

        The settings are reviewed whole against the operation's
        ``contract_definition`` (JSON Schema, backlog 939). Unsupported keys are
        tolerated with a warning naming them; a violation refuses. The Workflow
        calls this before releasing the step.
        """
        unsupported, violations = contract_review(
            self.operation.contract_definition or {},
            to_plain_data(self.settings),
        )
        if unsupported:
            _logger.warning(
                "workflow '%s': process '%s' carries settings the contract of "
                "implementation '%s' does not support; they are ignored: %s",
                workflow_name, self.process_name, self.operation.name,
                "; ".join(unsupported),
            )
        if not violations:
            return ""
        return (
            f"workflow '{workflow_name}': process '{self.process_name}' settings do not "
            "satisfy the "
            f"contract of implementation '{self.operation.name}': "
            + "; ".join(violations)
        )


def _published_operation(
    application: Application, operation_name: str, step_id: str
) -> ApplicationOperation:
    """The operation the application publishes under that name, or a refusal.

    Read from the application's own published operations; an operation another
    application publishes is not found here, so it is refused the same way.
    """
    for operation in application.workflow_operations:
        if operation.name == operation_name:
            return operation
    raise AppError(
        f"step '{step_id}' names operation '{operation_name}', which application "
        f"'{application.name}' does not publish."
    )


def _approved_implementation(
    application: Application,
    approved: Mapping[str, Callable[..., Any]],
    operation_name: str,
    step_id: str,
) -> Callable[..., Any]:
    """The implementation the application approves for the operation, or a refusal.

    Published but not approved is a gap in the application's own answer, never
    something to resolve by importing a name.
    """
    implementation = approved.get(operation_name)
    if implementation is None:
        raise AppError(
            f"step '{step_id}': operation '{operation_name}' is published by "
            f"'{application.name}' but is not in its approved implementation catalog."
        )
    return implementation
