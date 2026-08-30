"""Configured provider state, and what that configuration can do.

Two internal Rey-owned objects, separated because configuration and capability
are not the same question:

    ConfiguredProvider    how a configured provider/model is identified and reached
    ProviderCapabilities  what that configured provider/model can do

Neither is the public profile. ``AIProfile`` remains the public selection
projection and references these by stable Rey identity, so the profile a Console
renders never carries an endpoint, a credential or capability truth.

The credential boundary is stated once, here, because two objects otherwise
appear to own it:

    ConfiguredProvider  configuration describing *how to authenticate* --
                        a credential reference, never resolved material
    ProviderAdapter     the resolved authentication and client material *used
                        to perform calls*

So a configured provider may say ``credential_ref = "env.ANTHROPIC_API_KEY"``.
It never holds the instantiated client. The adapter receives this at
construction, resolves the reference as it needs to, and owns what results.

The hard boundary that follows: **no credential discovery occurs during
execution.** References are consumed at adapter construction, and no adapter
reads ``ctx``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rey_lib.ai.capabilities import AICapabilitySet

__all__ = ["ConfiguredProvider", "ProviderCapabilities"]


@dataclass(frozen=True)
class ConfiguredProvider:
    """One configured provider/model, as an installation reached it.

    Immutable configured connection facts, normalized out of installation
    configuration at construction. Nothing here is discovered later.

    Attributes
    ----------
    id:
        Stable Rey identity. What an ``AIProfile`` references, so the public
        projection selects configuration without carrying it.
    provider:
        Which adapter answers for this configuration.
    model:
        The model identity handed to that adapter.
    endpoint:
        Where the provider is reached, when configuration states one. Empty
        means the adapter's own default.
    timeout_seconds:
        How long one call may take. ``None`` is the adapter's own default.
    credential_ref:
        A *reference* to a credential, never the credential. The adapter
        resolves it at construction.
    options:
        Provider-specific configuration this adapter understands. Unlike
        ``AIRequestOptions``, which is cross-provider by definition, this is
        where a single provider's own settings legitimately live.
    """

    id: str
    provider: str = ""
    model: str = ""
    endpoint: str = ""
    timeout_seconds: float | None = None
    credential_ref: str = ""
    options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("A ConfiguredProvider must have an id.")


@dataclass(frozen=True)
class ProviderCapabilities:
    """What one configured provider/model can do.

    Layer one of the capability model, bound to the configuration it describes
    rather than inferred from a provider or model name. That inference is what
    let the old subsystem advertise a capability nothing implemented.
    """

    configured_provider_id: str
    capability: AICapabilitySet = field(default_factory=AICapabilitySet)

    def __post_init__(self) -> None:
        if not self.configured_provider_id:
            raise ValueError(
                "ProviderCapabilities must name the configured provider it describes."
            )
