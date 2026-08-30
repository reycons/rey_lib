"""What this runtime has: its profiles, instructions and provider adapters.

Owned by the ``AI`` instance, not by the module. The old subsystem kept
providers in a process-global dict, so two installations in one process shared
it -- which is exactly the state one canonical object per runtime exists to
prevent.

This is where configuration has been interpreted, once. Nothing above reads
``llm_profiles``, an analysis map or a contract directory; they ask here.
"""

from __future__ import annotations

from rey_lib.ai.capabilities import AICapabilitySet
from rey_lib.ai.errors import AIConfigurationError, AISelectionError
from rey_lib.ai.instructions import AIInstruction
from rey_lib.ai.profiles import AIProfile
from rey_lib.ai.providers.base import AIProvider
from rey_lib.ai.providers.configuration import ConfiguredProvider, ProviderCapabilities

__all__ = ["AIRegistry"]


class AIRegistry:
    """The resolved collection this runtime offers.

    Constructed from explicit resolved inputs. It performs no discovery, reads
    no context and looks nothing up: whoever builds it decided what this
    installation has.
    """

    def __init__(
        self,
        *,
        profiles: tuple[AIProfile, ...] = (),
        instructions: tuple[AIInstruction, ...] = (),
        providers: tuple[AIProvider, ...] = (),
        configured: tuple[ConfiguredProvider, ...] = (),
    ) -> None:
        self._profiles: dict[str, AIProfile] = {}
        for profile in profiles:
            if not profile.id:
                raise AIConfigurationError("An AI profile must have an id.")
            if profile.id in self._profiles:
                raise AIConfigurationError(
                    f"Two AI profiles are configured as '{profile.id}'."
                )
            self._profiles[profile.id] = profile

        self._instructions: dict[str, AIInstruction] = {}
        for instruction in instructions:
            if instruction.id in self._instructions:
                raise AIConfigurationError(
                    f"Two AI instructions are configured as '{instruction.id}'."
                )
            self._instructions[instruction.id] = instruction

        self._configured: dict[str, ConfiguredProvider] = {}
        for configuration in configured:
            if configuration.id in self._configured:
                raise AIConfigurationError(
                    f"Two configured providers share the id '{configuration.id}'."
                )
            self._configured[configuration.id] = configuration

        self._providers: dict[str, AIProvider] = {}
        for provider in providers:
            if provider.name in self._providers:
                raise AIConfigurationError(
                    f"Two AI providers are registered as '{provider.name}'."
                )
            self._providers[provider.name] = provider

    # -- what is available -----------------------------------------------

    def profiles(self) -> tuple[AIProfile, ...]:
        """Every configured profile, in configuration's order."""
        return tuple(self._profiles.values())

    def instructions(self) -> tuple[AIInstruction, ...]:
        """Every configured instruction, in configuration's order."""
        return tuple(self._instructions.values())

    def providers(self) -> tuple[str, ...]:
        """The names of the adapters this runtime can reach."""
        return tuple(self._providers)

    def has_profile(self, profile_id: str) -> bool:
        return str(profile_id or "") in self._profiles

    def has_instruction(self, instruction_id: str) -> bool:
        return str(instruction_id or "") in self._instructions

    # -- what one thing is -----------------------------------------------

    def profile(self, profile_id: str) -> AIProfile:
        """One profile, or a refusal naming what was asked for."""
        found = self._profiles.get(str(profile_id or ""))
        if found is None:
            raise AISelectionError(
                f"No AI profile is configured as '{profile_id or '(none)'}'."
            )
        return found

    def instruction(self, instruction_id: str) -> AIInstruction:
        """One instruction, or a refusal naming what was asked for."""
        found = self._instructions.get(str(instruction_id or ""))
        if found is None:
            raise AISelectionError(
                f"No AI instruction is configured as '{instruction_id or '(none)'}'."
            )
        return found

    def provider(self, name: str) -> AIProvider:
        """The adapter registered under this name.

        Used where execution names a provider directly rather than reaching one
        through a profile -- a fallback transition, which selects from an
        already-resolved sequence and never re-reads configuration.
        """
        found = self._providers.get(name)
        if found is None:
            raise AIConfigurationError(
                f"No AI provider is registered as '{name}' in this runtime."
            )
        return found

    def provider_for(self, profile: AIProfile) -> AIProvider:
        """The adapter that answers for this profile."""
        found = self._providers.get(profile.provider)
        if found is None:
            raise AIConfigurationError(
                f"AI profile '{profile.id}' names provider '{profile.provider}', "
                "which is not registered in this runtime."
            )
        return found

    # -- what an application actually gets -------------------------------

    def configuration(self, configured_provider_id: str) -> ConfiguredProvider:
        """The configured provider registered under this identity."""
        found = self._configured.get(configured_provider_id)
        if found is None:
            raise AIConfigurationError(
                f"No configured provider is registered as "
                f"'{configured_provider_id}' in this runtime."
            )
        return found

    def capabilities_of(self, profile: AIProfile) -> ProviderCapabilities:
        """Layer one: what the configured provider behind this profile can do.

        The adapter is asked rather than configuration being trusted, so a
        profile cannot advertise something its adapter does not implement.
        """
        provider = self.provider_for(profile)
        return ProviderCapabilities(
            configured_provider_id=profile.configured_provider_id or profile.id,
            capability=provider.capability_for(profile.model),
        )

    def effective_capability(self, profile: AIProfile) -> AICapabilitySet:
        """Layer three: the configured capability, narrowed by policy."""
        return profile.effective_capability(self.capabilities_of(profile).capability)
