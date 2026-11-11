"""AIConversation: a durable AI conversation about one subject (backlog 1338).

Three things are kept apart:

- **the conversation** -- this object: its identity, its whole message history,
  its persistence through Control, its status, and the history it offers an AI
  task as context;
- **the session** -- which conversation is active, held by the application that
  owns the subject (the Workflow);
- **the run** -- one AI task execution, recorded by the run log under its own
  run. A message references its run by ``run_id``; nothing of the run -- its
  package, tool calls, tool results -- is copied here.

Persistence is automatic: every exchange that reached a run is recorded. A
completed one is its prompt and its answer; a failed one is its prompt and its
run, with no answer invented for it. Whether an unanswered prompt failed or is
still unanswered is read from its run's own status.

The whole history is kept. What an AI task is given is chosen here, by the
task's configured ``conversation_message_limit``: its most recent messages, with
what was left out stated; with no limit configured, all of them. Nothing is
dropped without saying so, and nothing is deleted from storage.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Mapping, Optional

from rey_lib.ai.errors import AIRequestError
from rey_lib.run.state import status_of
from rey_lib.artifacts import ARTIFACT_MEDIA_TYPES
from rey_lib.formatting import markdown_to_html

__all__ = ["AIConversation", "supported_conversation_roles", "supported_conversation_statuses"]

#: Who a message is from, as ai.ai_conversation_message.role constrains it.
_ROLES = ("user", "assistant")
_USER, _ASSISTANT = _ROLES

#: What a conversation may be, as ai.ai_conversation.status constrains it.
_STATUSES = ("active", "archived")
_ACTIVE, _ARCHIVED = _STATUSES

#: The run states (rey_lib.run.state.SUPPORTED_STATES) in which a run ended without
#: an answer: its prompt is a failed exchange, not an unanswered one.
_ENDED_UNANSWERED = frozenset({"failed", "aborted"})

#: What became of a user message, as the context states it.
_ANSWERED, _FAILED, _UNANSWERED = "answered", "failed", "unanswered"


def supported_conversation_roles() -> list[str]:
    """Every role a conversation message may have."""
    return list(_ROLES)


def supported_conversation_statuses() -> list[str]:
    """Every status a conversation may have."""
    return list(_STATUSES)


class AIConversation:
    """One AI conversation about one subject: its history, held and persisted.

    Not saved until its first exchange is recorded: a conversation restored for
    a subject that has none yet is empty and has no id, and the first recorded
    exchange begins it in the database.

    Args:
        control: The Control its persistence goes through.
        installation_id: The installation it belongs to.
        application_id: The application that owns it (code.application).
        subject_identity: The subject's canonical identity.
        conversation_id: Its id once saved; None before.
        messages: Its history in order, as ``f_ai_conversation_message_get``
            returns it.
    """

    def __init__(self, control: Any, installation_id: int, application_id: int,
                 subject_identity: Mapping[str, Any], *,
                 conversation_id: Optional[int] = None,
                 messages: Optional[list[Mapping[str, Any]]] = None) -> None:
        self._control = control
        self.installation_id = int(installation_id)
        self.application_id = int(application_id)
        self.subject_identity: dict[str, Any] = dict(subject_identity)
        self.conversation_id: Optional[int] = (
            None if conversation_id is None else int(conversation_id))
        self._messages: list[dict[str, Any]] = [dict(one) for one in (messages or [])]
        # The runs already recorded, so an ended run told twice is recorded once.
        self._recorded: set[int] = {
            int(one["run_id"]) for one in self._messages if one.get("run_id") is not None
        }
        self._guard = threading.Lock()
        # Typed answer projections supplied by the live task. Stored messages
        # remain unchanged; restored text without a supplied type stays text.
        self._presentations: dict[int, dict[str, Any]] = {}

    @classmethod
    def restore(cls, control: Any, installation_id: int, application_id: int,
                subject_identity: Mapping[str, Any]) -> "AIConversation":
        """The subject's most recently updated active conversation, or a new one.

        Several conversations per subject are allowed; the one restored is the
        newest active one of this installation, application and subject. With
        none, the conversation returned is empty and unsaved.

        Raises:
            AppError: As Control raises, when the conversation cannot be read.
        """
        rows = control.ai_conversations(
            installation_id=int(installation_id), application_id=int(application_id),
            subject_identity=dict(subject_identity), status=_ACTIVE,
        )
        if not rows:
            return cls(control, installation_id, application_id, subject_identity)
        chosen = rows[0]
        conversation_id = int(chosen["conversation_id"])
        return cls(control, installation_id, application_id, subject_identity,
                   conversation_id=conversation_id,
                   messages=control.ai_conversation_messages(conversation_id))

    def messages(self) -> list[dict[str, Any]]:
        """The whole history in order, as copies."""
        with self._guard:
            return [dict(one) for one in self._messages]

    def present_answer(self, run_id: int, answer: Mapping[str, Any]) -> None:
        """Retain the live task's canonical artifact projection for its exchange."""
        artifact = {key: answer[key] for key in ("media_type", "text", "value", "content", "notes")
                    if key in answer}
        media_type = str(answer.get("media_type") or "text/plain")
        language = next((kind for kind, media in ARTIFACT_MEDIA_TYPES.items()
                         if media == media_type), "text")
        content = dict(answer.get("content") or {})
        text = content.get("text", answer.get("text"))
        if language == "json":
            try:
                value = json.loads(text) if isinstance(text, str) else answer.get("value")
                text = json.dumps(value,
                                  indent=2, ensure_ascii=False)
            except (ValueError, TypeError):
                pass
        content.update(text=str(text or ""), language=language)
        if language == "markdown":
            content.update(html=markdown_to_html(str(text or "")), html_trusted=True)
        else:
            content.update(html="", html_trusted=False)
        artifact["content"] = content
        with self._guard:
            self._presentations[int(run_id)] = artifact

    def projection(self, pending: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """The whole conversation, with owner-supplied pending execution if any.

        This is presentation of the canonical history, never the task-context
        window. Historical text without artifact metadata is explicitly plain.
        """
        with self._guard:
            history = [dict(one) for one in self._messages]
            presentations = dict(self._presentations)
        messages = []
        for message in history:
            shown = {**message, "media_type": "text/plain"}
            if message.get("role") == _USER:
                shown["outcome"] = _outcome(message, history)
            elif message.get("run_id") in presentations:
                shown["artifact"] = presentations[message["run_id"]]
                shown["media_type"] = shown["artifact"].get("media_type") or "text/plain"
            messages.append(shown)
        if pending and not any(one.get("run_id") == pending.get("run_id")
                               for one in history if pending.get("run_id") is not None):
            messages.append({"role": _USER, "content": str(pending.get("prompt") or ""),
                             "media_type": "text/plain", "pending": True,
                             "run_id": pending.get("run_id")})
        return {"conversation_id": self.conversation_id, "messages": messages}

    def record_exchange(self, *, prompt: str, run_id: Optional[int],
                        answer: Optional[str]) -> bool:
        """Record one exchange that reached a run; whether anything was recorded.

        The prompt is always recorded with its run; the answer only when there
        is one -- a failed run's exchange has none, and none is made up. A run
        already recorded is not recorded again. The first exchange begins the
        conversation in the database.

        Raises:
            AIRequestError: When there is no run to record the exchange against.
            AppError: As Control raises, when it cannot be written.
        """
        if run_id is None:
            raise AIRequestError(
                "An AI exchange is recorded against the run it reached; none was given.")
        run = int(run_id)
        with self._guard:
            if run in self._recorded:
                return False
            if self.conversation_id is None:
                self.conversation_id = int(self._control.create_ai_conversation(
                    self.installation_id, self.application_id, self.subject_identity))
            self._control.append_ai_conversation_message(
                self.conversation_id, run_id=run, role=_USER, content=str(prompt or ""))
            if answer is not None:
                self._control.append_ai_conversation_message(
                    self.conversation_id, run_id=run, role=_ASSISTANT, content=str(answer))
            self._recorded.add(run)
            # Read back, so each message carries its run's status as recorded.
            self._messages = [
                dict(one) for one in self._control.ai_conversation_messages(self.conversation_id)
            ]
            return True

    def archive(self) -> None:
        """Archive this conversation: kept, no longer restored or appended to."""
        with self._guard:
            if self.conversation_id is None:
                return
            self._control.update_ai_conversation_status(self.conversation_id, _ARCHIVED)

    def context(self, installation: str, task_key: str) -> dict[str, Any]:
        """The history offered to one AI task, under that task's configured limit.

        The limit is the task's ``conversation_message_limit`` in this
        installation's AI configuration: its most recent messages are offered,
        and how many were left out is stated. With none configured, every
        message is offered.

        Args:
            installation: The installation whose AI configuration is read.
            task_key: The AI task the history is offered to.
        """
        limit = self._message_limit(installation, task_key)
        with self._guard:
            history = [dict(one) for one in self._messages]
        offered = history if limit is None else history[-limit:]
        return {
            "conversation_id": self.conversation_id,
            "message_count": len(history),
            "message_limit": limit,
            "omitted": len(history) - len(offered),
            "messages": [_offered(one, history) for one in offered],
        }

    def _message_limit(self, installation: str, task_key: str) -> Optional[int]:
        """The task's configured history limit, or None for no explicit limit."""
        for row in self._control.ai_configuration(installation):
            if str(row.get("task_key") or "") == str(task_key):
                value = row.get("conversation_message_limit")
                return None if value is None else int(value)
        return None


def _offered(message: Mapping[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    """One message as the context offers it: a user message says what became of it."""
    offered = {
        "sequence": message.get("sequence"),
        "role": message.get("role"),
        "content": message.get("content"),
        "run_id": message.get("run_id"),
    }
    if message.get("role") == _USER:
        offered["outcome"] = _outcome(message, history)
    return offered


def _outcome(message: Mapping[str, Any], history: list[dict[str, Any]]) -> str:
    """Answered when its run gave an answer; failed when its run ended without one."""
    run_id = message.get("run_id")
    answered = any(
        one.get("role") == _ASSISTANT and one.get("run_id") == run_id
        for one in history
    )
    if answered:
        return _ANSWERED
    status = status_of(str(message.get("run_status") or ""))
    return _FAILED if status in _ENDED_UNANSWERED else _UNANSWERED
