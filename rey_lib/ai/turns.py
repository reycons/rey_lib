"""One provider turn, and everything that repeating or relocating it involves.

The subordinate owner for the narrowest unit of execution: send one turn to a
provider and come back with a reply. Three control domains meet here and are
kept distinct:

    transport retry   repeat this turn against the same provider
    replay safety     whether repeating it is legal at all
    fallback          move to the next configured provider and try there

None of them takes a turn from ``ExecutionBudget``. A turn is what the
coordinator spends when it asks the model something; everything here is the
machinery of getting that one question answered.

Fallback is model-invisible -- it changes nothing the model sees -- but it is
never observer-invisible. Every transition is reported as a canonical event, so
a provider switch cannot happen silently.
"""

from __future__ import annotations

import time
from typing import Callable

from rey_lib.ai.errors import AICancelled, AIError, AIProviderError
from rey_lib.ai.policies import AIExecutionPolicy
from rey_lib.ai.providers.base import AIProvider, ProviderCall, ProviderReply
from rey_lib.ai.registry import AIRegistry
from rey_lib.ai.requests import ResolvedAIRequest
from rey_lib.ai.results import AIAttempt
from rey_lib.ai.state import ExecutionState
from rey_lib.ai.streaming import AIEvent

__all__ = ["TurnExecutor", "TurnOutcome"]


class TurnOutcome:
    """What one turn came to.

    Two members rather than a reply that might be ``None``: a caller that has to
    test for absence eventually forgets to, and a failed turn carries a reason
    that a missing value cannot.
    """

    __slots__ = ("failure", "provider_name", "reply")

    def __init__(
        self,
        *,
        reply: ProviderReply | None = None,
        failure: AIError | None = None,
        provider_name: str = "",
    ) -> None:
        self.reply = reply
        self.failure = failure
        self.provider_name = provider_name

    @property
    def succeeded(self) -> bool:
        """Whether a provider answered."""
        return self.reply is not None


class TurnExecutor:
    """Performs one provider turn, with retry, replay safety and fallback.

    Holds no state between turns. Everything that accumulates belongs to
    ``ExecutionState``, which is handed in.
    """

    def __init__(self, *, registry: AIRegistry, policy: AIExecutionPolicy) -> None:
        self._registry = registry
        self._policy = policy

    def take(
        self,
        request: ResolvedAIRequest,
        state: ExecutionState,
        call: ProviderCall,
        *,
        emit: Callable[[AIEvent], None],
        on_text: Callable[[str], None],
    ) -> TurnOutcome:
        """Send one turn, and answer with what became of it.

        Tries the resolved provider first, then each provider the fallback
        policy names, and within each applies the transport retry policy subject
        to replay safety.
        """
        tried: tuple[str, ...] = ()
        primary = self._registry.provider_for(request.profile)
        provider: AIProvider | None = primary
        failure: AIError | None = None

        while provider is not None:
            tried = (*tried, provider.name)
            outcome = self._attempt_with(provider, request, state, call, on_text=on_text)
            if outcome.succeeded:
                return outcome
            failure = outcome.failure

            following = self._policy.fallback.after(tried)
            if not following:
                break
            provider = self._fallback_to(following, emit, failure, provider.name)

        return TurnOutcome(failure=failure, provider_name=tried[-1] if tried else "")

    # -- one provider, with its retry budget ------------------------------

    def _attempt_with(
        self,
        provider: AIProvider,
        request: ResolvedAIRequest,
        state: ExecutionState,
        call: ProviderCall,
        *,
        on_text: Callable[[str], None],
    ) -> TurnOutcome:
        """Every attempt this transport policy allows against one provider."""
        policy = self._policy.transport
        failure: AIError | None = None


        for attempt in range(1, policy.attempts + 1):
            if _withdrawn(request):
                raise AICancelled("The caller withdrew this AI execution.")

            delay = policy.delay_before(attempt)
            if delay:
                time.sleep(delay)

            try:
                reply = provider.invoke(
                    call, cancelled=request.cancelled, on_text=on_text,
                )
            except AICancelled:
                raise
            except AIError as exc:
                failure = exc
                state.record_attempt(
                    AIAttempt(number=attempt, failed=True, error=str(exc)),
                )
                if not self._may_repeat(provider, exc, attempt, state):
                    break
            except Exception as exc:  # noqa: BLE001 -- a provider must not leak
                failure = AIProviderError(
                    f"Provider '{provider.name}' failed: {exc}", cause=exc,
                )
                state.record_attempt(
                    AIAttempt(number=attempt, failed=True, error=str(failure)),
                )
                if not self._may_repeat(provider, failure, attempt, state):
                    break
            else:
                state.record_attempt(AIAttempt(number=attempt))
                return TurnOutcome(reply=reply, provider_name=provider.name)

        return TurnOutcome(failure=failure, provider_name=provider.name)

    def _may_repeat(
        self,
        provider: AIProvider,
        failure: AIError,
        attempt: int,
        state: ExecutionState,
    ) -> bool:
        """Whether this failure may be repeated: policy **and** replay safety.

        Two independent questions. A retryable failure whose operation cannot
        legally be replayed is not retried, and the refusal is recorded as an
        attempt note so it is visible rather than looking like exhaustion.
        """
        transport = self._policy.transport
        if attempt >= transport.attempts or not transport.retryable(failure):
            return False

        facts = provider.replay_facts(failure)
        if self._policy.replay.permits(facts):
            return True

        state.record_attempt(
            AIAttempt(
                number=attempt,
                failed=True,
                error=(
                    "Not replayed: "
                    + self._policy.replay.refusal(facts)
                    + f" (after: {failure})"
                ),
            ),
        )
        return False

    # -- moving to the next configured provider ---------------------------

    def _fallback_to(
        self,
        name: str,
        emit: Callable[[AIEvent], None],
        failure: AIError | None,
        previous: str,
    ) -> AIProvider | None:
        """The next provider, announced. ``None`` when it is not available."""
        try:
            provider = self._registry.provider(name)
        except AIError:
            return None
        emit(
            AIEvent.provider_changed(
                from_provider=previous,
                to_provider=name,
                reason=str(failure) if failure else "",
            ),
        )
        return provider


def _withdrawn(request: ResolvedAIRequest) -> bool:
    """Whether the caller has withdrawn this execution."""
    return request.cancelled is not None and bool(request.cancelled())
