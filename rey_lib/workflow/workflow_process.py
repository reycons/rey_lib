"""WorkflowProcess -- one Application instantiated in a workflow.

The middle of the workflow object model (backlog 950, slice 954). A process is
a configured instance of an existing Application: it holds that Application,
its own application configuration as governed for this instance, and its
ordered WorkflowStep children. Two processes may instantiate the same
Application with different configuration.

It owns population state, not catalog discovery. The workflow hydration object
materialises the graph from the governed rows (control.workflow_flat_vw) and
the approved implementation catalog its caller injects -- as ManifestSource
takes its reader and QuerySource its adapter -- and each WorkflowStep arrives
already bound to its ApplicationOperation and approved implementation. This
object neither obtains nor builds a catalog.

Public API
----------
WorkflowProcess    A configured Application instance and its ordered steps.
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, Mapping, Optional, Sequence

from rey_lib.config.applications import Application
from rey_lib.workflow.workflow_step import WorkflowError, WorkflowStep

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
            The process's steps, each already bound by hydration.

        Raises
        ------
        WorkflowError
            When a step instantiates an operation this process's Application
            does not publish.
        """
        if not name:
            raise WorkflowError("a workflow process is missing required 'name'.")
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
                raise WorkflowError(
                    f"process '{name}': step '{step.id}' instantiates operation "
                    f"'{step.operation.name}', which application "
                    f"'{application.name}' does not publish."
                )
        self.steps: tuple[WorkflowStep, ...] = tuple(steps)
        self.workflow: Optional["Workflow"] = None
