"""The AI subsystem, proved against its own model.

Eleven design scenarios and the isolation guarantees. Each is here because the
frozen model claims something; a test that only exercised a constructor would
prove none of them.

Nothing is attached to production: every runtime below is built from explicit
inputs, which is the same construction a bootstrap will eventually perform.
"""

from __future__ import annotations

import json

import pytest

from rey_lib.ai import (
    AI,
    AICancelled,
    AICapability,
    AICapabilityError,
    AICapabilitySet,
    AIContentKind,
    AIError,
    AIEventKind,
    AIExecutionError,
    AIInput,
    AIInstruction,
    AIInstructionKind,
    AIMessage,
    AIOutputForm,
    AIOutputSpec,
    AIProfile,
    AIProviderError,
    AIRegistry,
    AIRequest,
    AIRequestOptions,
    AIExecutionPolicy,
    AIRole,
    AISelectionError,
    AISettings,
    AITool,
    AIToolCall,
    CancellationToken,
    EchoProvider,
    document,
    image,
    text,
)

FULL = AICapabilitySet.of(*AICapability)


def runtime(
    *,
    provider: EchoProvider | None = None,
    profiles: tuple[AIProfile, ...] = (),
    instructions: tuple[AIInstruction, ...] = (),
    settings: AISettings | None = None,
    policy: AIExecutionPolicy | None = None,
) -> AI:
    """One runtime, built the way a bootstrap will build one."""
    echo = provider or EchoProvider()
    registry = AIRegistry(
        profiles=profiles or (AIProfile(id="fast", name="Fast", provider="echo", model="m1"),),
        instructions=instructions,
        providers=(echo,),
    )
    return AI(
        registry=registry,
        settings=settings or AISettings(profile_id="fast"),
        **({"policy": policy} if policy else {}),
    )


# -- the ten design scenarios ------------------------------------------------

def test_scenario_1_simple_prompt() -> None:
    """Text in, text out, no new object anywhere on the path."""
    result = runtime().execute(AIRequest.prompt("hello"))

    assert result.output.form is AIOutputForm.TEXT
    assert result.text == "hello"
    assert result.execution.model == "m1"


def test_scenario_2_structured_output_stays_structured() -> None:
    """A structured result keeps its value; it is not stringified."""
    schema = {"type": "object", "required": ["echo"], "properties": {"echo": {"type": "string"}}}
    result = runtime().execute(
        AIRequest.prompt("hello", output=AIOutputSpec.schema_of(schema)),
    )

    assert result.output.form is AIOutputForm.STRUCTURED
    assert result.output.value == {"echo": "hello"}


def test_scenario_2b_a_violated_schema_is_refused_and_not_retried() -> None:
    """An output failure is an execution failure, not a transient one."""
    schema = {"type": "object", "required": ["missing"]}
    provider = EchoProvider()
    ai = runtime(provider=provider)

    with pytest.raises(AIError, match="does not satisfy its schema"):
        ai.execute(AIRequest.prompt("hello", output=AIOutputSpec.schema_of(schema)))

    assert provider.calls == 1


def test_scenario_3_streaming_emits_canonical_events_only() -> None:
    """Provider chunks become canonical events, and never escape as themselves."""
    events = list(runtime().stream(AIRequest.prompt("streaming answer here")))
    kinds = [event.kind for event in events]

    assert kinds[0] is AIEventKind.EXECUTION_STARTED
    assert kinds[-1] is AIEventKind.EXECUTION_COMPLETED
    assert AIEventKind.CONTENT_DELTA in kinds
    deltas = "".join(e.text for e in events if e.kind is AIEventKind.CONTENT_DELTA)
    assert deltas == "streaming answer here"


def test_scenario_4_tool_call() -> None:
    """The model asks; the application decides what the tool means."""
    provider = EchoProvider(tool_calls=(AIToolCall(id="t1", name="lookup", arguments={"q": "x"}),))
    ai = runtime(provider=provider)

    result = ai.execute(AIRequest.prompt(
        "find it", tools=(AITool(name="lookup", description="look something up"),),
    ))

    assert result.finish_reason.value == "tool_calls"
    assert [call.name for call in result.tool_calls] == ["lookup"]


def test_scenario_5_multimodal_needs_no_new_request_model() -> None:
    """An image travels in the same request as text."""
    request = AIRequest(input=AIInput.of(text("what is this"), image("file://x.png")))

    result = runtime().execute(request)

    assert AIContentKind.IMAGE in request.input.kinds()
    assert result.execution.finish_reason.value == "completed"


