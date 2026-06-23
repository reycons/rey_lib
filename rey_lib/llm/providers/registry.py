"""
Provider registry for named provider lookup.

The registry maps logical provider names to BaseProvider instances.
Providers are registered by name and resolved at runtime so the rest of
the framework never needs to import SDK-specific modules directly.

Built-in provider names are 'anthropic', 'openai', and 'mock'.  Custom
providers can be registered by the caller before the first run.

Public API
----------
register(name, provider)
    Register a BaseProvider instance under a logical name.
get(name, api_key)
    Return the BaseProvider for the given name.
resolve(name, api_key)
    Resolve and return a provider, constructing built-ins on demand.
"""

from __future__ import annotations

from typing import Any, Optional

from rey_lib.llm.exceptions import ConfigurationFailure
from rey_lib.llm.providers.base import BaseProvider
from rey_lib.logs.log_utils import get_logger

__all__ = ["register", "get", "resolve"]

_logger = get_logger(__name__)

# Maps logical name → registered provider instance.
_registry: dict[str, BaseProvider] = {}


def register(name: str, provider: BaseProvider) -> None:
    """Register a provider instance under a logical name.

    Parameters
    ----------
    name : str
        Logical name (e.g. 'anthropic', 'my_custom_provider').
    provider : BaseProvider
        Fully initialised provider instance.
    """
    _registry[name.lower()] = provider
    _logger.debug("provider registered: %s → %s", name, type(provider).__name__)


def get(name: str) -> BaseProvider:
    """Return a previously registered provider by name.

    Parameters
    ----------
    name : str
        Logical provider name.

    Returns
    -------
    BaseProvider

    Raises
    ------
    ConfigurationFailure
        If no provider is registered under that name.
    """
    provider = _registry.get(name.lower())
    if provider is None:
        raise ConfigurationFailure(
            f"No provider registered under '{name}'. "
            "Register one with registry.register() or use resolve()."
        )
    return provider


def resolve(
    name:     str,
    api_key:  str                       = "",
    endpoint: str                       = "",
    timeout:  int                       = 0,
    options:  Optional[dict[str, Any]]  = None,
) -> BaseProvider:
    """Return or construct a provider by logical name.

    Checks the registry first.  If not found, constructs a built-in
    provider (anthropic, openai, ollama, mock) using the supplied api_key.

    Parameters
    ----------
    name : str
        Logical provider name.
    api_key : str
        API key for built-in providers.  Ignored for 'mock' and 'ollama'.
    endpoint : str
        Endpoint URL override.  Only used for 'ollama'; ignored otherwise.
    timeout : int
        HTTP timeout in seconds.  Only used for 'ollama'; 0 uses the default.
    options : dict, optional
        Provider options sourced from the LLM profile (endpoint,
        timeout_seconds, capability flags).  Only used for 'ollama'.

    Returns
    -------
    BaseProvider

    Raises
    ------
    ConfigurationFailure
        If the name is not registered and not a known built-in.
    """
    normalised = name.lower()

    if normalised in _registry:
        return _registry[normalised]

    if normalised == "anthropic":
        from rey_lib.llm.providers.anthropic import AnthropicProvider  # noqa: PLC0415
        return AnthropicProvider(api_key=api_key)

    if normalised in ("openai", "chatgpt"):
        from rey_lib.llm.providers.openai import OpenAIProvider  # noqa: PLC0415
        return OpenAIProvider(api_key=api_key)

    if normalised == "mock":
        from rey_lib.llm.providers.mock import MockProvider  # noqa: PLC0415
        return MockProvider()

    if normalised == "ollama":
        from rey_lib.llm.providers.ollama import OllamaProvider  # noqa: PLC0415

        opts: dict[str, Any] = dict(options or {})
        if endpoint and "endpoint" not in opts:
            opts["endpoint"] = endpoint
        if timeout and "timeout_seconds" not in opts:
            opts["timeout_seconds"] = timeout

        provider = OllamaProvider.from_options(opts)
        provider.health_check()
        return provider

    raise ConfigurationFailure(
        f"Unknown provider '{name}'. "
        "Known built-ins: anthropic, openai, ollama, mock. "
        "Register custom providers with registry.register() before use."
    )
