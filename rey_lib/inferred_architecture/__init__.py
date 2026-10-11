"""How an inferred-architecture generation is admitted.

Separate from ``rey_lib.repository_map``, which generates the index -- the
evidence -- and from ``rey_lib.system_capability``. This admits an AI's
*interpretation* of the evidence as architecture; keeping that apart from the
evidence in the code layout is what keeps the distinction real.

Nothing here infers anything about architecture.
"""

from __future__ import annotations

from rey_lib.inferred_architecture.publication import (
    ArchitecturePublisher,
    PublicationError,
    StagedArea,
    StagedEvidence,
    StagedGeneration,
    StagedMembership,
    generation_from_payload,
)

__all__ = [
    "ArchitecturePublisher",
    "PublicationError",
    "StagedArea",
    "StagedEvidence",
    "StagedGeneration",
    "StagedMembership",
    "generation_from_payload",
]