def test_scenario_5b_a_missing_capability_refuses_before_the_provider() -> None:
    """Refused on capability, with the provider never called."""
    provider = EchoProvider(capability=AICapabilitySet.of(AICapability.TEXT))
    blind = AIProfile(id="blind", provider="echo", model="m1")
    ai = runtime(provider=provider, profiles=(blind,), settings=AISettings(profile_id="blind"))

    with pytest.raises(AICapabilityError, match="vision"):
        ai.execute(AIRequest(input=AIInput.of(image("file://x.png"))))

    assert provider.calls == 0


def test_scenario_6_conversational_session() -> None:
    """History accumulates, and the session's identity is ours."""
    ai = runtime()
    session = ai.session()

    session.execute(AIRequest.prompt("first"))
    session.execute(AIRequest.prompt("second"))

    roles = [message.role for message in session.messages()]
    assert roles == [AIRole.USER, AIRole.ASSISTANT, AIRole.USER, AIRole.ASSISTANT]
    assert session.id


def test_scenario_7_cancellation_is_the_callers_decision() -> None:
    """The caller withdraws; the subsystem propagates and says so."""
    token = CancellationToken()
    token.cancel()

    with pytest.raises(AICancelled):
        runtime().execute(AIRequest.prompt("never sent", cancelled=token))


def test_scenario_8_profile_change_is_one_canonical_answer() -> None:
    """Selection changes, observers are told, nothing executes."""
    seen: list[str] = []
    ai = runtime(profiles=(
        AIProfile(id="fast", provider="echo", model="m1"),
        AIProfile(id="deep", provider="echo", model="m2"),
    ))
    stop = ai.observe(lambda settings: seen.append(settings.profile_id))

    ai.select_profile("deep")

    assert ai.settings.profile_id == "deep"
    assert seen == ["deep"]
    assert ai.execute(AIRequest.prompt("x")).execution.model == "m2"

    stop()
    ai.select_profile("fast")
    assert seen == ["deep"]


def test_scenario_8b_an_unknown_selection_is_refused() -> None:
    with pytest.raises(AISelectionError, match="nothing_like_this"):
        runtime().select_profile("nothing_like_this")


def test_scenario_9_provider_failure_normalizes_and_retries() -> None:
    """Two failures, then success, with the attempts recorded."""
    provider = EchoProvider(fail_times=2)
    result = runtime(provider=provider).execute(AIRequest.prompt("eventually"))

    assert result.execution.attempt_count == 3
    assert [attempt.failed for attempt in result.execution.attempts] == [True, True, False]


def test_scenario_9b_exhausted_retries_raise_a_canonical_failure() -> None:
    provider = EchoProvider(fail_times=9)

    with pytest.raises(AIProviderError):
        runtime(provider=provider).execute(AIRequest.prompt("never"))


def test_scenario_10_a_presentation_consumer_needs_only_the_snapshot() -> None:
    """Everything a future Console projection reads, in one answer."""
    ai = runtime(
        profiles=(AIProfile(id="fast", name="Fast", provider="echo", model="m1"),),
        instructions=(AIInstruction(id="c1", name="Summarise", kind=AIInstructionKind.CONTRACT,
                                    text="Be brief."),),
    )

    snapshot = ai.snapshot()

    assert [profile.label for profile in snapshot.profiles] == ["Fast"]
    assert [instruction.label for instruction in snapshot.instructions] == ["Summarise"]
    assert snapshot.settings.profile_id == "fast"


# -- scenario 11 and the isolation guarantees --------------------------------

def test_scenario_11_two_consumers_share_one_runtime_without_reaching_each_other() -> None:
    """One AI, two callers, independent executions."""
    ai = runtime()
    analyzer_token = CancellationToken()
    analyzer_token.cancel()

    with pytest.raises(AICancelled):
        ai.execute(AIRequest.prompt("analyzer", cancelled=analyzer_token))

    # The other consumer is untouched by the first one's withdrawal.
    assert ai.execute(AIRequest.prompt("workbench")).text == "workbench"


def test_two_runtimes_share_no_mutable_state() -> None:
    """Different installations never share a selection."""
    one = runtime(profiles=(
        AIProfile(id="fast", provider="echo", model="m1"),
        AIProfile(id="deep", provider="echo", model="m2"),
    ))
    two = runtime(profiles=(
        AIProfile(id="fast", provider="echo", model="m1"),
        AIProfile(id="deep", provider="echo", model="m2"),
    ))

    one.select_profile("deep")

    assert one.settings.profile_id == "deep"
    assert two.settings.profile_id == "fast"


