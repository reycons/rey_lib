"""AITask: fixed governed facts, and a lifecycle only its own transitions change.

Backlog 1021 (ai.redesign). No database, no hydration, no execution: the task is
built directly, as hydration (backlog 1022) will build it.
"""

from __future__ import annotations

import dataclasses

import pytest

from rey_lib.ai.content import AIInput
from rey_lib.ai.errors import AIConfigurationError
from rey_lib.ai.instructions import AIInstructionKind
from rey_lib.ai.requests import AIOutputSpec
from rey_lib.ai.task import AITask


def _task(**facts: object) -> AITask:
    """A task as hydration would settle it."""
    given = {
        "task_key": "explain", "installation": "local", "label": "Explain",
        "profile_key": "local_testing",
        "model_identifier": "ollama:qwen3-coder-next:latest",
        "base_url": "http://localhost:11434/v1",
        "access_allowed": ("redacted",), "access_default": "redacted",
        "instruction_key": "explain_markdown",
        "instruction_kind": AIInstructionKind.CONTRACT,
        "contract_key": "explain", "contract_version": "1", "contract_body": "Explain it.",
        "temperature": 0.0, "representation": "redacted", "connection": "code_index",
        "input": AIInput.prompt("What is this?"), "output": AIOutputSpec.markdown(),
        "subject": "SELECT 1", "subject_id": "node:42", "subject_kind": "sql",
        "origin_id": "node:41",
    }
    given.update(facts)
    return AITask(**given)


class TestTheFacts:
    def test_it_carries_what_hydration_settled(self) -> None:
        task = _task()

        assert task.model_identifier == "ollama:qwen3-coder-next:latest"
        assert task.base_url == "http://localhost:11434/v1"
        assert task.instruction_kind is AIInstructionKind.CONTRACT
        assert task.contract_body == "Explain it."
        assert task.connection == "code_index"
        assert task.input.is_empty() is False

    def test_caller_identity_is_preserved_as_given(self) -> None:
        task = _task()

        assert (task.subject_id, task.subject_kind, task.origin_id) == (
            "node:42", "sql", "node:41",
        )

    def test_a_fact_cannot_be_assigned(self) -> None:
        task = _task()

        with pytest.raises(dataclasses.FrozenInstanceError):
            task.model_identifier = "openai-chat:gpt-4o"  # type: ignore[misc]

    def test_a_different_choice_is_a_different_task(self) -> None:
        """No setter for governed facts: another engine means building again."""
        assert not any(name.startswith(("with_", "set_")) for name in dir(AITask))

    def test_a_task_that_names_no_engine_is_refused(self) -> None:
        with pytest.raises(AIConfigurationError, match="names no engine"):
            _task(model_identifier=" ")

    def test_each_task_is_its_own_unit_of_work(self) -> None:
        assert _task().id != _task().id


class TestTheLifecycle:
    def test_a_new_task_is_idle(self) -> None:
        task = _task()

        assert (task.status, task.answer, task.error, task.run_id) == (
            AITask.IDLE, None, "", "",
        )

    def test_started_clears_the_previous_outcome(self) -> None:
        task = _task()
        task.failed("boom")

        task.started("run-7")

        assert (task.status, task.answer, task.error, task.run_id) == (
            AITask.RUNNING, None, "", "run-7",
        )

    def test_answered_carries_what_came_back(self) -> None:
        task = _task()
        task.started("run-7")

        task.answered({"text": "ready"})

        assert task.status == AITask.COMPLETED
        assert task.answer == {"text": "ready"}
        assert task.run_id == "run-7"

    def test_failed_carries_why(self) -> None:
        task = _task()
        task.started()

        task.failed("engine unreachable")

        assert (task.status, task.answer, task.error) == (
            AITask.FAILED, None, "engine unreachable",
        )

    def test_cancelled_is_neither_failure_nor_answer(self) -> None:
        task = _task()
        task.started()

        task.cancelled()

        assert (task.status, task.answer, task.error) == (AITask.CANCELLED, None, "")

    def test_recording_the_run_keeps_an_answer_that_already_arrived(self) -> None:
        task = _task()
        task.started()
        task.answered("ready")

        task.record_run("run-8")

        assert (task.status, task.answer, task.run_id) == (AITask.COMPLETED, "ready", "run-8")

    def test_the_lifecycle_cannot_be_assigned_from_outside(self) -> None:
        task = _task()

        with pytest.raises(dataclasses.FrozenInstanceError):
            task.status = AITask.COMPLETED  # type: ignore[misc]

    def test_the_lifecycle_writer_never_writes_a_fact(self) -> None:
        task = _task()

        with pytest.raises(AttributeError, match="facts are fixed"):
            task._become(model_identifier="openai-chat:gpt-4o")

    def test_transitions_leave_the_facts_untouched(self) -> None:
        task = _task()
        before = {f.name: getattr(task, f.name) for f in dataclasses.fields(task) if f.init}

        task.started("run-1")
        task.answered("ready")

        after = {f.name: getattr(task, f.name) for f in dataclasses.fields(task) if f.init}
        assert after == before
