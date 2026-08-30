"""What an application can actually do, and the three layers behind it.

The distinction the old subsystem did not draw, and the reason a caller could
only guess from a provider or model name:

    provider capability   what the model can do
    profile policy        what this installation permits
    effective capability  what an application actually gets

Only the third is exposed. ``AI.capabilities(profile_id)`` answers it, so nothing
downstream infers behaviour from a name, and a request needing something the
effective set lacks is refused before a provider is reached -- the ordering the
old runner established and the one worth keeping.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from rey_lib.ai.content import AIContentKind

__all__ = ["AICapability", "AICapabilitySet", "capabilities_for_content"]


class AICapability(str, Enum):
    """One thing an AI capability may or may not be able to do.

    Every member is checkable against a real request. A capability nothing can
    ask for is not listed.
    """

    TEXT = "text"
    STRUCTURED_OUTPUT = "structured_output"
    STREAMING = "streaming"
    TOOLS = "tools"
    VISION = "vision"
    DOCUMENTS = "documents"
    AUDIO = "audio"


#: Which capability each kind of content part requires.
#:
#: Text and structured data are the same capability on the way in -- a
#: structured input is rendered for the provider, and it is the *output*
#: requirement that needs STRUCTURED_OUTPUT.
_REQUIRED_BY_KIND: dict[AIContentKind, AICapability] = {
    AIContentKind.TEXT: AICapability.TEXT,
    AIContentKind.STRUCTURED: AICapability.TEXT,
    AIContentKind.IMAGE: AICapability.VISION,
    AIContentKind.DOCUMENT: AICapability.DOCUMENTS,
    AIContentKind.AUDIO: AICapability.AUDIO,
}


def capabilities_for_content(kinds: frozenset[AIContentKind]) -> frozenset[AICapability]:
    """What an input made of these part kinds requires."""
    return frozenset(_REQUIRED_BY_KIND[kind] for kind in kinds if kind in _REQUIRED_BY_KIND)


@dataclass(frozen=True)
class AICapabilitySet:
    """A set of capabilities, and the operations the layers need.

    A value rather than a bare frozenset so the three layers combine in one
    stated way: policy narrows provider capability, and never widens it. An
    installation cannot grant what the model cannot do.
    """

    members: frozenset[AICapability] = frozenset()

    @staticmethod
    def of(*members: AICapability) -> "AICapabilitySet":
        return AICapabilitySet(members=frozenset(members))

    def has(self, capability: AICapability) -> bool:
        return capability in self.members

    def missing(self, required: frozenset[AICapability]) -> frozenset[AICapability]:
        """What is required and not present."""
        return frozenset(required) - self.members

    def narrowed_by(self, policy: "AICapabilitySet | None") -> "AICapabilitySet":
        """This capability, restricted by an installation's policy.

        No policy is no restriction. A policy naming something the provider
        cannot do is ignored rather than honoured: the intersection is the
        answer, because a permission to do the impossible is not a capability.
        """
        if policy is None:
            return self
        return AICapabilitySet(members=self.members & policy.members)

    def __iter__(self):
        return iter(sorted(self.members, key=lambda member: member.value))

    def __len__(self) -> int:
        return len(self.members)
