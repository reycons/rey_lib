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
from rey_lib.ai.errors import AISelectionError

__all__ = ["PROFILE_ACCESS_MODES", "PROFILE_ACCESS_REDACTED",
           "PROFILE_ACCESS_UNREDACTED", "AIProfile"]

#: The two representations a configured model may be permitted to receive.
PROFILE_ACCESS_REDACTED = "redacted"
PROFILE_ACCESS_UNREDACTED = "unredacted"
PROFILE_ACCESS_MODES = frozenset({PROFILE_ACCESS_REDACTED, PROFILE_ACCESS_UNREDACTED})


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
    profile_access:
        The declared policy for which *representation* of a data-profiling
        record this configured model may receive -- redacted or unredacted.
        Consumed from configuration at construction, so nothing re-reads it from
        an application context per request. ``None`` declares none, and asking
        for a policy then refuses rather than assuming one.
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
    profile_access: Any = None
    options: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """What a reader sees, falling back to what addresses it."""
        return self.name or self.id

    def access_policy(self) -> dict[str, Any]:
        """The validated profile-access policy this profile declares.

        Answers "which representation may this model receive", which is
        authorization and therefore not ``profile_library``'s: that owner
        produces a redacted or unredacted representation and deliberately
        applies no authorization, which is why an operator inspecting both in
        the Feeds tree does not depend on this layer.

        The declaration is the only authority. No provider name, endpoint or
        model grants access, and a missing or invalid declaration fails closed.

        Raises:
            AISelectionError: when the declaration is absent or invalid.
        """
        policy = self.profile_access
        allowed_value = (
            policy.get("allowed") if hasattr(policy, "get")
            else getattr(policy, "allowed", None)
        )
        default_value = (
            policy.get("default") if hasattr(policy, "get")
            else getattr(policy, "default", None)
        )
        if not isinstance(allowed_value, (list, tuple)) or not allowed_value:
            raise AISelectionError(
                f"AI profile '{self.id}' requires profile_access.allowed."
            )

        allowed: list[str] = []
        for value in allowed_value:
            mode = str(value or "").strip()
            if mode not in PROFILE_ACCESS_MODES:
                raise AISelectionError(
                    "profile_access.allowed may contain only "
                    f"{' or '.join(sorted(PROFILE_ACCESS_MODES))}."
                )
            if mode not in allowed:
                allowed.append(mode)

        default = str(default_value or "").strip()
        if default not in allowed:
            raise AISelectionError(
                "profile_access.default must be a member of profile_access.allowed."
            )
        return {"allowed": allowed, "default": default}

    def permitted_access(self, requested: str = "") -> str:
        """The access mode this profile permits for a request, or a refusal.

        An empty request takes the declared default. Anything else must be in
        ``allowed``.

        Raises:
            AISelectionError: when the mode is not permitted.
        """
        policy = self.access_policy()
        selected = str(requested or "").strip() or str(policy["default"])
        if selected not in policy["allowed"]:
            raise AISelectionError(
                f"AI profile '{self.id}' does not allow profile_access "
                f"'{selected}'."
            )
        return selected

    def effective_capability(self, provider_capability: AICapabilitySet) -> AICapabilitySet:
        """What an application actually gets: layer three.

        The configured provider's capability, narrowed by this installation's
        policy. Layer one is passed in rather than held, because a profile that
        held it could disagree with the adapter.
        """
        return provider_capability.narrowed_by(self.policy)
