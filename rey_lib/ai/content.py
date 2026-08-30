"""What a request is made of.

Content is content. A message sequence is not a kind of content -- it is how an
input is organised -- so the two are separate and the model does not nest into
itself:

    AIInput     direct content, or messages
    AIMessage   a role, its content parts, and metadata
    AIContent   one part: text, structured data, an image, a document, audio

That separation is why a multimodal request needs no new object and why adding a
part kind later changes nothing above it. A canonical API of
``AIRequest(prompt: str)`` was rejected for the same reason: it is obsolete the
first time an image is sent.

Only the part kinds with established use are constructible today. The rest exist
as a named kind so a provider adapter can refuse them precisely rather than a
caller discovering the limit at the provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "AIContent",
    "AIContentKind",
    "AIInput",
    "AIMessage",
    "AIRole",
    "audio",
    "document",
    "image",
    "structured",
    "text",
]


class AIContentKind(str, Enum):
    """What one part of an input is."""

    TEXT = "text"
    STRUCTURED = "structured"
    IMAGE = "image"
    DOCUMENT = "document"
    AUDIO = "audio"


class AIRole(str, Enum):
    """Who is speaking.

    Three, because three are established. A role is added when something needs
    it, not in anticipation.
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True)
class AIContent:
    """One part of an input.

    ``value`` is the part's own payload: the string for text, the object for
    structured data, the reference for an image, document or audio. Nothing here
    reads it -- what a part means is the provider adapter's to translate and the
    capability model's to permit.
    """

    kind: AIContentKind
    value: Any
    media_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def text(value: str) -> AIContent:
    """One text part."""
    return AIContent(kind=AIContentKind.TEXT, value=str(value))


def structured(value: Any) -> AIContent:
    """One structured part, carried as the value it is."""
    return AIContent(kind=AIContentKind.STRUCTURED, value=value)


def image(reference: str, media_type: str = "") -> AIContent:
    """One image, by reference. The bytes are not this model's to hold."""
    return AIContent(kind=AIContentKind.IMAGE, value=reference, media_type=media_type)


def document(reference: str, media_type: str = "") -> AIContent:
    """One document, by reference."""
    return AIContent(kind=AIContentKind.DOCUMENT, value=reference, media_type=media_type)


def audio(reference: str, media_type: str = "") -> AIContent:
    """One audio input, by reference."""
    return AIContent(kind=AIContentKind.AUDIO, value=reference, media_type=media_type)


@dataclass(frozen=True)
class AIMessage:
    """One turn: who said it, what it was made of, and anything about it."""

    role: AIRole
    content: tuple[AIContent, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def of(role: AIRole, *parts: AIContent) -> "AIMessage":
        """One message from its parts."""
        return AIMessage(role=role, content=tuple(parts))


@dataclass(frozen=True)
class AIInput:
    """What the caller is sending.

    Either loose content or a sequence of messages. Both are allowed together --
    a conversation with a fresh attachment is one request, not two -- and the
    order is messages first, then the loose content as the final turn.
    """

    content: tuple[AIContent, ...] = ()
    messages: tuple[AIMessage, ...] = ()

    @staticmethod
    def of(*parts: AIContent) -> "AIInput":
        """An input that is just its parts."""
        return AIInput(content=tuple(parts))

    @staticmethod
    def prompt(value: str) -> "AIInput":
        """The simple case, spelled out rather than made the canonical shape."""
        return AIInput(content=(text(value),))

    @staticmethod
    def conversation(*messages: AIMessage) -> "AIInput":
        """An input that is a sequence of turns."""
        return AIInput(messages=tuple(messages))

    def is_empty(self) -> bool:
        """Whether there is anything at all to send."""
        return not self.content and not self.messages

    def kinds(self) -> frozenset[AIContentKind]:
        """Every part kind present, which is what capability is checked against."""
        present = {part.kind for part in self.content}
        for message in self.messages:
            present.update(part.kind for part in message.content)
        return frozenset(present)
