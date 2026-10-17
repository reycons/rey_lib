"""WorkflowProcess -- one Application instantiated in a workflow.

The middle of the workflow object model (backlog 950). A process is one
configured instance of an existing Application: it holds that Application, its
own application configuration as governed for this instance, and groups its
WorkflowStep children -- each a configured instance of one of that
Application's operations. Two processes may instantiate the same Application
with different configuration.

It does not route execution: the Workflow decides when a step can run and
creates its child Run, and the child Run executes the step's operation. A
step that has run stays in ``steps``; its own state says how it ran.

It owns population state, not catalog discovery. The workflow hydrator
materialises the graph from the governed rows and the approved implementation
catalog its caller injects -- as ManifestSource takes its reader -- and each
WorkflowStep arrives already bound to its ApplicationOperation and approved
implementation.

Public API
----------
WorkflowProcess    A configured Application instance and its steps.
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional, Sequence

from rey_lib.config.applications import Application
from rey_lib.config.config_namespace import Namespace
from rey_lib.errors.error_utils import AppError
from rey_lib.workflow.workflow_step import WorkflowStep

if TYPE_CHECKING:
    from rey_lib.workflow.workflow import Workflow

__all__ = ["WorkflowProcess"]


class WorkflowProcess:
    """One Application instantiated in a workflow, with its configuration and steps.

    Attributes
    ----------
    name : str
        The process's governed name.
    ordinal : int
        The process's position in its workflow.
    application : Application
        The existing Application this process instantiates.
    config : dict | None
        This instance's application configuration as governed; None where the
        governed value is null, kept distinct from an empty mapping.
    steps : tuple[WorkflowStep, ...]
        The process's steps, in the order hydration gives them.
    workflow : Workflow | None
        The Workflow this process belongs to; set by that Workflow when it is
        constructed, None before.
    """

    def __init__(
        self,
        *,
        name: str,
        ordinal: int,
        application: Application,
        config: Optional[Mapping[str, Any]],
        steps: Sequence[WorkflowStep],
    ) -> None:
        """Hold the Application instance, its configuration and its steps.

        Parameters
        ----------
        name, ordinal
            The process's governed identity and position.
        application : Application
            The Application this process instantiates.
        config : Mapping[str, Any] | None
            The process's application configuration as governed. Copied, so
            no other process or caller shares it.
        steps : Sequence[WorkflowStep]
            The process's steps, each already bound by hydration. Each is made
            this process's own.

        Raises
        ------
        AppError
            When a step instantiates an operation this process's Application
            does not publish, or already belongs to another process.
        """
        if not name:
            raise AppError("a workflow process is missing required 'name'.")
        self.name = name
        self.ordinal = ordinal
        self.application = application
        self.config: Optional[dict[str, Any]] = (
            None if config is None else deepcopy(dict(config))
        )
        # By identity: a step is bound to the operation object its Application
        # published, so an equal operation another Application publishes is not it.
        published = application.workflow_operations
        for step in steps:
            if not any(step.operation is one for one in published):
                raise AppError(
                    f"process '{name}': step '{step.id}' instantiates operation "
                    f"'{step.operation.name}', which application "
                    f"'{application.name}' does not publish."
                )
            if step.process is not None:
                raise AppError(
                    f"process '{name}': step '{step.id}' already belongs to process "
                    f"'{step.process.name}'."
                )
        self.steps: tuple[WorkflowStep, ...] = tuple(steps)
        for step in self.steps:
            step.process = self
        self.workflow: Optional["Workflow"] = None

    def data_source(self, name: str) -> Namespace:
        """One of this process's data sources, by name (backlogs 1032, 1043).

        A data source is part of the process's governed configuration
        (``config.data_sources``), held here as the Workflow's hydration
        resolved it, so its paths are the workflow's paths. A step asks its own
        process; nothing reads the configuration or the runtime context for one.

        Raises:
            AppError: When this process declares no data source of that name.
        """
        return self._named("data_sources", name, "data source")

    def analysis_config(self, name: str) -> Namespace:
        """One of this process's analysis configs, by name (backlog 1005).

        An analysis config is part of the process's governed configuration
        (``config.analysis_configs``), held here as the Workflow's hydration
        resolved it. A step asks its own process; nothing reads the runtime
        context for one.

        Raises:
            AppError: When this process declares no analysis config of that name.
        """
        return self._named("analysis_configs", name, "analysis config")

    def _named(self, section: str, name: str, kind: str) -> Namespace:
        """The entry named ``name`` in this process's hydrated ``config[section]``.

        The one lookup every named entry of the process's configuration is read
        through, so no two answers drift. Attribute-readable; nothing is copied
        from anywhere but this process's own configuration.
        """
        for entry in (self.config or {}).get(section) or []:
            if isinstance(entry, Mapping) and entry.get("name") == name:
                return Namespace(dict(entry))
        raise AppError(f"process '{self.name}' declares no {kind} '{name}'.")

    @classmethod
    def hydrate(
        cls,
        rows: Sequence[Mapping[str, Any]],
        *,
        application: Application,
        approved: Mapping[str, Callable[..., Any]],
    ) -> "WorkflowProcess":
        """The process its governed rows state, with its steps (backlog 963).

        ``rows`` are this process's rows of control.workflow_flat_vw, their
        configuration already resolved by the workflow's hydration. A process
        without steps is one row with no step columns, and has no steps.

        Raises:
            AppError: When the rows are empty, or a step or the process is refused.
        """
        if not rows:
            raise AppError("a workflow process was hydrated from no rows.")
        first = rows[0]
        return cls(
            name=str(first.get("process_name") or ""),
            ordinal=int(first["process_ordinal"]),
            application=application,
            config=first.get("process_config"),
            steps=[
                WorkflowStep.hydrate(row, application=application, approved=approved)
                for row in rows if row.get("workflow_step_id") is not None
            ],
        )
