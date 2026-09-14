"""One AI invocation, seeded from a subject and thereafter its own.

The thing a reader composes before anything is sent. It holds what this
particular ask is -- its subject, its task, and whatever it overrides locally --
and nothing about how an execution runs.

    seed once from the source; after that this object owns the invocation.

That rule is the whole reason it exists. A surface that re-read its source on
every redraw would change what a reader was composing underneath them, and a
reader who selects something else in the tree they launched from is starting a
different ask, not editing this one.

**It resolves nothing.** The overrides it carries are *overrides*: empty means
"whatever the task or the defaults say", which is exactly what ``AIRequest``
already means by them. So the precedence

    this request's override  ->  the task's override  ->  the default

stays in the one place that implements it, and this object cannot disagree with
the runtime about which engine an ask will reach.

**It executes nothing.** ``AIExecutor`` owns an execution lifecycle; this holds
the last one's outcome so a surface can draw it, and hands its request over to
be run again. Two runs of one object are two executions of the same ask.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from rey_lib.ai.content import AIInput, text
from rey_lib.ai.requests import AIOutputSpec, AIRequest, AIRequestOptions

__all__ = ["AIObject", "AIObjectStatus"]


class AIObjectStatus:
    """What an object's last execution is doing. Words, not behaviour."""

    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class AIObject:
    """One composed ask.

    Attributes:
        id: What addresses it. Minted here, because nothing outside has a name
            for an ask that has not run.
        subject: What is being asked about, already resolved and trusted by
            whoever created it. Never re-read.
        subject_kind: What the subject is, for a surface deciding how to draw
            it. Opaque here -- this object does not interpret a subject.
        subject_id: How whoever created this ask addresses its subject, if it
            has an identity apart from its text. Opaque, carried and never
            read: it lets the creator find the subject again -- to offer a
            capability over it, say -- without this object learning what kind
            of thing it is.
        task: Which configured task this ask is. It is the task that executes.
        prompt: What the reader typed, if anything.
        profile_id, instruction_id, temperature, representation: **overrides**.
            Empty or None means inherit, exactly as everywhere else.
        status: the last execution's state.
        answer: what came back, as the runtime answered it.
        error: why the last execution failed, when it did.
    """

    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    subject: str = ""
    subject_kind: str = ""
    subject_id: str = ""
    #: How the caller addressed the subject this ask was **created from**.
    #: Unchanged when the subject moves, so a surface can still say where the
    #: ask began -- which is a different question from what it is about now.
    origin_id: str = ""
    task: str = ""
    prompt: str = ""
    profile_id: str = ""
    instruction_id: str = ""
    temperature: float | None = None
    representation: str = ""
    #: The connection this ask queries through, or empty to take the task's.
    #: An override like the others: a name, never a handle.
    connection: str = ""
    status: str = AIObjectStatus.IDLE
    #: The execution now under way, as whoever launched it addresses one.
    #: Carried so a surface can watch it; this object neither creates nor
    #: interprets it, and `AIExecutor` still owns the lifecycle.
    run_id: str = ""
    answer: Any = None
    error: str = ""

    def with_settings(self, **changes: Any) -> "AIObject":
        """This object with some of its local choices changed.

        Only the fields a reader composes. Naming them rather than accepting
        anything keeps a caller from writing `status` or `id` through the same
        door a prompt change uses.
        """
        allowed = {
            "prompt", "task", "profile_id", "instruction_id",
            "temperature", "representation", "connection",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(
                f"An AI object has no composable setting named: {', '.join(sorted(unknown))}"
            )
        return replace(self, **changes)

    def about(self, subject: str, subject_id: str = "") -> "AIObject":
        """The same ask, now about something else.

        Deliberately not one of the composable settings: a subject is not a
        preference a reader edits, it is **resolved** by whoever can vouch for
        it and handed over. Keeping it off `with_settings` means a surface
        cannot write one through the same door a prompt change uses.

        The previous outcome goes with it. An answer about the old subject
        displayed beside the new one would read as an answer about the new one.

        Args:
            subject: The new subject, already resolved and trusted by the
                caller. This object does not check it.
            subject_id: How the caller addresses it, if it has an identity
                apart from its text.

        Returns:
            The ask, about that.
        """
        # `origin_id` is deliberately not touched: where the ask began does
        # not move when what it is about does.
        return replace(
            self, subject=subject, subject_id=subject_id,
            status=AIObjectStatus.IDLE, answer=None, error="",
        )

    def started(self, run_id: str = "") -> "AIObject":
        """Running, with the previous outcome cleared.

        The outcome goes with the run that produced it: leaving it would let a
        surface draw the last answer beside a running state and read as though
        the new one had already arrived.

        Args:
            run_id: How the execution now under way is addressed, so a surface
                can watch it.
        """
        return replace(
            self, status=AIObjectStatus.RUNNING, answer=None, error="",
            run_id=str(run_id or ""),
        )

    def with_run(self, run_id: str) -> "AIObject":
        """The same ask, now naming the execution that is watching it.

        **Only the id.** Deliberately not `started`, which also clears the
        previous outcome: an execution can reach its end before whoever
        launched it has finished writing down its name, and a call that reset
        the status here would erase the answer that had already arrived.

        So this is safe to apply to whatever the ask is by then -- running,
        answered or failed -- because it changes none of that.
        """
        return replace(self, run_id=str(run_id or ""))

    def answered(self, answer: Any) -> "AIObject":
        """Completed, carrying what came back."""
        return replace(self, status=AIObjectStatus.COMPLETED, answer=answer, error="")

    def failed(self, message: str) -> "AIObject":
        """Failed, carrying why."""
        return replace(
            self, status=AIObjectStatus.FAILED, answer=None, error=str(message),
        )

    def cancelled(self) -> "AIObject":
        """Cancelled. Not a failure, and not an answer."""
        return replace(self, status=AIObjectStatus.CANCELLED, answer=None, error="")

    def request(self) -> AIRequest:
        """The request this ask currently stands for.

        Composed from this object alone, so what runs is what the reader is
        looking at. The subject and the prompt are both sent because both are
        what was asked -- a prompt about a subject is not a prompt instead of
        one -- and either being empty is legitimate.
        """
        parts = [part for part in (self.subject, self.prompt) if part.strip()]
        return AIRequest(
            input=AIInput(content=(text("\n\n".join(parts)),)),
            task=self.task,
            profile_id=self.profile_id,
            instruction_id=self.instruction_id,
            output=AIOutputSpec.markdown(),
            options=AIRequestOptions(temperature=self.temperature),
        )
