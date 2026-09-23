"""What the estate measurably is, and how a concluded description is admitted.

Separate from ``rey_lib.repository_map`` on purpose. That package generates the
index -- the evidence. This one admits an AI's *interpretation* of it. The whole
design rests on those being different things, and putting the publisher inside
the index package would collapse the distinction in the code layout while the
documentation still claimed it.

Nothing here concludes anything about capability.
"""

from __future__ import annotations

from rey_lib.system_capability.publication import (
    CapabilityPublisher,
    PublicationError,
    StagedCapability,
    StagedEvidence,
    StagedGeneration,
    generation_from_payload,
)

__all__ = [
    "CapabilityPublisher",
    "PublicationError",
    "StagedCapability",
    "StagedEvidence",
    "StagedGeneration",
    "generation_from_payload",
]
