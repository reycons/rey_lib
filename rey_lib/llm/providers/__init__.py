"""
Provider abstraction layer for LLM dispatch.

Application code must never import provider SDKs directly.  All LLM calls
go through a BaseProvider implementation so the provider can be swapped
without touching orchestration logic.

Modules
-------
base        BaseProvider interface, ProviderCapabilities, Message, ProviderResponse.
registry    Module-level provider registry for named provider lookup.
anthropic   Anthropic (Claude) provider implementation.
openai      OpenAI (GPT) provider implementation.
ollama      Ollama local LLM provider implementation.
mock        Deterministic mock provider for testing.
"""

from rey_lib.llm.providers.base import (
    BaseProvider,
    Message,
    ProviderCapabilities,
    ProviderResponse,
)
from rey_lib.llm.providers.registry import get as get_provider
from rey_lib.llm.providers.registry import register as register_provider

__all__ = [
    "BaseProvider",
    "Message",
    "ProviderCapabilities",
    "ProviderResponse",
    "get_provider",
    "register_provider",
]
