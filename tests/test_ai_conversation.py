"""AIConversation (backlog 1338): a subject's AI conversation, kept whole and offered within a limit.

The Control is a stand-in holding conversations and messages in memory, each
message carrying its run's status as f_ai_conversation_message_get reads it from
control.run_manifest. These tests prove the object restores the newest active
conversation, records an exchange against its run without copying anything of
the run or inventing an answer, records a run once, keeps the whole history and
offers the configured most recent part of it, saying what it left out.
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.ai.conversation import (
    AIConversation,
    supported_conversation_roles,
    supported_conversation_statuses,
)
from rey_lib.ai.ai import AI
from rey_lib.ai.errors import AIRequestError

_SUBJECT = {"kind": "workflow", "workflow_id": 8}


class _Control:
    """Conversations, messages, run statuses and task limits, in memory."""

    def __init__(self, run_statuses: dict[int, str] | None = None,
                 limits: dict[str, Any] | None = None) -> None:
        self.conversations: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []
        self.run_statuses = dict(run_statuses or {})
        self.limits = dict(limits or {})
        self.statuses: list[tuple[int, str]] = []

    def ai_configuration(self, installation: str) -> list[dict[str, Any]]:
        return [{"task_key": key, "conversation_message_limit": limit}
                for key, limit in self.limits.items()]

    def ai_conversations(self, **wanted: Any) -> list[dict[str, Any]]:
        return [dict(one) for one in reversed(self.conversations)
                if all(value is None or one[name] == value for name, value in wanted.items())]

    def ai_conversation_messages(self, conversation_id: int) -> list[dict[str, Any]]:
        return [{**one, "run_status": self.run_statuses.get(one["run_id"])}
                for one in self.messages if one["conversation_id"] == conversation_id]

    def create_ai_conversation(self, installation_id: int, application_id: int,
                               subject_identity: dict[str, Any], **_: Any) -> int:
        conversation_id = len(self.conversations) + 1
        self.conversations.append({
            "conversation_id": conversation_id, "installation_id": installation_id,
            "application_id": application_id, "subject_identity": dict(subject_identity),
            "status": "active",
        })
        return conversation_id

    def append_ai_conversation_message(self, conversation_id: int, *, run_id: Any,
                                       role: str, content: str) -> int:
        sequence = 1 + sum(one["conversation_id"] == conversation_id for one in self.messages)
        self.messages.append({"message_id": len(self.messages) + 1,
                              "conversation_id": conversation_id, "run_id": run_id,
                              "sequence": sequence, "role": role, "content": content})
        return len(self.messages)

    def update_ai_conversation_status(self, conversation_id: int, status: str) -> None:
        self.statuses.append((conversation_id, status))


def _restored(control: _Control) -> AIConversation:
    return AIConversation.restore(control, 1, 7, _SUBJECT)


class TestRestoring:
    def test_a_subject_with_no_conversation_gets_an_empty_unsaved_one(self) -> None:
        control = _Control()
        conversation = _restored(control)

        assert (conversation.conversation_id, conversation.messages()) == (None, [])
        assert control.conversations == []

    def test_the_newest_active_conversation_of_the_subject_is_restored(self) -> None:
        control = _Control()
        older = control.create_ai_conversation(1, 7, _SUBJECT)
        newer = control.create_ai_conversation(1, 7, _SUBJECT)
        control.create_ai_conversation(1, 7, {"kind": "workflow", "workflow_id": 9})
        control.append_ai_conversation_message(newer, run_id=1, role="user", content="hi")

        conversation = _restored(control)

        assert conversation.conversation_id == newer != older
        assert [one["content"] for one in conversation.messages()] == ["hi"]


class TestRecording:
    def test_the_first_exchange_begins_the_conversation_with_its_prompt_and_answer(self) -> None:
        control = _Control()
        conversation = _restored(control)

        assert conversation.record_exchange(prompt="Build it", run_id=11, answer="Proposal")

        (saved,) = control.conversations
        assert (saved["installation_id"], saved["application_id"],
                saved["subject_identity"]) == (1, 7, _SUBJECT)
        assert [(one["role"], one["content"], one["run_id"]) for one in control.messages] == [
            ("user", "Build it", 11), ("assistant", "Proposal", 11)]

    def test_a_failed_exchange_keeps_its_prompt_and_run_and_no_answer(self) -> None:
        control = _Control(run_statuses={12: "FAILED"})
        conversation = _restored(control)

        conversation.record_exchange(prompt="Try", run_id=12, answer=None)

        assert [(one["role"], one["run_id"]) for one in control.messages] == [("user", 12)]

    def test_a_run_is_recorded_once(self) -> None:
        control = _Control()
        conversation = _restored(control)

        assert conversation.record_exchange(prompt="a", run_id=11, answer="b")
        assert not conversation.record_exchange(prompt="a", run_id=11, answer="b")
        assert len(control.messages) == 2

    def test_an_exchange_without_a_run_is_refused(self) -> None:
        with pytest.raises(AIRequestError):
            _restored(_Control()).record_exchange(prompt="a", run_id=None, answer=None)

    def test_archiving_a_saved_conversation_sets_its_status(self) -> None:
        control = _Control()
        conversation = _restored(control)
        conversation.record_exchange(prompt="a", run_id=11, answer="b")

        conversation.archive()

        assert control.statuses == [(1, "archived")]


class TestTheContextOffered:
    def test_with_no_limit_the_whole_history_is_offered(self) -> None:
        control = _Control()
        conversation = _restored(control)
        conversation.record_exchange(prompt="one", run_id=1, answer="first")
        conversation.record_exchange(prompt="two", run_id=2, answer="second")

        offered = conversation.context("local", "build_workflow")

        assert (offered["message_count"], offered["message_limit"], offered["omitted"]) == (
            4, None, 0)
        assert [one["content"] for one in offered["messages"]] == [
            "one", "first", "two", "second"]

    def test_a_limit_offers_the_most_recent_and_states_what_was_left_out(self) -> None:
        control = _Control(limits={"build_workflow": 3})
        conversation = _restored(control)
        conversation.record_exchange(prompt="one", run_id=1, answer="first")
        conversation.record_exchange(prompt="two", run_id=2, answer="second")

        offered = conversation.context("local", "build_workflow")

        assert (offered["message_count"], offered["message_limit"], offered["omitted"]) == (
            4, 3, 1)
        assert [one["content"] for one in offered["messages"]] == ["first", "two", "second"]
        assert len(control.messages) == 4

    def test_a_user_message_says_whether_its_run_answered_failed_or_has_not_ended(self) -> None:
        control = _Control(run_statuses={1: "SUCCEEDED", 2: "FAILED", 3: "ABORTED", 4: "RUNNING"})
        conversation = _restored(control)
        conversation.record_exchange(prompt="answered", run_id=1, answer="yes")
        conversation.record_exchange(prompt="failed", run_id=2, answer=None)
        conversation.record_exchange(prompt="aborted", run_id=3, answer=None)
        conversation.record_exchange(prompt="unended", run_id=4, answer=None)

        outcomes = {one["content"]: one.get("outcome")
                    for one in conversation.context("local", "build_workflow")["messages"]}

        assert outcomes == {"answered": "answered", "yes": None, "failed": "failed",
                            "aborted": "failed", "unended": "unanswered"}


def test_the_roles_and_statuses_are_the_tables_own() -> None:
    assert supported_conversation_roles() == ["user", "assistant"]
    assert supported_conversation_statuses() == ["active", "archived"]


class TestTheAIOwnsItsConversations:
    """The live AI holds one conversation per subject, as it is persisted (#1338)."""

    def test_an_existing_conversation_is_restored_once_and_appended_to(self) -> None:
        control = _Control()
        existing = control.create_ai_conversation(1, 7, _SUBJECT)
        control.append_ai_conversation_message(existing, run_id=1, role="user", content="hi")
        ai = AI()

        held = ai.conversation(control, 1, 7, dict(_SUBJECT))
        ai.record_exchange(control, 1, 7, {"workflow_id": 8, "kind": "workflow"},
                           prompt="Again", run_id=2, answer="Done")

        assert held is ai.conversation(control, 1, 7, _SUBJECT)
        assert held.conversation_id == existing and len(control.conversations) == 1
        assert [one["content"] for one in held.messages()] == ["hi", "Again", "Done"]

    def test_two_subjects_and_two_applications_never_share_a_conversation(self) -> None:
        control = _Control()
        ai = AI()

        ai.record_exchange(control, 1, 7, _SUBJECT, prompt="A", run_id=1, answer=None)
        ai.record_exchange(control, 1, 7, {"kind": "workflow", "workflow_id": 9},
                           prompt="B", run_id=2, answer=None)
        ai.record_exchange(control, 1, 5, _SUBJECT, prompt="C", run_id=3, answer=None)

        assert len(control.conversations) == 3
        assert [one["content"] for one in ai.conversation(control, 1, 7, _SUBJECT).messages()
                ] == ["A"]


def test_conversation_projection_keeps_whole_history_and_pending_without_writing() -> None:
    control = _Control({1: "SUCCEEDED", 2: "RUNNING"})
    conversation = _restored(control)
    conversation.record_exchange(prompt="first", run_id=1, answer="answer")
    before = list(control.messages)
    shown = conversation.projection({"prompt": "second", "run_id": 2})
    assert [one["content"] for one in shown["messages"]] == ["first", "answer", "second"]
    assert shown["messages"][-1]["pending"] is True
    assert shown["messages"][1]["media_type"] == "text/plain"
    assert control.messages == before
    # A finished exchange replaces its pending presentation, never duplicates it.
    conversation.record_exchange(prompt="second", run_id=2, answer="done")
    assert len(conversation.projection({"prompt": "second", "run_id": 2})["messages"]) == 4


@pytest.mark.parametrize("media_type,text,language", [
    ("text/x-sql", "SELECT 1", "sql"),
    ("text/x-python", "print('hi')", "python"),
    ("application/yaml", "key: value", "yaml"),
    ("application/xml", "<item/>", "xml"),
    ("text/html", "<p>hello</p>", "html"),
    ("text/plain", "hello", "text"),
])
def test_supplied_artifact_type_is_projected_from_the_shared_contract(media_type, text, language) -> None:
    control = _Control({1: "SUCCEEDED"})
    conversation = _restored(control)
    conversation.record_exchange(prompt="question", run_id=1, answer=text)
    conversation.present_answer(1, {"text": text, "media_type": media_type,
                                    "tool_results": [{"value": "private tool data"}]})
    artifact = conversation.projection()["messages"][-1]["artifact"]
    assert artifact["content"]["language"] == language
    assert artifact["content"]["text"] == text
    assert "tool_results" not in artifact
    assert control.messages[-1]["content"] == text


def test_conversation_renders_markdown_with_the_existing_safe_formatter_and_formats_json() -> None:
    conversation = _restored(_Control({1: "SUCCEEDED", 2: "SUCCEEDED"}))
    conversation.record_exchange(prompt="format", run_id=1, answer="# Hello\n<script>alert(1)</script>")
    conversation.present_answer(1, {"text": "# Hello\n<script>alert(1)</script>", "media_type": "text/markdown"})
    content = conversation.projection()["messages"][-1]["artifact"]["content"]
    assert "<h1>Hello</h1>" in content["html"]
    assert "<script>" not in content["html"]
    assert content["html_trusted"] is True
    conversation.record_exchange(prompt="json", run_id=2, answer='{"a":1}')
    conversation.present_answer(2, {"text": '{"a":1}', "media_type": "application/json",
                                     "value": {"artifact_type": "json", "content": '{"a":1}'}})
    assert conversation.projection()["messages"][-1]["artifact"]["content"]["text"] == '{\n  "a": 1\n}'
