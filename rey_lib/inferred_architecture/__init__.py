"""How an inferred-architecture generation is admitted, and the map drawn of it.

``ObjectMap`` is the read side: the live map of the estate (and of one
database schema), opened on a governed map subject (#1119).


Separate from ``rey_lib.repository_map``, which generates the index -- the
evidence -- and from ``rey_lib.system_capability``. This admits an AI's
*interpretation* of the evidence as architecture; keeping that apart from the
evidence in the code layout is what keeps the distinction real.

Nothing here infers anything about architecture.
"""

from __future__ import annotations

from rey_lib.inferred_architecture.object_map import (
    CLAIM_NODE_TYPE,
    OBJECT_PROJECTION,
    SCHEMA_PROJECTION,
    ObjectMap,
)
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
    "CLAIM_NODE_TYPE",
    "OBJECT_PROJECTION",
    "SCHEMA_PROJECTION",
    "ObjectMap",
    "ArchitecturePublisher",
    "PublicationError",
    "StagedArea",
    "StagedEvidence",
    "StagedGeneration",
    "StagedMembership",
    "generation_from_payload",
]
