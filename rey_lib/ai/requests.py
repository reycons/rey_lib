"""What shape an AI task's answer must take.

``AIOutputSpec`` is the output requirement a governed AITask carries; ``AIOutputKind``
names the structure, and ``media_type`` the representation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

__all__ = ["AIOutputKind", "AIOutputSpec"]


class AIOutputKind(str, Enum):
    """What shape the caller needs back."""

    TEXT = "text"
    JSON = "json"
    SCHEMA = "schema"
    CONTRACT = "contract"


@dataclass(frozen=True)
class AIOutputSpec:
    """The caller's output requirement.

    First-class, because structured output added afterwards to a text-shaped API
    is how structured results end up stringified. ``CONTRACT`` defers the shape
    to the resolved instruction, which is the only one that knows it.

    ``media_type`` states the *representation* of the payload, separately from
    its structure. Empty means unstated.
    """

    kind: AIOutputKind = AIOutputKind.TEXT
    schema: dict[str, Any] | None = None
    media_type: str = ""

    @staticmethod
    def text(media_type: str = "") -> "AIOutputSpec":
        return AIOutputSpec(media_type=media_type)

    @staticmethod
    def markdown() -> "AIOutputSpec":
        """Text whose canonical consumer representation is Markdown.

        Deliberately **not** a new ``AIOutputKind``. TEXT versus STRUCTURED is a
        structural distinction; Markdown versus plain text is a representation
        one, and putting both in one enum would conflate serialization structure
        with presentation format. The estate already expresses "same structure,
        different content format" as a media type -- ``AIArtifact.media_type``
        -- and this reuses that vocabulary.
        """
        return AIOutputSpec(media_type="text/markdown")

    @staticmethod
    def json() -> "AIOutputSpec":
        return AIOutputSpec(kind=AIOutputKind.JSON)

    @staticmethod
    def schema_of(schema: dict[str, Any]) -> "AIOutputSpec":
        return AIOutputSpec(kind=AIOutputKind.SCHEMA, schema=schema)

    @staticmethod
    def from_contract() -> "AIOutputSpec":
        return AIOutputSpec(kind=AIOutputKind.CONTRACT)

    def is_structured(self) -> bool:
        return self.kind is not AIOutputKind.TEXT
