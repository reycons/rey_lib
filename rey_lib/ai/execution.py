"""One execution, from a resolved request to a normalized result.

The coordinator. It owns the *order* of an execution and delegates every
mechanism, so that adding a control domain does not grow one method:

    refuse what cannot be done          before a provider is touched
    take a turn                         TurnExecutor
    continue through tools              ToolLoop
    validate the output                 OutputParser
    correct it, where policy allows     ValidationCorrectionPolicy
    normalize it for the caller         OutputNormalizer
    report canonically throughout       AIEvent

Nothing here performs provider transport, retry, replay adjudication, fallback,
tool execution, validation or normalization. It decides what happens next.

The ordering is the old runner's where the old runner was right: capability
refused first, cancellation checked before each attempt, provider failures
translated then retried if policy says so, output validated, evidence recorded
for what actually ran.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Iterator

from rey_lib.ai.capabilities import AICapability, capabilities_for_content
from rey_lib.ai.content import AIContent, AIMessage, AIRole, structured, text
from rey_lib.ai.contracts import ContractResolver
from rey_lib.ai.errors import (
    AICancelled,
    AICapabilityError,
    AIError,
    AIExecutionError,
    AIOutputError,
    AIRequestError,
)
from rey_lib.ai.normalization import OutputNormalizer
from rey_lib.ai.output import OutputParser
from rey_lib.ai.policies import DEFAULT_EXECUTION_POLICY, AIExecutionPolicy
from rey_lib.ai.providers.base import ProviderCall, ProviderReply
from rey_lib.ai.registry import AIRegistry
from rey_lib.ai.requests import AIOutputKind, ResolvedAIRequest
from rey_lib.ai.results import (
    AIEvidence,
    AIExecutionInfo,
    AIFinishReason,
    AIOutput,
    AIResult,
)
from rey_lib.ai.state import ExecutionState
from rey_lib.ai.streaming import AIEvent
from rey_lib.ai.tool_loop import CanonicalToolLoop, ToolLoop
from rey_lib.ai.turns import TurnExecutor

__all__ = ["AIExecutor"]


class AIExecutor:
    """Performs one resolved request at a time, for a runtime.

    Holds no mutable state of its own between executions: configuration arrives
    in the resolved request and accumulation lives in ``ExecutionState``, which
    is what lets two consumers share one runtime without reaching each other.
    """

    def __init__(
        self,
        *,
        registry: AIRegistry,
        contracts: ContractResolver | None = None,
        parser: OutputParser | None = None,
        normalizer: OutputNormalizer | None = None,
        tool_loop: ToolLoop | None = None,
        policy: AIExecutionPolicy = DEFAULT_EXECUTION_POLICY,
    ) -> None:
        self._registry = registry
        self._contracts = contracts or ContractResolver()
        self._parser = parser or OutputParser()
        self._normalizer = normalizer or OutputNormalizer()
        self._policy = policy
        self._turns = TurnExecutor(registry=registry, policy=policy)
        self._tool_loop = tool_loop or CanonicalToolLoop(policy=policy)

    # -- the two ways in --------------------------------------------------

    def execute(self, request: ResolvedAIRequest) -> AIResult:
        """Run it and answer with the result."""
        result: AIResult | None = None
        for event in self.stream(request):
            if event.result is not None:
                result = event.result
        if result is None:  # pragma: no cover -- stream ends in one or raises
            raise AIExecutionError("Execution produced no result.")
        return result

    def stream(self, request: ResolvedAIRequest) -> Iterator[AIEvent]:
        """Run it, reporting canonical events as it goes.

        ``execute`` is this, drained. One path, so a streamed execution and a
        waited-for one cannot come to different answers.
        """
        self._refuse_if_impossible(request)

        state = ExecutionState(execution_id=uuid.uuid4().hex)
        started = datetime.now(timezone.utc)
        pending: list[AIEvent] = []

        yield AIEvent.started(state.execution_id)

        try:
            reply = self._turn(request, state, pending.append)
            yield from _drain(pending)

            if request.tool_runner is not None:
                reply = self._tool_loop.run(
                    request,
                    state,
                    reply,
                    take_turn=lambda req, st: self._turn(req, st, pending.append),
                    runner=request.tool_runner,
                    emit=pending.append,
                )
                yield from _drain(pending)
            elif reply.tool_calls:
                yield from self._unhandled_tools(request, state, started, reply)
                return

            output, reply = self._validated(request, state, reply, pending.append)
            yield from _drain(pending)
        except AICancelled:
            yield AIEvent.failed(state.execution_id, "Cancelled.")
            raise
        except AIError as failure:
            yield from _drain(pending)
            yield AIEvent.failed(state.execution_id, str(failure))
            raise

        if output.value is not None:
            yield AIEvent.structured(state.execution_id, output.value)

        result = AIResult(
            output=self._normalizer.normalize(request, output),
            execution=self._info(request, state, started, AIFinishReason.COMPLETED),
            usage=state.usage,
            tool_calls=tuple(state.tool_calls),
            tool_results=tuple(state.tool_results),
            evidence=AIEvidence(
                payload_id=uuid.uuid4().hex, provider_raw=dict(reply.raw),
            ),
        )
        yield AIEvent.usage_updated(state.execution_id, state.usage)
        yield AIEvent.completed(state.execution_id, result)

    # -- one turn, and what it costs --------------------------------------

    def _turn(
        self,
        request: ResolvedAIRequest,
        state: ExecutionState,
        emit: Any,
    ) -> ProviderReply:
        """Take one provider turn against the resolved configuration.

        Spends one ``ExecutionBudget`` turn. Transport retries, replay refusals
        and fallback transitions happen inside and cost no turn, because they
        are the machinery of asking once rather than asking again.
        """
        if self._policy.budget.exhausted(state.turns_taken):
            raise AIExecutionError(
                "The execution budget of "
                f"{self._policy.budget.max_turns} turns is spent."
            )
        state.began_turn()

        deltas: list[str] = []
        outcome = self._turns.take(
            request, state, self._call_for(request, state),
            emit=emit, on_text=deltas.append,
        )
        if not outcome.succeeded:
            raise outcome.failure or AIExecutionError("Execution failed.")

        for delta in deltas:
            emit(AIEvent.content(state.execution_id, delta))

        reply = outcome.reply
        state.add_usage(reply.usage)
        return reply

    # -- refusals that come before a provider -----------------------------

    def _refuse_if_impossible(self, request: ResolvedAIRequest) -> None:
        """Everything that can be known without calling anyone."""
        if request.input.is_empty():
            raise AIRequestError("An AI request must carry something to send.")

        effective = self._registry.effective_capability(request.profile)

        required = set(capabilities_for_content(request.input.kinds()))
        if request.output.is_structured():
            required.add(AICapability.STRUCTURED_OUTPUT)
        if request.tools:
            required.add(AICapability.TOOLS)
        if request.options.stream:
            required.add(AICapability.STREAMING)

        missing = effective.missing(frozenset(required))
        if missing:
            names = ", ".join(sorted(capability.value for capability in missing))
            raise AICapabilityError(
                f"AI profile '{request.profile.id}' cannot {names}."
            )

    # -- validation, and the correction it may earn -----------------------

    def _validated(
        self,
        request: ResolvedAIRequest,
        state: ExecutionState,
        reply: ProviderReply,
        emit: Any,
    ) -> tuple[AIOutput, ProviderReply]:
        """Output that satisfies the contract, correcting where policy allows.

        A correction is a *new turn* carrying the failure back to the model --
        not a repeat of the same call, which the transport policy forbids for
        exactly this failure. It spends a validation correction and a turn, and
        no transport attempt.
        """
        policy = self._policy.validation_correction
        while True:
            try:
                return self._parser.parse(request, reply.text, reply.value), reply
            except AIOutputError as invalid:
                taken = state.validation_corrections
                if policy.exhausted(taken):
                    raise
                state.corrected_validation()
                state.turns.append(
                    AIMessage(
                        role=AIRole.ASSISTANT,
                        content=(text(reply.text),),
                    ),
                )
                state.correction(
                    AIMessage(
                        role=AIRole.USER,
                        content=(
                            text(
                                "That output did not satisfy the required shape: "
                                f"{invalid}. Answer again, satisfying it exactly."
                            ),
                        ),
                    ),
                )
                reply = self._turn(request, state, emit)

    # -- the model asked for a tool nobody can carry out -------------------

    def _unhandled_tools(
        self,
        request: ResolvedAIRequest,
        state: ExecutionState,
        started: datetime,
        reply: ProviderReply,
    ) -> Iterator[AIEvent]:
        """Report requested calls and finish, when no runner was supplied.

        A caller that offered tools without the means to run them gets the calls
        rather than a failure: deciding what to do with them is its own.
        """
        state.requested_tools(reply.tool_calls)
        for call in reply.tool_calls:
            yield AIEvent.tool_requested(state.execution_id, call)
        result = AIResult(
            output=AIOutput(),
            execution=self._info(request, state, started, AIFinishReason.TOOL_CALLS),
            usage=state.usage,
            tool_calls=tuple(state.tool_calls),
            evidence=AIEvidence(
                payload_id=uuid.uuid4().hex, provider_raw=dict(reply.raw),
            ),
        )
        yield AIEvent.usage_updated(state.execution_id, state.usage)
        yield AIEvent.completed(state.execution_id, result)

    # -- turning a resolved request into a provider call ------------------

    def _call_for(
        self, request: ResolvedAIRequest, state: ExecutionState,
    ) -> ProviderCall:
        """The provider-facing call: the resolved configuration plus history.

        This is the continuation rule in code. The configuration comes from the
        resolved request every time, unchanged; only the accumulated turns grow.
        Nothing re-reads settings or capabilities because another round is
        needed.

        Messages stay canonical. Flattening them to strings here is what let a
        declared vision capability hand an adapter ``[image: ...]`` text instead
        of the image.
        """
        messages: list[AIMessage] = []

        body = self._contracts.body_of(request.instruction)
        if body:
            messages.append(AIMessage(role=AIRole.SYSTEM, content=(text(body),)))

        messages.extend(request.input.messages)
        if request.input.content:
            messages.append(
                AIMessage(role=AIRole.USER, content=request.input.content),
            )
        messages.extend(state.accumulated())

        return ProviderCall(
            model=request.profile.model,
            messages=tuple(messages),
            tools=tuple(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema or {},
                }
                for tool in request.tools
            ),
            json_output=request.output.kind in (AIOutputKind.JSON, AIOutputKind.SCHEMA)
            or (
                request.output.kind is AIOutputKind.CONTRACT
                and request.schema is not None
            ),
            schema=request.schema,
            temperature=request.options.temperature,
            max_tokens=request.options.max_tokens,
            options=dict(request.profile.options),
        )

    # -- evidence ----------------------------------------------------------

    @staticmethod
    def _info(
        request: ResolvedAIRequest,
        state: ExecutionState,
        started: datetime,
        finish: AIFinishReason,
    ) -> AIExecutionInfo:
        """What actually ran."""
        return AIExecutionInfo(
            execution_id=state.execution_id,
            profile_id=request.profile.id,
            provider=request.profile.provider,
            model=request.profile.model,
            instruction_id=request.instruction.id,
            started_at=started.isoformat(),
            ended_at=datetime.now(timezone.utc).isoformat(),
            attempts=tuple(state.attempts),
            finish_reason=finish,
        )


def _drain(pending: list[AIEvent]) -> Iterator[AIEvent]:
    """Hand out the events a subordinate owner reported, and forget them.

    Subordinates take an ``emit`` callable rather than being generators, because
    a generator that is not fully consumed silently stops doing its work
    part-way through an execution.
    """
    while pending:
        yield pending.pop(0)
