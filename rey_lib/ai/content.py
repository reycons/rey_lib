"""What an AI task sends.

    AIInput     the task's content parts
    AIContent   one part: text, structured data, an image, a document, audio

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
class AIInput:
    """What a task is sending: its content parts."""

    content: tuple[AIContent, ...] = ()

    @staticmethod
    def of(*parts: AIContent) -> "AIInput":
        """An input that is just its parts."""
        return AIInput(content=tuple(parts))

    @staticmethod
    def prompt(value: str) -> "AIInput":
        """The simple case, spelled out rather than made the canonical shape."""
        return AIInput(content=(text(value),))

    def is_empty(self) -> bool:
        """Whether there is anything at all to send."""
        return not self.content

    def kinds(self) -> frozenset[AIContentKind]:
        """Every part kind present, which is what capability is checked against."""
        return frozenset(part.kind for part in self.content)
