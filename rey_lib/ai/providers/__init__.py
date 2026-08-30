"""Provider adapters, and the boundary they implement.

Adapters are held by a runtime's own registry. Nothing here is registered in a
module-level map: the old subsystem's process-global provider dict was shared by
every installation in the process, which is the state this subsystem does not
have.
"""

from rey_lib.ai.providers.base import AIProvider, ProviderCall, ProviderReply
from rey_lib.ai.providers.configuration import ConfiguredProvider, ProviderCapabilities
from rey_lib.ai.providers.echo_provider import EchoProvider

__all__ = [
    "AIProvider",
    "ConfiguredProvider",
    "EchoProvider",
    "ProviderCall",
    "ProviderCapabilities",
    "ProviderReply",
]