def test_an_explicit_request_override_beats_the_default() -> None:
    """A governed operation does not depend on what an operator selected."""
    ai = runtime(profiles=(
        AIProfile(id="fast", provider="echo", model="m1"),
        AIProfile(id="deep", provider="echo", model="m2"),
    ))

    result = ai.execute(AIRequest.prompt("x", profile_id="deep"))

    assert result.execution.model == "m2"
    assert ai.settings.profile_id == "fast"


def test_changing_a_default_cannot_alter_an_already_resolved_execution() -> None:
    """Resolution is a fact; a later selection does not rewrite it."""
    ai = runtime(profiles=(
        AIProfile(id="fast", provider="echo", model="m1"),
        AIProfile(id="deep", provider="echo", model="m2"),
    ))
    resolved = ai.resolve(AIRequest.prompt("x"))

    ai.select_profile("deep")

    assert resolved.profile.id == "fast"
    assert resolved.profile.model == "m1"


def test_resolution_does_not_mutate_the_callers_request() -> None:
    ai = runtime()
    request = AIRequest.prompt("x")

    ai.resolve(request)

    assert request.profile_id == ""
    assert request.instruction is None


def test_a_session_does_not_leak_into_another() -> None:
    ai = runtime()
    first, second = ai.session(), ai.session()

    first.execute(AIRequest.prompt("only mine"))

    assert len(first.messages()) == 2
    assert second.messages() == ()
    assert first.id != second.id


def test_execution_evidence_says_what_actually_ran() -> None:
    ai = runtime(instructions=(AIInstruction(id="c1", kind=AIInstructionKind.CONTRACT,
                                             text="Be brief."),),
                 settings=AISettings(profile_id="fast", instruction_id="c1"))

    info = ai.execute(AIRequest.prompt("x")).execution

    assert info.profile_id == "fast"
    assert info.provider == "echo"
    assert info.model == "m1"
    assert info.instruction_id == "c1"
    assert info.started_at and info.ended_at
    assert info.execution_id


def test_observation_creates_no_second_owner() -> None:
    """An observer is told; it never becomes the answer."""
    ai = runtime(profiles=(
        AIProfile(id="fast", provider="echo", model="m1"),
        AIProfile(id="deep", provider="echo", model="m2"),
    ))
    held: list[AISettings] = []
    ai.observe(held.append)

    ai.select_profile("deep")

    assert held[-1] == ai.settings
    assert ai.settings is ai.snapshot().settings


def test_an_observer_that_raises_does_not_break_the_owner() -> None:
    ai = runtime(profiles=(
        AIProfile(id="fast", provider="echo", model="m1"),
        AIProfile(id="deep", provider="echo", model="m2"),
    ))
    ai.observe(lambda _settings: (_ for _ in ()).throw(RuntimeError("listener")))
    seen: list[str] = []
    ai.observe(lambda settings: seen.append(settings.profile_id))

    ai.select_profile("deep")

    assert ai.settings.profile_id == "deep"
    assert seen == ["deep"]


def test_a_runtime_cannot_start_holding_a_selection_it_does_not_offer() -> None:
    registry = AIRegistry(
        profiles=(AIProfile(id="fast", provider="echo", model="m1"),),
        providers=(EchoProvider(),),
    )

    with pytest.raises(AISelectionError):
        AI(registry=registry, settings=AISettings(profile_id="absent"))


def test_an_empty_request_is_refused_before_a_provider() -> None:
    provider = EchoProvider()

    with pytest.raises(AIError, match="something to send"):
        runtime(provider=provider).execute(AIRequest(input=AIInput()))

    assert provider.calls == 0


def test_policy_narrows_provider_capability_and_never_widens_it() -> None:
    """An installation cannot grant what the model cannot do."""
    restricted = AIProfile(
        id="restricted", provider="echo", model="m1",
        policy=AICapabilitySet.of(AICapability.TEXT, AICapability.VISION),
    )
    ai = runtime(
        provider=EchoProvider(capability=AICapabilitySet.of(AICapability.TEXT)),
        profiles=(restricted,),
        settings=AISettings(profile_id="restricted"),
    )

    effective = ai.capabilities("restricted")

    assert effective.has(AICapability.TEXT)
    assert not effective.has(AICapability.VISION)
