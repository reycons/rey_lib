"""The provider boundary.

Everything provider-specific stops here. Above this line the subsystem speaks
its own vocabulary; below it, an adapter speaks whatever its SDK requires.

An adapter owns request translation, authentication and client material, SDK
interaction, stream decoding, native error translation, and reporting what its
model can do. It owns no application settings, no default selection, no prompt
and no interface state.

Two rules learned from the old provider base and kept:

- no SDK is imported at module level, so an optional dependency stays optional
  until the provider is actually used;
- a provider's own exception never leaves the adapter -- it is translated, with
  the original kept as the cause so an operator still gets the diagnosis.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from rey_lib.ai.capabilities import AICapabilitySet
from rey_lib.ai.results import AIUsage
from rey_lib.ai.tools import AIToolCall

__all__ = ["AIProvider", "ProviderCall", "ProviderReply"]


@dataclass(frozen=True)
class ProviderCall:
    """What an adapter is asked to do.

    Already resolved: the model is chosen, the messages are flattened into the
    turn sequence this provider will receive, and the options are the ones that
    survived resolution. An adapter decides nothing about which profile applies.
    """

    model: str
    messages: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...] = ()
    json_output: bool = False
    schema: dict[str, Any] | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderReply:
    """What an adapter answers with, in the subsystem's terms.

    ``raw`` is the provider's own payload, kept for replay and audit. It is data
    from here upward -- nothing reads it, and it reaches an application only
    inside evidence.
    """

    text: str = ""
    value: Any = None
    tool_calls: tuple[AIToolCall, ...] = ()
    usage: AIUsage = field(default_factory=AIUsage)
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class AIProvider(ABC):
    """One provider, adapted.

    Instances are held by the runtime's own registry, never by a module-level
    map: the old subsystem kept providers in a process-global dict, which two
    installations then shared.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """What configuration addresses this provider by."""

    @abstractmethod
    def capability_for(self, model: str) -> AICapabilitySet:
        """What this provider and model can do.

        Layer one of the capability model. An installation's policy narrows it;
        nothing widens it.
        """

    @abstractmethod
    def invoke(
        self,
        call: ProviderCall,
        *,
        cancelled: Callable[[], bool] | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> ProviderReply:
        """Perform one call and answer in the subsystem's terms.

        ``on_text`` is how an adapter reports text as it arrives. It is a
        provider-facing detail: the execution owner turns those into canonical
        events, and no caller above sees this callback.

        Raises:
            AIProviderError: for anything the provider refused or could not do,
                with the native error kept as the cause.
        """
