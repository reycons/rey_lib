"""Tests for WorkflowProcess, the instantiated Application (backlog 954).

A process holds the existing Application, its own configuration as governed,
and its ordered WorkflowStep children, each already bound by hydration. It
refuses a step whose operation its Application does not publish, and no two
processes share configuration.
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.config.applications import Application, ApplicationOperation
from rey_lib.workflow import WorkflowError
from rey_lib.workflow.workflow_process import WorkflowProcess
from rey_lib.workflow.workflow_step import WorkflowStep

_SCHEMA = "https://json-schema.org/draft/2020-12/schema"


def _operation(name: str) -> ApplicationOperation:
    """An operation accepting no settings."""
    return ApplicationOperation(
        name=name,
        contract_definition={"$schema": _SCHEMA, "type": "object", "additionalProperties": False},
    )


def _handler(*_args: Any) -> None:
    return None


def _step(application: Application, operation: str, step_id: str, ordinal: int) -> WorkflowStep:
    """A step bound to ``operation`` of ``application``."""
    return WorkflowStep(
        id=step_id, process_name=operation, label=step_id, ordinal=ordinal,
        application=application, operation_name=operation,
        approved={operation: _handler}, settings={},
    )


def test_it_holds_its_application_configuration_and_steps_in_order() -> None:
    application = Application(name="app", workflow_operations=(_operation("a"), _operation("b")))
    steps = [_step(application, "b", "s2", 2), _step(application, "a", "s1", 1)]

    process = WorkflowProcess(
        name="app", ordinal=1, application=application, config={"paths": []}, steps=steps,
    )

    assert process.application is application
    assert process.config == {"paths": []}
    assert [one.id for one in process.steps] == ["s2", "s1"]
    assert all(one is given for one, given in zip(process.steps, steps))


def test_a_null_configuration_stays_null_and_an_empty_one_stays_empty() -> None:
    application = Application(name="app", workflow_operations=(_operation("a"),))

    absent = WorkflowProcess(name="app", ordinal=1, application=application,
                             config=None, steps=[])
    empty = WorkflowProcess(name="app", ordinal=2, application=application,
                            config={}, steps=[])

    assert absent.config is None
    assert empty.config == {}


def test_two_instances_of_one_application_do_not_share_configuration() -> None:
    application = Application(name="app", workflow_operations=(_operation("a"),))
    governed = {"app": {"repositories": ["rey_lib"]}}

    first = WorkflowProcess(name="app", ordinal=1, application=application,
                            config=governed, steps=[])
    second = WorkflowProcess(name="app", ordinal=2, application=application,
                             config=governed, steps=[])
    first.config["app"]["repositories"].append("rey_loader")

    assert second.config == {"app": {"repositories": ["rey_lib"]}}
    assert governed == {"app": {"repositories": ["rey_lib"]}}
    assert first.application is second.application


def test_a_step_of_another_application_is_refused() -> None:
    application = Application(name="app", workflow_operations=(_operation("a"),))
    elsewhere = Application(name="elsewhere", workflow_operations=(_operation("a"),))

    with pytest.raises(WorkflowError, match="does not publish"):
        WorkflowProcess(
            name="app", ordinal=1, application=application, config=None,
            steps=[_step(elsewhere, "a", "s1", 1)],
        )


def test_a_process_without_a_name_is_refused() -> None:
    application = Application(name="app")

    with pytest.raises(WorkflowError, match="missing required 'name'"):
        WorkflowProcess(name="", ordinal=1, application=application, config=None, steps=[])
