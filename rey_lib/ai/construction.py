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

from typing import Any, Callable, Mapping

from rey_lib.ai.ai import AI
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
from rey_lib.ai.settings import AISettings
from rey_lib.config.env_reference import declaration_map

__all__ = [
    "ProviderFactory",
    "ai_from_ctx",
    "configured_providers_from_ctx",
    "default_adapters",
]

#: How one configured provider becomes one adapter.
#:
#: Called once, during construction, and it is the last place that could have
#: read ``ctx``. It resolves no credential: the adapter is given the reference
#: and reads the environment when it builds its client.
ProviderFactory = Callable[[ConfiguredProvider], AIProvider]


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


def configured_providers_from_ctx(ctx: Any) -> tuple[ConfiguredProvider, ...]:
    """Normalize ``ctx.llm`` into Rey-owned configured providers.

    Reads only what the AI runtime contract names: provider, model, credential
    reference, and where configuration states them, endpoint, timeout and
    provider options.

    Args:
        ctx: The application context. Treated as opaque and never retained.

    Returns:
        One ``ConfiguredProvider`` per configured instance, keyed by the
        instance name as its stable Rey identity.

    Raises:
        AIConfigurationError: when ``ctx.llm`` is absent, or an instance names
            no provider.
    """
    instances = getattr(ctx, "llm", None)
    if not instances:
        raise AIConfigurationError(
            "ctx.llm is not set, so there is no AI configuration to build from."
        )

    configured: list[ConfiguredProvider] = []
    for name, entry in _entries(instances):
        provider = str(_field(entry, "provider", "") or "").strip()
        if not provider:
            raise AIConfigurationError(
                f"ctx.llm['{name}'] names no provider."
            )
        configured.append(
            ConfiguredProvider(
                id=str(name),
                provider=provider,
                model=str(_field(entry, "model", "") or ""),
                endpoint=str(_field(entry, "endpoint", "") or ""),
                timeout_seconds=_optional_number(_field(entry, "timeout", None)),
                credential_ref=str(_field(entry, "api_key", "") or ""),
                options=dict(_field(entry, "options", {}) or {}),
            ),
        )
    return tuple(configured)


def ai_from_ctx(
    ctx: Any,
    *,
    adapters: Mapping[str, ProviderFactory] | None = None,
    profiles: tuple[AIProfile, ...] = (),
    instructions: tuple[AIInstruction, ...] = (),
    settings: AISettings | None = None,
    policy: AIExecutionPolicy = DEFAULT_EXECUTION_POLICY,
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

    Returns:
        An ``AI`` holding only Rey-owned state.

    Raises:
        AIConfigurationError: when configuration is missing, or names a provider
            no factory can build.
    """
    configured = configured_providers_from_ctx(ctx)
    factories = (
        adapters if adapters is not None
        else default_adapters(credentials_from_ctx(ctx))
    )
    built: list[AIProvider] = []
    for configuration in configured:
        factory = factories.get(configuration.provider)
        if factory is None:
            raise AIConfigurationError(
                f"ctx.llm['{configuration.id}'] names provider "
                f"'{configuration.provider}', which no adapter factory builds."
            )
        built.append(factory(configuration))

    registry = AIRegistry(
        profiles=_linked(profiles, configured),
        instructions=instructions,
        providers=tuple(built),
        configured=configured,
    )
    return AI(registry=registry, settings=settings, policy=policy)


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


def _entries(instances: Any) -> list[tuple[str, Any]]:
    """Every configured instance, as ``(name, entry)``.

    ``ctx.llm`` is the estate's **named collection**: a list of records each
    carrying its own ``name``, which is what ``_alias_named_collection`` produces
    when it aliases ``llm_profiles`` onto the canonical ``llm`` key. That is the
    configured shape, and ``find_in_ctx`` reads every other section the same way.

    A mapping is also accepted, because building a runtime from explicit inputs
    is legitimate and a caller holding a dict should not have to build a list to
    be understood. What is *not* accepted is guessing: an earlier version of this
    walked ``vars()`` when the value was neither, which failed on the only shape
    production actually uses.
    """
    if instances is None:
        return []
    if hasattr(instances, "keys"):
        return [(str(name), instances[name]) for name in instances.keys()]
    if isinstance(instances, (list, tuple)):
        named: list[tuple[str, Any]] = []
        for entry in instances:
            name = _field(entry, "name", "")
            if not str(name or "").strip():
                raise AIConfigurationError(
                    "An AI configuration entry carries no name, so nothing can "
                    "select it."
                )
            named.append((str(name), entry))
        return named
    raise AIConfigurationError(
        "ctx.llm must be the configured named collection -- a list of entries "
        f"each carrying a name -- and this runtime holds {type(instances).__name__}."
    )


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


def _optional_number(value: Any) -> float | None:
    """A configured number, or nothing when configuration stated nothing."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise AIConfigurationError(
            f"A configured AI timeout must be a number, got {value!r}."
        ) from exc
