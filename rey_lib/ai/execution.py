"""One execution, from a resolved request to a result.

The subordinate owner the root delegates to. It holds the lifecycle -- prepare,
invoke, retry, stream, cancel, parse, normalize, complete or fail -- so the root
stays the domain and state owner rather than becoming the place every mechanism
ended up.

The ordering is the old runner's, where the old runner was right:

    capability refused first, before a provider is touched
    cancellation checked before each attempt
    the provider called
    provider failures translated, then retried if policy says so
    output parsed and validated
    evidence recorded for what actually ran

Nothing here knows which profile is *selected*; it is told which one applies.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from rey_lib.ai.capabilities import AICapability, capabilities_for_content
from rey_lib.ai.contracts import ContractResolver
from rey_lib.ai.content import AIContentKind, AIMessage, AIRole
from rey_lib.ai.errors import (
    AICancelled,
    AICapabilityError,
    AIError,
    AIExecutionError,
    AIProviderError,
    AIRequestError,
)
from rey_lib.ai.output import OutputParser
from rey_lib.ai.providers.base import AIProvider, ProviderCall, ProviderReply
from rey_lib.ai.registry import AIRegistry
from rey_lib.ai.requests import AIOutputKind, ResolvedAIRequest
from rey_lib.ai.results import (
    AIAttempt,
    AIEvidence,
    AIExecutionInfo,
    AIFinishReason,
    AIOutput,
    AIResult,
    AIUsage,
)
from rey_lib.ai.retry import DEFAULT_RETRY_POLICY, AIRetryPolicy
from rey_lib.ai.streaming import AIEvent

__all__ = ["AIExecutor"]


class AIExecutor:
    """Performs one resolved request at a time, for a runtime.

    Holds no mutable state of its own between executions: everything an
    execution needs arrives in its resolved request, which is what lets two
    consumers share one runtime without reaching each other.
    """

    def __init__(
        self,
        *,
        registry: AIRegistry,
        contracts: ContractResolver | None = None,
        parser: OutputParser | None = None,
        retry_policy: AIRetryPolicy = DEFAULT_RETRY_POLICY,
    ) -> None:
        self._registry = registry
        self._contracts = contracts or ContractResolver()
        self._parser = parser or OutputParser()
        self._retry = retry_policy

    # -- the two ways in --------------------------------------------------

    def execute(self, request: ResolvedAIRequest) -> AIResult:
        """Run it and answer with the result."""
        result: AIResult | None = None
        for event in self.stream(request):
            if event.result is not None:
                result = event.result
        if result is None:  # pragma: no cover -- stream always ends in one or raises
            raise AIExecutionError("Execution produced no result.")
        return result

    def stream(self, request: ResolvedAIRequest) -> Iterator[AIEvent]:
        """Run it, reporting canonical events as it goes.

        ``execute`` is this, drained. One path, so a streamed execution and a
        waited-for one cannot come to different answers.
        """
        execution_id = uuid.uuid4().hex
        self._refuse_if_impossible(request)

        started = datetime.now(timezone.utc)
        yield AIEvent.started(execution_id)

        provider = self._registry.provider_for(request.profile)
        call = self._call_for(request)
        attempts: list[AIAttempt] = []
        deltas: list[str] = []
        reply: ProviderReply | None = None
        failure: AIError | None = None
        provider_failures = 0

        for number in range(1, self._retry.max_attempts + 1):
            if _withdrawn(request):
                yield from self._cancelled(execution_id, request, started, attempts)
                return
            deltas.clear()
            try:
                reply = provider.invoke(
                    call,
                    cancelled=request.cancelled,
                    on_text=deltas.append,
                )
                attempts.append(AIAttempt(number=number))
                break
            except AICancelled:
                yield from self._cancelled(execution_id, request, started, attempts)
                return
            except AIError as exc:
                attempts.append(AIAttempt(number=number, failed=True, error=str(exc)))
                failure = exc
                if isinstance(exc, AIProviderError):
                    provider_failures += 1
                    if (
                        self._retry.provider_failure_limit is not None
                        and provider_failures >= self._retry.provider_failure_limit
                    ):
                        break
                if not self._retry.retryable(exc) or number == self._retry.max_attempts:
                    break
            except Exception as exc:  # noqa: BLE001 -- a provider must not leak
                translated = AIProviderError(
                    f"Provider '{provider.name}' failed: {exc}", cause=exc,
                )
                attempts.append(AIAttempt(number=number, failed=True, error=str(translated)))
                failure = translated
                provider_failures += 1
                if number == self._retry.max_attempts:
                    break

        if reply is None:
            info = self._info(execution_id, request, started, attempts, AIFinishReason.FAILED)
            message = str(failure) if failure else "Execution failed."
            yield AIEvent.failed(execution_id, message)
            raise AIExecutionError(message, cause=failure) if failure is None else failure

        for delta in deltas:
            yield AIEvent.content(execution_id, delta)

        if reply.tool_calls:
            for call_requested in reply.tool_calls:
                yield AIEvent.tool_requested(execution_id, call_requested)
            info = self._info(
                execution_id, request, started, attempts, AIFinishReason.TOOL_CALLS,
            )
            result = AIResult(
                output=AIOutput(form=AIOutput().form, text="", value=None),
                execution=info,
                usage=reply.usage,
                tool_calls=reply.tool_calls,
                evidence=self._evidence(reply),
            )
            yield AIEvent.usage_updated(execution_id, reply.usage)
            yield AIEvent.completed(execution_id, result)
            return

        output = self._parser.parse(request, reply.text, reply.value)
        if output.value is not None:
            yield AIEvent.structured(execution_id, output.value)

        info = self._info(execution_id, request, started, attempts, AIFinishReason.COMPLETED)
        result = AIResult(
            output=output,
            execution=info,
            usage=reply.usage,
            evidence=self._evidence(reply),
        )
        yield AIEvent.usage_updated(execution_id, reply.usage)
        yield AIEvent.completed(execution_id, result)

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

    # -- turning a resolved request into a provider call ------------------

    def _call_for(self, request: ResolvedAIRequest) -> ProviderCall:
        """The provider-facing call, built once per execution."""
        messages: list[dict[str, Any]] = []

        body = self._contracts.body_of(request.instruction)
        if body:
            messages.append({"role": AIRole.SYSTEM.value, "content": body})

        for message in request.input.messages:
            messages.append({
                "role": message.role.value,
                "content": _flatten(message),
            })
        if request.input.content:
            messages.append({
                "role": AIRole.USER.value,
                "content": _flatten(AIMessage(role=AIRole.USER, content=request.input.content)),
            })

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
            or (request.output.kind is AIOutputKind.CONTRACT and request.schema is not None),
            schema=request.schema,
            temperature=request.options.temperature,
            max_tokens=request.options.max_tokens,
            options=dict(request.profile.options),
        )

    # -- endings -----------------------------------------------------------

    def _cancelled(
        self,
        execution_id: str,
        request: ResolvedAIRequest,
        started: datetime,
        attempts: list[AIAttempt],
    ) -> Iterator[AIEvent]:
        """The caller withdrew. Reported, then raised."""
        self._info(execution_id, request, started, attempts, AIFinishReason.CANCELLED)
        yield AIEvent.failed(execution_id, "Cancelled.")
        raise AICancelled("The caller withdrew this AI execution.")

    @staticmethod
    def _info(
        execution_id: str,
        request: ResolvedAIRequest,
        started: datetime,
        attempts: list[AIAttempt],
        finish: AIFinishReason,
    ) -> AIExecutionInfo:
        """What actually ran."""
        return AIExecutionInfo(
            execution_id=execution_id,
            profile_id=request.profile.id,
            provider=request.profile.provider,
            model=request.profile.model,
            instruction_id=request.instruction.id,
            started_at=started.isoformat(),
            ended_at=datetime.now(timezone.utc).isoformat(),
            attempts=tuple(attempts),
            finish_reason=finish,
        )

    @staticmethod
    def _evidence(reply: ProviderReply) -> AIEvidence:
        """Provenance, carrying the provider's own payload as data."""
        return AIEvidence(payload_id=uuid.uuid4().hex, provider_raw=dict(reply.raw))


def _withdrawn(request: ResolvedAIRequest) -> bool:
    """Whether the caller has withdrawn this execution."""
    return request.cancelled is not None and bool(request.cancelled())


def _flatten(message: AIMessage) -> str:
    """One message's parts, as the text a provider adapter receives.

    Text and structured parts render; a reference part contributes its
    reference. An adapter that can do better with a part is free to read the
    canonical message instead -- this is the shape every adapter can accept.
    """
    rendered: list[str] = []
    for part in message.content:
        if part.kind is AIContentKind.TEXT:
            rendered.append(str(part.value))
        elif part.kind is AIContentKind.STRUCTURED:
            import json  # noqa: PLC0415 -- only where a structured part appears

            rendered.append(json.dumps(part.value, ensure_ascii=False, sort_keys=True))
        else:
            rendered.append(f"[{part.kind.value}: {part.value}]")
    return "\n\n".join(rendered)
