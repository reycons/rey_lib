"""Provider adapters, and the boundary they implement.

Adapters are held by a runtime's own registry. Nothing here is registered in a
module-level map: the old subsystem's process-global provider dict was shared by
every installation in the process, which is the state this subsystem does not
have.
"""

from rey_lib.ai.providers.anthropic_provider import AnthropicProvider
from rey_lib.ai.providers.base import AIProvider, ProviderCall, ProviderReply
from rey_lib.ai.providers.configuration import ConfiguredProvider, ProviderCapabilities
from rey_lib.ai.providers.echo_provider import EchoProvider
from rey_lib.ai.providers.gemini_provider import GeminiProvider
from rey_lib.ai.providers.ollama_provider import OllamaProvider
from rey_lib.ai.providers.openai_provider import OpenAIProvider

__all__ = [
    "AIProvider",
    "AnthropicProvider",
    "ConfiguredProvider",
    "EchoProvider",
    "GeminiProvider",
    "OllamaProvider",
    "OpenAIProvider",
    "ProviderCall",
    "ProviderCapabilities",
    "ProviderReply",
]
