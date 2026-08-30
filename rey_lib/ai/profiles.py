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
    configured_provider_id:
        The stable Rey identity of the ``ConfiguredProvider`` this profile
        selects. The reference is the link; the configuration itself -- endpoint,
        timeout, credential reference -- stays internal and never reaches a
        reader through here.
    provider:
        Which provider adapter answers for this profile. Identity, projected for
        a reader; ``ConfiguredProvider`` is the authority.
    model:
        The model identity handed to that provider. Projected for the same
        reason.
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
    configured_provider_id: str = ""
    provider: str = ""
    model: str = ""
    policy: AICapabilitySet | None = None
    options: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """What a reader sees, falling back to what addresses it."""
        return self.name or self.id

    def effective_capability(self, provider_capability: AICapabilitySet) -> AICapabilitySet:
        """What an application actually gets: layer three.

        The configured provider's capability, narrowed by this installation's
        policy. Layer one is passed in rather than held, because a profile that
        held it could disagree with the adapter.
        """
        return provider_capability.narrowed_by(self.policy)
