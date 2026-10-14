"""Tests for WorkflowStep, the configured ApplicationOperation (backlog 950).

Binding happens once, at construction, against the application's published
operations and its approved implementation catalog. The step holds its
resolved settings (deep-copied) and declarations and reviews them against its
operation's contract; it holds no execution state -- its child Run executes it.
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.config.applications import Application, ApplicationOperation
from rey_lib.errors.error_utils import AppError
from rey_lib.workflow import workflow_step
from rey_lib.workflow.workflow_step import WorkflowStep

_SCHEMA = "https://json-schema.org/draft/2020-12/schema"


def _operation(name: str, definition: dict[str, Any] | None = None) -> ApplicationOperation:
    """An operation publishing ``definition``, or one accepting no settings."""
    return ApplicationOperation(
        name=name,
        contract_definition=definition or {
            "$schema": _SCHEMA, "type": "object", "additionalProperties": False,
        },
    )


def _application(*operations: ApplicationOperation, name: str = "fixture_app") -> Application:
    """An application publishing ``operations``."""
    return Application(name=name, workflow_operations=operations)


def _handler(*_args: Any) -> None:
    return None


def _step(*, application: Application | None = None,
          settings: dict[str, Any] | None = None) -> WorkflowStep:
    """Step s1 ("One") instantiating p1."""
    return WorkflowStep(
        id="s1", process_name="p1", label="One", ordinal=1,
        application=application or _application(_operation("p1")),
        operation_name="p1", approved={"p1": _handler}, settings=settings or {},
    )


# --- Binding ----------------------------------------------------------------


def test_binding_holds_the_published_operation_and_approved_implementation() -> None:
    operation = _operation("p1")
    step = _step(application=_application(operation))

    assert step.operation is operation
    assert step.implementation is _handler
    assert step.process is None


def test_binding_refuses_an_operation_the_application_does_not_publish() -> None:
    with pytest.raises(AppError, match="does not publish"):
        _step(application=_application(_operation("other")))


def test_binding_refuses_a_published_operation_not_in_the_approved_catalog() -> None:
    with pytest.raises(AppError, match="approved implementation catalog"):
        WorkflowStep(
            id="s1", process_name="p1", label="One", ordinal=1,
            application=_application(_operation("p1")),
            operation_name="p1", approved={}, settings={},
        )


def test_a_step_without_an_id_is_refused() -> None:
    with pytest.raises(AppError, match="missing required 'id'"):
        WorkflowStep(
            id="", process_name="p1", label="", ordinal=1,
            application=_application(_operation("p1")),
            operation_name="p1", approved={"p1": _handler}, settings={},
        )


# --- Settings and contract ---------------------------------------------------


def test_nested_settings_are_isolated_from_the_caller() -> None:
    accepts = _operation("p1", {
        "$schema": _SCHEMA, "type": "object", "properties": {"source": {}},
        "additionalProperties": False,
    })
    given = {"source": {"names": ["a"]}}
    first = _step(application=_application(accepts), settings=given)
    second = _step(application=_application(accepts), settings=given)

    first.settings["source"]["names"].append("changed")

    assert given == {"source": {"names": ["a"]}}
    assert second.settings == {"source": {"names": ["a"]}}


def test_the_contract_refusal_names_the_workflow_process_and_implementation() -> None:
    required = _operation("p1", {
        "$schema": _SCHEMA, "type": "object",
        "properties": {"source": {"type": "string"}}, "required": ["source"],
    })

    refusal = _step(application=_application(required)).contract_refusal("wf")

    assert refusal.startswith(
        "workflow 'wf': process 'p1' settings do not satisfy the contract of "
        "implementation 'p1': "
    )


def test_settings_that_satisfy_the_contract_are_not_refused() -> None:
    assert _step().contract_refusal("wf") == ""


def test_unsupported_settings_are_warned_about_and_not_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    step = _step(settings={"stray": 1})

    with caplog.at_level("WARNING"):
        refusal = step.contract_refusal("wf")

    assert refusal == ""
    assert any("does not support" in record.getMessage() for record in caplog.records)


# --- Superseded types --------------------------------------------------------


@pytest.mark.parametrize("name", [
    "StepOutcome", "RunContext", "WorkflowError", "ProcessHandler",
])
def test_the_superseded_types_do_not_exist(name: str) -> None:
    assert not hasattr(workflow_step, name)


@pytest.mark.parametrize("name", ["execute", "run", "reset", "settle", "outcome"])
def test_the_step_holds_no_execution_api_or_state(name: str) -> None:
    assert not hasattr(_step(), name)
