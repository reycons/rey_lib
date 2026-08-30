"""One configured, selectable AI execution capability.

A profile is not a provider client and not a live model. It is the configured
choice an installation offers: which provider and model, under what policy, and
what that leaves an application able to do.

It carries its own capability rather than letting a caller infer one from the
provider or model name. That inference is what made the old subsystem's
``supports_tools`` advertise something nothing implemented, and it is the single
thing most likely to force a redesign later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rey_lib.ai.capabilities import AICapabilitySet

__all__ = ["AIProfile"]


@dataclass(frozen=True)
class AIProfile:
    """One configured execution capability, as this runtime offers it.

    Attributes
    ----------
    id:
        What the profile is addressed by. Configuration's own name.
    name:
        What a reader sees. Never what addresses it.
    provider:
        Which provider adapter answers for this profile.
    model:
        The model identity handed to that provider.
    provider_capability:
        What the provider and model can do. Layer one, reported by the adapter
        or declared by configuration.
    policy:
        What this installation permits, where it restricts anything. Layer two.
        ``None`` restricts nothing.
    options:
        Provider-independent execution defaults this profile carries, such as a
        temperature or a token ceiling. Not a bag of provider parameters.
    metadata:
        Presentation-safe facts about the profile. Carried, never read here.
    """

    id: str
    name: str = ""
    provider: str = ""
    model: str = ""
    provider_capability: AICapabilitySet = field(default_factory=AICapabilitySet)
    policy: AICapabilitySet | None = None
    options: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """What a reader sees, falling back to what addresses it."""
        return self.name or self.id

    def effective_capability(self) -> AICapabilitySet:
        """What an application actually gets: layer three.

        Provider capability narrowed by installation policy. This is the only
        answer that leaves the subsystem; the two layers behind it are how it
        was arrived at, not something a consumer reasons about.
        """
        return self.provider_capability.narrowed_by(self.policy)
