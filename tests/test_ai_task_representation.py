"""AI states its result's representation and renders nothing (backlog 1040).

The new execution path's contract, pinned before the legacy one is removed: AI
answers with the text or structured value and the media type its task asked for.
Markdown -> HTML is presentation's, so a result from ``execute_task`` never
carries a rendering -- not in what is returned, and not in the completed event.
Deterministic: the model AI builds is Pydantic AI's FunctionModel.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from rey_lib.ai.ai import AI
from tests.ai_answers import enveloped
from rey_lib.ai.content import AIInput
from rey_lib.ai.requests import AIOutputKind, AIOutputSpec
from rey_lib.ai.results import AIOutputForm
from rey_lib.ai.streaming import AIEvent, AIEventKind
from rey_lib.ai.task import AITask

MARKDOWN = "# Findings\n\n| table | rows |\n|---|---|\n| orders | 12 |\n\n*Done.*"

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


def _task(output: AIOutputSpec) -> AITask:
    return AITask(task_key="explain", profile_key="local_testing",
                  model_identifier="ollama:qwen3-coder-next:latest",
                  input=AIInput.prompt("What is this?"), output=output)


def _ai(model: FunctionModel, monkeypatch: pytest.MonkeyPatch) -> AI:
    ai = AI()
    monkeypatch.setattr(ai, "_model", lambda *engine_facts: model)
    return ai


def _answering(text: str, json_schema: bool = False) -> FunctionModel:
    """A model answering ``text`` as it is: an envelope, or structured JSON."""
    return FunctionModel(
        lambda messages, info: ModelResponse(parts=[TextPart(text)]),
        profile={"supports_json_schema_output": json_schema},
    )


class TestMarkdown:
    def test_an_answer_declared_markdown_is_markdown_and_no_rendering(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = _ai(_answering(enveloped(MARKDOWN, "markdown")), monkeypatch).execute_task(
            _task(AIOutputSpec.text()),
        )

        assert result.output.form is AIOutputForm.TEXT
        assert result.text == MARKDOWN
        assert result.media_type == "text/markdown"
        assert result.html == ""

    def test_the_task_does_not_decide_what_its_answer_is(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A task asking for Markdown answered as SQL is SQL: the model declares it."""
        result = _ai(_answering(enveloped("SELECT 1", "sql")), monkeypatch).execute_task(
            _task(AIOutputSpec.markdown()),
        )

        assert (result.text, result.media_type, result.html) == ("SELECT 1", "text/x-sql", "")


class TestStructured:
    @pytest.mark.parametrize("json_schema", [True, False], ids=["native", "prompted"])
    def test_a_structured_task_answers_its_value_and_no_rendering(
        self, monkeypatch: pytest.MonkeyPatch, json_schema: bool,
    ) -> None:
        result = _ai(_answering('{"answer": "ready"}', json_schema), monkeypatch).execute_task(
            _task(AIOutputSpec.schema_of(SCHEMA)),
        )

        assert result.output.form is AIOutputForm.STRUCTURED
        assert result.value == {"answer": "ready"}
        assert result.html == ""

    def test_a_structured_task_carries_the_media_type_it_asked_for(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        spec = AIOutputSpec(kind=AIOutputKind.SCHEMA, schema=SCHEMA,
                            media_type="application/json")

        result = _ai(_answering('{"answer": "ready"}'), monkeypatch).execute_task(_task(spec))

        assert (result.value, result.media_type, result.html) == (
            {"answer": "ready"}, "application/json", "")


class TestCompletedEvent:
    def test_the_completed_event_carries_the_same_unrendered_result(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
            answer = enveloped(MARKDOWN, "markdown")
            for at in range(0, len(answer), 20):
                yield answer[at:at + 20]

        events: list[AIEvent] = []
        task = _task(AIOutputSpec.markdown())

        result = _ai(FunctionModel(stream_function=stream), monkeypatch).execute_task(
            task, observe=events.append,
        )

        (completed,) = [e for e in events if e.kind is AIEventKind.EXECUTION_COMPLETED]
        assert completed.result is result
        assert (completed.result.text, completed.result.media_type, completed.result.html) == (
            MARKDOWN, "text/markdown", "")
        # The envelope is never told in part: the artifact arrives complete.
        assert [e for e in events if e.kind is AIEventKind.CONTENT_DELTA] == []
        assert task.answer is result

    def test_nothing_rendered_is_ever_told(self, monkeypatch: pytest.MonkeyPatch) -> None:
        events: list[AIEvent] = []

        async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
            yield enveloped(MARKDOWN, "markdown")

        _ai(FunctionModel(stream_function=stream), monkeypatch).execute_task(
            _task(AIOutputSpec.markdown()), observe=events.append,
        )

        told: list[Any] = [e.text for e in events] + [
            e.result.html for e in events if e.result is not None]
        assert not any("<h1" in str(value) or "<table" in str(value) for value in told)
