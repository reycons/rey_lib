"""The AI declares what it produces: the artifact envelope on the AI execution path.

A task with no structured output answers ``{"artifact_type", "content", "notes"}``;
rey_lib.artifacts owns the types, their media types, extraction and validation, and
AI turns the envelope into the answer: content as text, the declared type's media
type, the notes beside it. A REJECTED note fails the task; an envelope that cannot
be read goes back to the model for correction before it fails.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from rey_lib.ai import ai as ai_module
from rey_lib.ai.ai import AI
from rey_lib.ai.content import AIInput
from rey_lib.ai.errors import AIOutputError
from rey_lib.ai.requests import AIOutputSpec
from rey_lib.ai.task import AITask
from rey_lib.artifacts.envelope import (
    ARTIFACT_MEDIA_TYPES,
    build_envelope_instruction,
    extract_declared_artifact,
    media_type_for,
    permitted_artifact_types,
)
from rey_lib.artifacts.errors import ArtifactEnvelopeError
from tests.ai_answers import enveloped


def _task(**facts: Any) -> AITask:
    given: dict[str, Any] = {
        "task_key": "explain", "model_identifier": "ollama:qwen3-coder-next:latest",
        "instruction_key": "explain_v1", "contract_body": "Explain it.",
        "input": AIInput.prompt("What is this?"),
    }
    given.update(facts)
    return AITask(**given)


def _ai(monkeypatch: pytest.MonkeyPatch, *answers: str, corrections: int = 0) -> tuple[AI, list]:
    """An AI whose model answers in turn, and what it was sent."""
    sent: list[str] = []
    queue = list(answers)

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        sent.append(str(messages[0].parts[-1].content))
        return ModelResponse(parts=[TextPart(queue.pop(0))])

    monkeypatch.setattr(ai_module, "_MAX_OUTPUT_CORRECTIONS", corrections)
    ai = AI()
    monkeypatch.setattr(ai, "_model", lambda *engine_facts: FunctionModel(answer))
    return ai, sent


class TestTheTypes:
    def test_each_declared_type_is_one_media_type(self) -> None:
        assert ARTIFACT_MEDIA_TYPES == {
            "sql": "text/x-sql", "markdown": "text/markdown", "json": "application/json",
            "text": "text/plain", "python": "text/x-python", "yaml": "application/yaml",
            "html": "text/html", "xml": "application/xml"}
        assert media_type_for("SQL") == "text/x-sql"

    def test_a_type_the_estate_does_not_define_is_no_artifact(self) -> None:
        with pytest.raises(ArtifactEnvelopeError, match="'csv', which is none of"):
            media_type_for("csv")

    def test_the_model_is_asked_to_declare_one_of_them(self) -> None:
        asked = build_envelope_instruction()
        assert all(kind in asked for kind in ARTIFACT_MEDIA_TYPES)
        assert '"artifact_type": "sql"' in build_envelope_instruction("sql")


class TestExtraction:
    def test_the_declared_type_content_and_notes_come_back(self) -> None:
        raw = "```json\n" + enveloped("SELECT 1", "sql", ["one row"]) + "\n```"
        assert extract_declared_artifact(raw) == ("sql", "SELECT 1", ["one row"])

    def test_strict_content_is_validated_as_its_declared_type(self) -> None:
        with pytest.raises(ArtifactEnvelopeError, match="Markdown fencing"):
            extract_declared_artifact(enveloped("```sql\nSELECT 1\n```", "sql"))

    def test_markdown_content_may_carry_fences(self) -> None:
        body = "Run:\n```sql\nSELECT 1\n```"
        assert extract_declared_artifact(enveloped(body, "markdown"))[1] == body

    @pytest.mark.parametrize("raw", ["just prose", '["a list"]', enveloped("x", "csv"),
                                     '{"content": "no type"}'],
                             ids=["not-json", "not-an-object", "unknown-type", "no-type"])
    def test_anything_else_is_refused(self, raw: str) -> None:
        with pytest.raises(ArtifactEnvelopeError):
            extract_declared_artifact(raw)


class TestOnTheExecutionPath:
    def test_the_answer_is_the_declared_artifact_with_its_notes_beside_it(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ai, sent = _ai(monkeypatch, enveloped("# Done", "markdown", ["read 2 tables"]))
        task = _task()

        result = ai.execute_task(task)

        assert (result.text, result.media_type) == ("# Done", "text/markdown")
        assert result.output.notes == ("read 2 tables",)
        assert sent == ["What is this?" + build_envelope_instruction()]
        assert task.status == AITask.COMPLETED

    def test_a_rejected_artifact_fails_the_task_and_is_never_its_answer(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ai, _sent = _ai(monkeypatch, enveloped("", "text", ["REJECTED: not SQL I can comment"]))
        task = _task()

        with pytest.raises(AIOutputError, match="declined: not SQL I can comment"):
            ai.execute_task(task)

        assert (task.status, task.answer) == (AITask.FAILED, None)

    def test_an_unreadable_envelope_is_corrected_before_it_fails(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ai, sent = _ai(monkeypatch, "just prose", enveloped("fixed"), corrections=1)

        result = ai.execute_task(_task())

        # Asked twice: the answer, then the correction it was sent back for.
        assert result.text == "fixed" and len(sent) == 2

    def test_an_envelope_never_corrected_fails_the_task(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ai, _sent = _ai(monkeypatch, "just prose")
        task = _task()

        with pytest.raises(AIOutputError):
            ai.execute_task(task)

        assert task.status == AITask.FAILED

    def test_a_task_with_structured_output_keeps_its_contract_and_no_envelope(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        schema = {"type": "object", "properties": {"answer": {"type": "string"}},
                  "required": ["answer"]}
        ai, sent = _ai(monkeypatch, '{"answer": "ready"}')

        result = ai.execute_task(_task(output=AIOutputSpec.schema_of(schema)))

        assert result.value == {"answer": "ready"}
        assert build_envelope_instruction() not in sent[0]


class TestADestinationsPermittedTypes:
    """Option (a): the destination narrows the envelope; the task's contract is its own."""

    def test_a_destinations_media_types_are_the_types_a_model_may_declare(self) -> None:
        assert permitted_artifact_types(["text/x-sql", "text/x-unknown"]) == ("sql",)
        assert permitted_artifact_types(["text/markdown", "text/x-sql"]) == ("sql", "markdown")

    def test_the_model_is_told_only_the_permitted_types(self) -> None:
        asked = build_envelope_instruction(permitted=("sql",))
        assert "where only sql is accepted" in asked
        assert "markdown, json" not in asked

    def test_a_type_outside_the_permitted_is_refused(self) -> None:
        with pytest.raises(ArtifactEnvelopeError, match="accepts only sql"):
            extract_declared_artifact(enveloped("# Notes", "markdown"), ("sql",))

    def test_a_task_bound_to_sql_is_told_so_and_corrected_into_it(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ai, sent = _ai(monkeypatch, enveloped("# Notes", "markdown"),
                       enveloped("SELECT 1 -- one", "sql"), corrections=1)
        task = _task(artifact_types=("sql",))

        result = ai.execute_task(task)

        assert (result.text, result.media_type) == ("SELECT 1 -- one", "text/x-sql")
        assert sent[0] == "What is this?" + build_envelope_instruction(permitted=("sql",))
        assert task.contract_body == "Explain it."
        tries = result.execution.attempts
        assert [(one.failed, "accepts only sql" in one.error) for one in tries] == [
            (True, True), (False, False)]

    def test_a_structured_task_keeps_its_contract_whatever_is_permitted(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        schema = {"type": "object", "properties": {"answer": {"type": "string"}},
                  "required": ["answer"]}
        ai, sent = _ai(monkeypatch, '{"answer": "ready"}')

        result = ai.execute_task(_task(output=AIOutputSpec.schema_of(schema),
                                       artifact_types=("sql",)))

        assert result.value == {"answer": "ready"}
        assert "artifact_type" not in sent[0]
