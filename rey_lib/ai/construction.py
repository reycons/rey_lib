"""The one place installation context is allowed to be read.

``ctx`` is a bootstrap and discovery input. It is not AI runtime state:

    ctx -> construction adapter -> ConfiguredProvider[] + adapters
                                -> AIRegistry -> AI
                                -> the ctx reference is discarded

Everything above this module operates on Rey-owned state alone. Nothing in
``rey_lib.ai`` retains a reference to ``ctx``, no execution path re-enters it to
discover a provider, model, profile, capability, endpoint, credential, timeout
or output setting, no adapter receives it, and no continuation re-resolves
configuration from it.

This module exists so that boundary is a *place* rather than a convention. When
the whole subsystem contained no ``ctx`` and no seam either, the invariant held
by accident; the first application to wire it up is what would have broken it.

**Only fields the AI runtime contract names are carried forward.** The `llm`
config subtree is read and normalized, never retained as an opaque stand-in for
``ctx`` -- keeping the subtree would reintroduce ambient lookup under a new name.

**Adapters are built here, credentials are not resolved here.** A factory turns
one ``ConfiguredProvider`` into one ``AIProvider``, handing it the credential
*reference* and a resolver. The environment is read later, at each point of use,
so nothing resolved is carried out of this module and no cleartext credential
becomes adapter state.

Isolated construction from explicit resolved inputs remains possible without any
application context: ``AI(registry=...)`` needs nothing here.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Mapping

from rey_lib.ai.ai import AI
from rey_lib.ai.contracts import ContractResolver
from rey_lib.ai.credentials import CredentialResolver
from rey_lib.ai.errors import AIConfigurationError
from rey_lib.ai.instructions import AIInstruction
from rey_lib.ai.policies import DEFAULT_EXECUTION_POLICY, AIExecutionPolicy
from rey_lib.ai.profiles import AIProfile
from rey_lib.ai.providers import (
    AnthropicProvider,
    EchoProvider,
    GeminiProvider,
    OllamaProvider,
    OpenAIProvider,
)
from rey_lib.ai.providers.base import AIProvider
from rey_lib.ai.providers.configuration import ConfiguredProvider
from rey_lib.ai.registry import AIRegistry
from rey_lib.ai.settings import AISettings, AISettingsTask
from rey_lib.config.env_reference import declaration_map

__all__ = [
    "ProviderFactory",
    "ai_from_ctx",
    "default_adapters",
    "DEFAULT_TASK_NAME",
]

#: How one configured provider becomes one adapter.
#:
#: Called once, during construction, and it is the last place that could have
#: read ``ctx``. It resolves no credential: the adapter is given the reference
#: and reads the environment when it builds its client.
ProviderFactory = Callable[[ConfiguredProvider], AIProvider]

#: What addresses the defaults rather than a task. Reserved, because the same
#: word names the block the tasks sit beside, and a surface offering a scope per
#: task needs one stable name for "no task".
DEFAULT_TASK_NAME = "default"


def default_adapters(credentials: CredentialResolver) -> dict[str, ProviderFactory]:
    """The adapters this library ships, each bound to one credential resolver.

    A function rather than a module-level dict. The old subsystem kept providers
    in a process-global map that two installations in one process then shared;
    this builds a fresh mapping per runtime, so nothing is shared and nothing
    accumulates across constructions.

    Adding a provider is adding a line here and implementing ``AIProvider`` --
    never a branch anywhere else.
    """
    return {
        "anthropic": lambda configured: AnthropicProvider(configured, credentials),
        "openai": lambda configured: OpenAIProvider(configured, credentials),
        "gemini": lambda configured: GeminiProvider(configured, credentials),
        "ollama": lambda configured: OllamaProvider(configured, credentials),
        "echo": lambda configured: EchoProvider(name=configured.id),
    }


def credentials_from_ctx(ctx: Any) -> CredentialResolver:
    """The credential resolver for this runtime.

    Takes the ``env`` declaration block -- plain configuration naming variables,
    holding no values -- and binds it. The environment itself is read later, at
    each point of use, so nothing resolved is carried out of this function.
    """
    return CredentialResolver(declaration_map(getattr(ctx, "env", None)))


def ai_from_ctx(
    ctx: Any,
    *,
    adapters: Mapping[str, ProviderFactory] | None = None,
    profiles: tuple[AIProfile, ...] = (),
    instructions: tuple[AIInstruction, ...] = (),
    settings: AISettings | None = None,
    policy: AIExecutionPolicy = DEFAULT_EXECUTION_POLICY,
    configured: tuple[ConfiguredProvider, ...] = (),
) -> AI:
    """Build one runtime's ``AI`` from installation context, then let ctx go.

    Args:
        ctx: The application context. Read here and nowhere else, and not
            retained by anything this returns.
        adapters: A factory per provider name. Absent, the adapters this
            library ships are used, bound to this runtime's credential resolver.
        profiles: The public selection projections this runtime offers. A
            profile naming no ``configured_provider_id`` is taken to select the
            configured provider sharing its own id.
        instructions: The instructions this runtime offers.
        settings: The starting selection, validated on construction.
        policy: The control domains for this runtime.
        configured: The engines this runtime may reach, already read. Supplied
            like profiles, instructions and settings are, so this opens no
            configuration source of its own.

    Returns:
        An ``AI`` holding only Rey-owned state.

    Raises:
        AIConfigurationError: when configuration is missing, or names a provider
            no factory can build.
    """
    factories = (
        adapters if adapters is not None
        else default_adapters(credentials_from_ctx(ctx))
    )
    built: list[AIProvider] = []
    for configuration in configured:
        factory = factories.get(configuration.provider)
        if factory is None:
            raise AIConfigurationError(
                f"Engine profile '{configuration.id}' names provider "
                f"'{configuration.provider}', which no adapter factory builds."
            )
        built.append(factory(configuration))

    registry = AIRegistry(
        profiles=_linked(profiles, configured),
        instructions=instructions,
        providers=tuple(built),
        configured=configured,
    )
    return AI(
        registry=registry,
        settings=settings,
        policy=policy,
        contracts=ContractResolver(loader=_contract_loader),
    )


def _contract_loader(reference: str) -> str:
    """One configured contract's body, read through the canonical file reader.

    ``rey_lib.ai`` opens no path of its own -- "where contracts live is
    configuration's answer" -- so the loader is supplied here, at the one place
    an AI is built, and reads exactly the reference configuration declared.

    Without it a CONTRACT instruction carries a reference and no text, and
    refuses at execution. That made the Instruction setting unusable: every
    instruction this runtime offers is a reference, so none could run.
    """
    from rey_lib.files import read_text_file

    return read_text_file(reference)


def _linked(
    profiles: tuple[AIProfile, ...], configured: tuple[ConfiguredProvider, ...],
) -> tuple[AIProfile, ...]:
    """Profiles with their configured-provider reference and projection filled.

    A profile states which configuration it selects; the provider and model it
    shows a reader are projected from that configuration rather than stated
    twice. Where a profile already names both, it is left alone -- an explicitly
    configured projection is not something to overwrite.
    """
    by_id = {configuration.id: configuration for configuration in configured}
    linked: list[AIProfile] = []
    for profile in profiles:
        reference = profile.configured_provider_id or profile.id
        configuration = by_id.get(reference)
        if configuration is None:
            linked.append(profile)
            continue
        linked.append(
            AIProfile(
                id=profile.id,
                name=profile.name,
                configured_provider_id=configuration.id,
                provider=profile.provider or configuration.provider,
                model=profile.model or configuration.model,
                policy=profile.policy,
                profile_access=profile.profile_access,
                options=profile.options,
                metadata=profile.metadata,
            ),
        )
    return tuple(linked)


def _field(entry: Any, name: str, default: Any) -> Any:
    """One configured field, whether the entry is a Namespace or a mapping.

    Both shapes reach here: configuration finalizes into Namespaces, and a
    caller constructing a runtime directly may hand plain dictionaries.
    """
    if entry is None:
        return default
    if hasattr(entry, "get"):
        value = entry.get(name, default)
    else:
        value = getattr(entry, name, default)
    return default if value is None else value


