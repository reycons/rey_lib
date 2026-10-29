"""AITask and AIEvent describe themselves as plain data (backlog 1047, D1).

What ai_app forwards over the app channel without reinterpreting it: every
projection is JSON-serialisable as the channel sends it, carries the answer as
AI stated it (text or value with the media type, never rendered), and keeps
execution-only facts -- credentials, the contract body -- out.
"""

from __future__ import annotations

import json

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from rey_lib.ai.ai import AI
from rey_lib.ai.content import AIInput
from rey_lib.ai.requests import AIOutputSpec
from rey_lib.ai.results import AIExecutionInfo, AIOutput, AIResult
from rey_lib.ai.streaming import AIEvent, AIEventKind
from rey_lib.ai.task import AITask
from rey_lib.ai.tools import AIToolCall, AIToolResult


def _task(**facts: object) -> AITask:
    given: dict[str, object] = {
        "task_key": "explain", "label": "Explain", "profile_key": "anthropic",
        "model_identifier": "anthropic:claude-sonnet-4-6",
        "credential_ref": "env.ANTHROPIC_API_KEY", "contract_body": "Secret contract text.",
        "input": AIInput.prompt("What is this?"), "output": AIOutputSpec.markdown(),
        "subject": "SELECT 1", "subject_reference": {"workflow_id": "7"},
        "origin_id": "node-7",
    }
    given.update(facts)
    return AITask(**given)  # type: ignore[arg-type]


class TestTaskProjection:
    def test_an_idle_task_projects_what_it_is_and_was_asked(self) -> None:
        task = _task(choices={"profile_key": "anthropic", "output": AIOutputSpec.markdown()})

        projection = task.projection()

        assert json.loads(json.dumps(projection)) == projection
        assert projection["id"] == task.id
        assert (projection["task_key"], projection["label"]) == ("explain", "Explain")
        assert projection["prompt"] == "What is this?"
        assert projection["media_type"] == "text/markdown"
        assert projection["choices"] == {"profile_key": "anthropic", "output": "text/markdown"}
        assert (projection["subject"], projection["subject_reference"],
                projection["origin_id"]) == ("SELECT 1", {"workflow_id": "7"}, "node-7")
        assert (projection["status"], projection["answer"], projection["error"]) == (
            AITask.IDLE, None, "")

    def test_execution_only_facts_stay_out(self) -> None:
        shown = json.dumps(_task().projection())

        assert "ANTHROPIC_API_KEY" not in shown
        assert "Secret contract text." not in shown

    def test_an_answered_task_projects_its_answer_unrendered(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ai = AI()
        model = FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("# Done")]))
        monkeypatch.setattr(ai, "_model", lambda *engine_facts: model)
        task = _task()

        ai.execute_task(task)
        projection = task.projection()

        assert projection["status"] == AITask.COMPLETED
        assert projection["answer"] == {
            "text": "# Done", "value": None, "media_type": "text/markdown"}
        assert json.loads(json.dumps(projection)) == projection

    def test_a_failed_task_projects_why(self) -> None:
        task = _task()
        task.failed("The engine refused.")

        assert (task.projection()["status"], task.projection()["error"]) == (
            AITask.FAILED, "The engine refused.")


class TestEventProjection:
    def test_each_kind_projects_the_fields_it_carries(self) -> None:
        result = AIResult(
            output=AIOutput.of_value({"answer": "ready"}, text='{"answer": "ready"}',
                                     media_type="application/json"),
            execution=AIExecutionInfo(execution_id="x1"),
        )
        events = [
            AIEvent.started("x1"),
            AIEvent.content("x1", "Hel"),
            AIEvent.tool_requested("x1", AIToolCall(id="c1", name="query",
                                                    arguments={"sql": "SELECT 1"})),
            AIEvent.tool_accepted("x1", AIToolResult(call_id="c1", failed=True,
                                                     message="refused")),
            AIEvent.completed("x1", result),
            AIEvent.failed("x1", "down"),
            AIEvent.cancelled("x1"),
        ]

        projected = [event.projection() for event in events]

        assert json.loads(json.dumps(projected)) == projected
        assert [one["kind"] for one in projected] == [
            AIEventKind.EXECUTION_STARTED.value, AIEventKind.CONTENT_DELTA.value,
            AIEventKind.TOOL_CALL_REQUESTED.value, AIEventKind.TOOL_RESULT_ACCEPTED.value,
            AIEventKind.EXECUTION_COMPLETED.value, AIEventKind.EXECUTION_FAILED.value,
            AIEventKind.EXECUTION_CANCELLED.value,
        ]
        assert {one["execution_id"] for one in projected} == {"x1"}
        assert projected[1]["text"] == "Hel"
        assert projected[2]["tool_call"] == {"id": "c1", "name": "query",
                                             "arguments": {"sql": "SELECT 1"}}
        assert projected[3]["tool_result"] == {"call_id": "c1", "value": None,
                                               "failed": True, "message": "refused"}
        assert projected[4]["result"] == {"text": '{"answer": "ready"}',
                                          "value": {"answer": "ready"},
                                          "media_type": "application/json"}
        assert projected[5]["error"] == "down"
        assert projected[0]["result"] is projected[0]["tool_call"] is None


class TestTaskDefinition:
    """AITask.definition (#1199 S6): its domain read, independent of display."""

    def test_it_states_what_the_task_is_and_where_it_stands(self) -> None:
        task = _task(installation="admin",
                     choices={"profile_key": "anthropic", "output": AIOutputSpec.markdown()})

        definition = task.definition()

        assert json.loads(json.dumps(definition)) == definition
        assert definition == {
            "id": task.id, "task_key": "explain", "label": "Explain", "installation": "admin",
            "profile_key": "anthropic", "instruction_key": "", "temperature": None,
            "representation": "", "connection": "",
            "choices": {"profile_key": "anthropic", "output": "text/markdown"},
            "prompt": "What is this?", "media_type": "text/markdown",
            "subject": "SELECT 1", "subject_reference": {"workflow_id": "7"},
            "origin_id": "node-7", "status": AITask.IDLE, "error": "", "run_id": "",
            "answer": None,
        }

    def test_no_engine_execution_or_interaction_fact_is_carried(self) -> None:
        definition = _task(composed_first=True).definition()
        shown = json.dumps(definition)

        assert not {"composed_first", "model_identifier", "execution_mechanism", "base_url",
                    "credential_ref", "timeout_seconds", "access_allowed", "access_default",
                    "contract_key", "contract_body", "output_schema", "tools"} & set(definition)
        assert "ANTHROPIC_API_KEY" not in shown
        assert "Secret contract text." not in shown

    def test_it_follows_the_lifecycle_the_task_owns(self) -> None:
        task = _task()
        task.failed("The engine refused.")

        assert (task.definition()["status"], task.definition()["error"]) == (
            AITask.FAILED, "The engine refused.")

    def test_projection_is_unchanged_beside_it(self) -> None:
        task = _task(composed_first=True)

        assert task.projection()["composed_first"] is True
        assert "installation" not in task.projection()
