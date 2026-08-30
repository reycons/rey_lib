"""Conversational continuity, owned here rather than by a provider.

A session holds the turns so far and executes against them. Its identity is
ours: a provider's own conversation or thread id may be an implementation
detail inside an adapter, but it is never what addresses a session, because
that would make the estate's continuity depend on one provider's feature.

Sessions are created by the root and reach the same executor, so a
session-bound execution and a stateless one resolve identically.
"""

from __future__ import annotations

import uuid
from typing import Iterator

from rey_lib.ai.content import AIInput, AIMessage, AIRole
from rey_lib.ai.requests import AIRequest
from rey_lib.ai.results import AIResult
from rey_lib.ai.streaming import AIEvent

__all__ = ["AISession"]


class AISession:
    """One conversation against one runtime.

    Args:
        runtime: The AI this session belongs to. Sessions do not outlive it and
            do not reach another.
        session_id: What addresses this session.
    """

    def __init__(self, runtime, *, session_id: str = "") -> None:
        self._ai = runtime
        self._id = session_id or uuid.uuid4().hex
        self._messages: list[AIMessage] = []

    @property
    def id(self) -> str:
        return self._id

    def messages(self) -> tuple[AIMessage, ...]:
        """The turns so far, oldest first."""
        return tuple(self._messages)

    def execute(self, request: AIRequest) -> AIResult:
        """Run one turn, and keep it.

        The request's own input becomes the next turn, sent after the history.
        What came back is appended, so the following turn sees it.
        """
        combined = self._with_history(request)
        result = self._ai.execute(combined, session_id=self._id)
        self._remember(request, result)
        return result

    def stream(self, request: AIRequest) -> Iterator[AIEvent]:
        """Run one turn, reporting events, and keep it.

        The turn is remembered when the execution completes; an execution that
        failed or was withdrawn leaves the history as it was, because a turn
        that did not happen is not part of the conversation.
        """
        combined = self._with_history(request)
        result: AIResult | None = None
        for event in self._ai.stream(combined, session_id=self._id):
            if event.result is not None:
                result = event.result
            yield event
        if result is not None:
            self._remember(request, result)

    def _with_history(self, request: AIRequest) -> AIRequest:
        """The request as it is sent: history first, then this turn.

        The caller's request is not mutated -- a new one is built, so the value
        the caller holds still says what they asked for.
        """
        import dataclasses  # noqa: PLC0415 -- one use, at the boundary

        turns = tuple(self._messages) + tuple(request.input.messages)
        return dataclasses.replace(
            request,
            input=AIInput(content=request.input.content, messages=turns),
        )

    def _remember(self, request: AIRequest, result: AIResult) -> None:
        """Append what was said and what came back."""
        for message in request.input.messages:
            self._messages.append(message)
        if request.input.content:
            self._messages.append(
                AIMessage(role=AIRole.USER, content=request.input.content),
            )
        if result.output.text:
            from rey_lib.ai.content import text  # noqa: PLC0415

            self._messages.append(
                AIMessage(role=AIRole.ASSISTANT, content=(text(result.output.text),)),
            )
