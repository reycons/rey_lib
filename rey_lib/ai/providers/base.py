"""The provider boundary.

Everything provider-specific stops here. Above this line the subsystem speaks
its own vocabulary; below it, an adapter speaks whatever its SDK requires.

An adapter owns request translation, resolved authentication and client
material, SDK interaction, stream decoding, native error translation, and
reporting what its model can do. It owns no application settings, no default
selection, no prompt and no interface state -- and it never reads ``ctx``.

Three rules learned from the old provider base and kept:

- no SDK is imported at module level, so an optional dependency stays optional
  until the provider is actually used;
- a provider's own exception never leaves the adapter -- it is translated, with
  the original kept as the cause so an operator still gets the diagnosis;
- an adapter holds no process-global state; the runtime's own registry holds it.

**The nested-retry contract.** An adapter must not let its SDK retry
internally. Retry policy is owned above this boundary, and an SDK retrying
underneath it multiplies rather than composes: a three-attempt policy over an
SDK configured for three, over a router configured for two, is eighteen network
calls presented to an operator as three. An adapter therefore configures its
client for **no internal retry** and declares that it has, through
``retries_internally``. Where a client cannot be made to stop, the adapter says
so truthfully and the execution owner accounts for it rather than being
surprised by it.

**Replay facts.** An adapter reports what it knows about whether a failed call
may be repeated -- whether the provider had begun emitting, and whether the
call carried provider-side state. The execution owner decides; only the adapter
can observe. Silence is ``UNKNOWN``, which is treated as unsafe.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from rey_lib.ai.capabilities import AICapabilitySet
from rey_lib.ai.content import AIMessage
from rey_lib.ai.policies import ReplayFacts
from rey_lib.ai.results import AIUsage
from rey_lib.ai.tools import AIToolCall

__all__ = ["AIProvider", "ProviderCall", "ProviderReply"]


@dataclass(frozen=True)
class ProviderCall:
    """What an adapter is asked to do.

    Already resolved: the model is chosen, the turns are in order, and the
    options are the ones that survived resolution. An adapter decides nothing
    about which profile applies.

    ``messages`` carries **canonical** ``AIMessage`` values, not flattened
    strings. That is deliberate and was a defect before: flattening rendered an
    image part to ``[image: ...]`` text, so a profile could declare vision, pass
    the capability check, and then hand the adapter a description of an image
    instead of the image. An adapter translates each part into what its SDK
    takes, and can only do that if the part still exists.
    """

    model: str
    messages: tuple[AIMessage, ...]
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

    An adapter is constructed from its ``ConfiguredProvider`` and resolves any
    credential reference at that point. Construction is the adapter's own -- this
    contract mandates none -- but nothing may be discovered afterwards.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """What configuration addresses this provider by."""

    @property
    def retries_internally(self) -> bool:
        """Whether this adapter's SDK retries beneath the Rey boundary.

        False is the contract, and the default, because an adapter is required
        to configure its client for no internal retry. An adapter that genuinely
        cannot stop its client overrides this to say so, which is what keeps a
        multiplied attempt count visible instead of silent.
        """
        return False

    @abstractmethod
    def capability_for(self, model: str) -> AICapabilitySet:
        """What this provider and model can do.

        Layer one of the capability model. An installation's policy narrows it;
        nothing widens it.
        """

    def replay_facts(self, failure: BaseException) -> ReplayFacts:
        """What is known about repeating the call this failure came from.

        The default says nothing is known, which the replay predicate treats as
        unsafe. An adapter that can tell whether emission had begun, or whether
        the call carried provider-side conversation state, overrides this --
        answering honestly is what allows a retry rather than preventing one.
        """
        return ReplayFacts()

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
