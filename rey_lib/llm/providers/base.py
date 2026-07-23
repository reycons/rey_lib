"""
BaseProvider interface and supporting types.

All provider implementations must subclass BaseProvider and implement run().
The rest of the framework depends only on these types — never on SDK-specific
objects.

Public API
----------
Message
    A single role+content pair in a provider-neutral message array.
ProviderCapabilities
    Declares what a provider supports (tools, images, JSON mode, etc.).
ProviderResponse
    Normalised response envelope returned by every provider.
BaseProvider
    Abstract base class all providers must implement.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Optional

__all__ = [
    "Message",
    "ProviderCapabilities",
    "ProviderResponse",
    "BaseProvider",
]


@dataclass(frozen=True)
class Message:
    """A single message in a provider-neutral conversation array.

    Attributes
    ----------
    role : str
        Speaker role — typically 'system', 'user', or 'assistant'.
    content : str
        Text content of the message.
    """

    role:    str
    content: str


@dataclass(frozen=True)
class ProviderCapabilities:
    """Declares the capabilities of a specific provider.

    Pipelines may require capabilities; if the selected provider lacks a
    required capability, execution must fail before the provider is called.

    Attributes
    ----------
    supports_tools : bool
    supports_images : bool
    supports_json_mode : bool
    supports_streaming : bool
    supports_system_messages : bool
    max_context_tokens : Optional[int]
        None means unknown or unlimited.
    """

    supports_tools:           bool
    supports_images:          bool
    supports_json_mode:       bool
    supports_streaming:       bool
    supports_system_messages: bool
    max_context_tokens:       Optional[int] = None


@dataclass(frozen=True)
class ProviderResponse:
    """Normalised response envelope from a provider call.

    Attributes
    ----------
    content : str
        Text content of the LLM response.
    tokens_in : int
        Input tokens consumed (0 if the provider does not report usage).
    tokens_out : int
        Output tokens generated (0 if the provider does not report usage).
    model : str
        Model identifier as returned by the provider.
    raw : dict[str, Any]
        Full raw response payload for audit/replay purposes.
    """

    content:    str
    tokens_in:  int
    tokens_out: int
    model:      str
    raw:        dict[str, Any]


class BaseProvider(ABC):
    """Abstract base class for all LLM provider implementations.

    Subclasses must implement run() and expose capabilities.
    No provider SDK may be imported at module level — imports must be
    deferred inside run() so the SDK is only required when the provider
    is actually used.
    """

    @property
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        """Return this provider's capability declaration."""

    @abstractmethod
    def run(
        self,
        messages:    list[Message],
        model:       str,
        max_tokens:  int   = 4000,
        temperature: float = 0.0,
        on_chunk:    Optional[Callable[[str], None]] = None,
        cancelled:   Optional[Callable[[], bool]] = None,
    ) -> ProviderResponse:
        """Send messages to the provider and return a normalised response.

        Parameters
        ----------
        messages : list[Message]
            Ordered message array (system → user → assistant turns).
        model : str
            Model identifier (e.g. 'claude-opus-4-7', 'gpt-4o').
        max_tokens : int
            Maximum tokens the provider may generate.
        temperature : float
            Sampling temperature.  0.0 for deterministic output.
        on_chunk : Optional[Callable[[str], None]]
            Optional incremental-output callback. When supplied and the provider
            supports streaming, it is invoked with each text delta as it arrives;
            the full ProviderResponse is still returned. When None (the default)
            the call is a single blocking request — behaviour is unchanged.
            Providers that do not support streaming ignore this callback.
        cancelled : Optional[Callable[[], bool]]
            Optional cooperative cancellation check. Streaming providers must
            stop and release their response when it becomes true.

        Returns
        -------
        ProviderResponse
            Normalised response with content, token counts, and raw payload.

        Raises
        ------
        ProviderFailure
            If the provider API call fails for any reason.
        TimeoutFailure
            If the provider call exceeds the configured timeout.
        """
