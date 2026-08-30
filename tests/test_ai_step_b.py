"""The pre-cutover obligations, exercised.

One test per behaviour the architecture contract asked for, written against the
public surface rather than the internals, so a later decomposition that keeps
the contract does not have to rewrite these.
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.ai import (
    AI,
    AICapability,
    AICapabilitySet,
    AIConfigurationError,
    AIExecutionPolicy,
    AIInput,
    AIProfile,
    AIRegistry,
    AIRequest,
    AIResult,
    AISettings,
    AIToolCall,
    AIToolResult,
    ConfiguredProvider,
    EchoProvider,
    ExecutionBudget,
    ExecutionState,
    FallbackPolicy,
    OutputNormalizer,
    ProviderCall,
    ProviderReply,
    ReplayClassification,
    ReplayFacts,
    ReplaySafety,
    ToolCorrectionPolicy,
    TransportRetryPolicy,
    ValidationCorrectionPolicy,
    ai_from_ctx,
    configured_providers_from_ctx,
    image,
)
from rey_lib.ai.errors import AIError, AIProviderError, AIToolError
from rey_lib.ai.providers.base import AIProvider


def runtime(
    *,
    provider: AIProvider | None = None,
    providers: tuple[AIProvider, ...] = (),
    profiles: tuple[AIProfile, ...] = (),
    policy: AIExecutionPolicy | None = None,
) -> AI:
    """One runtime, built from explicit resolved inputs and no context."""
    built = providers or (provider or EchoProvider(),)
    registry = AIRegistry(
        profiles=profiles
        or (AIProfile(id="fast", provider=built[0].name, model="m1"),),
        providers=built,
    )
    return AI(
        registry=registry,
        settings=AISettings(profile_id=profiles[0].id if profiles else "fast"),
        **({"policy": policy} if policy else {}),
    )


# -- obligation 1: ExecutionState -------------------------------------------

def test_execution_state_accumulates_separately_from_the_resolved_request() -> None:
    """History accrues on state; the resolved decision never changes."""
    ai = runtime()
    request = ai.resolve(AIRequest.prompt("hello"))
    state = ExecutionState(execution_id="e1")

    state.requested_tools((AIToolCall(id="c1", name="t"),))
    state.accepted_tool_results((AIToolResult(call_id="c1", value=7),))

    assert len(state.accumulated()) == 2
    assert state.has_history()
    assert request.input.content[0].value == "hello"


def test_usage_accumulates_across_turns_rather_than_being_replaced() -> None:
    """A multi-turn run reports the whole execution's cost."""
    from rey_lib.ai.results import AIUsage

    state = ExecutionState(execution_id="e1")
    state.add_usage(AIUsage(input_tokens=3, output_tokens=1))
    state.add_usage(AIUsage(input_tokens=4, output_tokens=2))

    assert state.usage.input_tokens == 7
    assert state.usage.total_tokens == 10


# -- obligation 3: the control domains are independent ----------------------

def test_output_error_may_not_be_made_transport_retryable() -> None:
    """The guard that predates this work, kept."""
    from rey_lib.ai.errors import AIOutputError

    with pytest.raises(ValueError, match="must not be transport-retryable"):
        TransportRetryPolicy(retry_on=(AIOutputError,))


def test_each_domain_refuses_a_nonsense_budget() -> None:
    """A budget that cannot be satisfied is refused at construction."""
    with pytest.raises(ValueError):
        ExecutionBudget(max_turns=0)
    with pytest.raises(ValueError):
        TransportRetryPolicy(attempts=0)
    with pytest.raises(ValueError):
        ToolCorrectionPolicy(max_corrections=-1)
    with pytest.raises(ValueError):
        ValidationCorrectionPolicy(max_corrections=-1)


def test_transport_backoff_is_zero_until_configured() -> None:
    """A caller wanting no backoff pays nothing for the mechanism."""
    assert TransportRetryPolicy().delay_before(3) == 0.0
    stepped = TransportRetryPolicy(backoff_seconds=0.5, backoff_factor=2.0)
    assert stepped.delay_before(1) == 0.0
    assert stepped.delay_before(2) == 0.5
    assert stepped.delay_before(3) == 1.0


# -- obligation 6: replay safety is an authorization predicate --------------

def test_replay_is_refused_after_emission_has_begun() -> None:
    """A turn already observed downstream is not silently repeated."""
    safety = ReplaySafety()
    assert not safety.permits(ReplayFacts(response_started=True))
    assert "begun emitting" in safety.refusal(ReplayFacts(response_started=True))


def test_replay_is_refused_for_a_stateful_request_and_when_unknown() -> None:
    """Silence is not permission."""
    safety = ReplaySafety()
    assert not safety.permits(ReplayFacts(stateful=True))
    assert not safety.permits(ReplayFacts(classification=ReplayClassification.UNKNOWN))
    assert safety.permits(ReplayFacts(classification=ReplayClassification.SAFE))


def test_explicit_authority_is_required_to_replay_unsafe_work() -> None:
    """Deciding to retry never by itself authorises unsafe replay."""
    assert ReplaySafety(approve_unsafe_replay=True).permits(
        ReplayFacts(response_started=True, stateful=True),
    )


def test_a_retryable_failure_is_not_retried_when_replay_is_refused() -> None:
    """Policy and legality are two questions, and both must say yes."""
    provider = EchoProvider(
        fail_times=1, replay=ReplayFacts(response_started=True),
    )
    ai = runtime(provider=provider, policy=AIExecutionPolicy(
        transport=TransportRetryPolicy(attempts=3),
    ))

    with pytest.raises(AIError):
        ai.execute(AIRequest.prompt("hello"))

    assert provider.calls == 1


def test_an_adapter_declares_whether_its_sdk_retries_underneath() -> None:
    """The nested-retry contract is stated, not assumed."""
    assert EchoProvider().retries_internally is False


# -- obligation 5: fallback is owned, and never silent ----------------------

class _AlwaysFails(EchoProvider):
    """An adapter that cannot answer, to make the next one necessary."""

    @property
    def name(self) -> str:
        return "broken"

    def invoke(self, call: ProviderCall, **kwargs: Any) -> ProviderReply:
        self.calls += 1
        raise AIProviderError("nothing here answers")


def test_fallback_moves_to_the_next_provider_and_announces_it() -> None:
    """Model-invisible, but never observer-invisible."""
    broken, working = _AlwaysFails(), EchoProvider()
    ai = runtime(
        providers=(broken, working),
        profiles=(AIProfile(id="p", provider="broken", model="m1"),),
        policy=AIExecutionPolicy(
            transport=TransportRetryPolicy(attempts=1),
            fallback=FallbackPolicy(sequence=("echo",)),
        ),
    )

    events = list(ai.stream(AIRequest.prompt("hello")))
    transitions = [e for e in events if e.kind.value == "provider_changed"]

    assert broken.calls == 1
    assert working.calls == 1
    assert len(transitions) == 1
    assert transitions[0].metadata == {"from": "broken", "to": "echo"}


def test_fallback_sequence_reports_exhaustion() -> None:
    """A spent sequence says so rather than cycling."""
    policy = FallbackPolicy(sequence=("a", "b"))
    assert policy.after(()) == "a"
    assert policy.after(("a",)) == "b"
    assert policy.exhausted(("a", "b"))


# -- obligation 7: multimodal content survives the provider boundary --------

class _AsksOnce(EchoProvider):
    """Asks for tools on the first turn, then answers.

    A model that asks forever is a real case, and the turn budget covers it --
    but a continuation that *completes* needs a provider that stops asking.
    """

    def invoke(self, call: ProviderCall, **kwargs: Any) -> ProviderReply:
        reply = super().invoke(call, **kwargs)
        self._tool_calls = ()
        return reply


class _Recording(EchoProvider):
    """An adapter that keeps the call it was handed."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.seen: ProviderCall | None = None

    def invoke(self, call: ProviderCall, **kwargs: Any) -> ProviderReply:
        self.seen = call
        return super().invoke(call, **kwargs)


class _RecordingAsksOnce(_Recording):
    """Records the call, and stops asking for tools after the first turn."""

    def invoke(self, call: ProviderCall, **kwargs: Any) -> ProviderReply:
        reply = super().invoke(call, **kwargs)
        self._tool_calls = ()
        return reply


def test_an_image_reaches_the_adapter_as_an_image() -> None:
    """The defect this repairs: a declared vision capability, then '[image: …]'."""
    provider = _Recording()
    ai = runtime(provider=provider)

    ai.execute(AIRequest(input=AIInput.of(image("file://x.png", "image/png"))))

    parts = [part for message in provider.seen.messages for part in message.content]
    kinds = {part.kind.value for part in parts}
    assert "image" in kinds
    assert all(not str(part.value).startswith("[image:") for part in parts)


# -- obligations 4 and 3: continuation, under its own budgets ---------------

def test_a_tool_continuation_runs_and_returns_a_final_answer() -> None:
    """Accept results, append turns, ask again, stop."""
    calls = (AIToolCall(id="c1", name="lookup", arguments={"q": "x"}),)
    provider = _AsksOnce(tool_calls=calls, reply_with="done")
    ai = runtime(provider=provider)

    result = ai.execute(
        AIRequest.prompt("hello", tool_runner=lambda call: AIToolResult(
            call_id=call.id, value={"answer": 42},
        )),
    )

    assert isinstance(result, AIResult)
    assert result.tool_calls
    assert result.tool_results[0].value == {"answer": 42}


def test_without_a_runner_the_calls_are_handed_back_rather_than_failing() -> None:
    """Deciding what to do with a requested call is the caller's."""
    calls = (AIToolCall(id="c1", name="lookup"),)
    ai = runtime(provider=EchoProvider(tool_calls=calls))

    result = ai.execute(AIRequest.prompt("hello"))

    assert result.execution.finish_reason.value == "tool_calls"
    assert result.tool_calls == calls


def test_a_failing_tool_spends_the_correction_budget_and_then_refuses() -> None:
    """Tool correction is its own budget, and it can run out."""
    calls = (AIToolCall(id="c1", name="lookup"),)
    ai = runtime(
        provider=EchoProvider(tool_calls=calls),
        policy=AIExecutionPolicy(tool_correction=ToolCorrectionPolicy(max_corrections=0)),
    )

    with pytest.raises(AIToolError, match="tool-correction budget"):
        ai.execute(
            AIRequest.prompt("hello", tool_runner=lambda call: AIToolResult(
                call_id=call.id, failed=True, message="no",
            )),
        )


def test_the_turn_budget_bounds_a_model_that_never_stops_asking() -> None:
    """A continuation that would not terminate is stopped by its own budget."""
    calls = (AIToolCall(id="c1", name="lookup"),)
    ai = runtime(
        provider=EchoProvider(tool_calls=calls),
        policy=AIExecutionPolicy(budget=ExecutionBudget(max_turns=3)),
    )

    with pytest.raises(AIToolError, match="execution budget"):
        ai.execute(
            AIRequest.prompt("hello", tool_runner=lambda call: AIToolResult(
                call_id=call.id, value=1,
            )),
        )


def test_continuation_does_not_re_resolve_settings_changed_mid_flight() -> None:
    """The resolved decision is immutable for the execution that holds it."""
    calls = (AIToolCall(id="c1", name="t"),)
    provider = _RecordingAsksOnce(tool_calls=calls, reply_with="done")
    ai = runtime(
        provider=provider,
        profiles=(
            AIProfile(id="first", provider="echo", model="m1"),
            AIProfile(id="second", provider="echo", model="m2"),
        ),
    )

    def switch(call: AIToolCall) -> AIToolResult:
        ai.select_profile("second")
        return AIToolResult(call_id=call.id, value=1)

    result = ai.execute(AIRequest.prompt("hello", tool_runner=switch))

    assert result.execution.model == "m1"
    assert provider.seen.model == "m1"
    assert ai.settings.profile_id == "second"


# -- obligation 9: normalization removes only what Rey introduced -----------

def test_normalization_removes_only_a_rey_introduced_envelope() -> None:
    """The envelope is known because resolution recorded it."""
    from dataclasses import replace
    from rey_lib.ai.results import AIOutput

    ai = runtime()
    request = ai.resolve(AIRequest.prompt("hi"))
    wrapped = replace(request, envelope_key="response")

    normalized = OutputNormalizer().normalize(
        wrapped, AIOutput.of_value({"response": {"a": 1}}),
    )

    assert normalized.value == {"a": 1}


def test_a_caller_schema_using_result_or_content_is_left_intact() -> None:
    """No key-name heuristics: unrecorded means untouched."""
    from rey_lib.ai.results import AIOutput

    ai = runtime()
    request = ai.resolve(AIRequest.prompt("hi"))
    payload = {"result": {"content": "mine"}}

    normalized = OutputNormalizer().normalize(request, AIOutput.of_value(payload))

    assert normalized.value == payload


def test_representation_is_a_media_type_not_a_new_output_kind() -> None:
    """Markdown is text with a representation, not a structural distinction."""
    from rey_lib.ai.requests import AIOutputKind, AIOutputSpec

    spec = AIOutputSpec.markdown()
    assert spec.kind is AIOutputKind.TEXT
    assert spec.media_type == "text/markdown"

    ai = runtime(provider=EchoProvider(reply_with="# Report"))
    result = ai.execute(AIRequest.prompt("hi", output=spec))

    assert result.text == "# Report"
    assert result.media_type == "text/markdown"


def test_validation_correction_is_off_until_a_runtime_asks_for_it() -> None:
    """A default runtime keeps the original refusal behaviour."""
    assert ValidationCorrectionPolicy().max_corrections == 0


# -- obligation 10: ctx is construction-only --------------------------------

class _Ctx:
    """Just enough of an application context to construct from."""

    def __init__(self) -> None:
        self.llm = {
            "fast": {
                "provider": "echo",
                "model": "m1",
                "api_key": "env.ANTHROPIC_API_KEY",
                "timeout": 30,
            },
        }


def test_configuration_is_normalized_out_of_ctx_into_rey_owned_state() -> None:
    """Only fields the runtime contract names are carried forward."""
    configured = configured_providers_from_ctx(_Ctx())

    assert len(configured) == 1
    assert configured[0] == ConfiguredProvider(
        id="fast",
        provider="echo",
        model="m1",
        credential_ref="env.ANTHROPIC_API_KEY",
        timeout_seconds=30.0,
    )


def test_a_credential_reference_is_carried_never_a_resolved_credential() -> None:
    """ConfiguredProvider describes how to authenticate; the adapter resolves."""
    configured = configured_providers_from_ctx(_Ctx())[0]

    assert configured.credential_ref == "env.ANTHROPIC_API_KEY"
    assert not hasattr(configured, "api_key")


def test_the_built_runtime_retains_no_reference_to_ctx() -> None:
    """ctx is bootstrap input, and is let go once construction is done."""
    ctx = _Ctx()
    resolved: list[ConfiguredProvider] = []

    def factory(configuration: ConfiguredProvider) -> AIProvider:
        resolved.append(configuration)
        # A factory builds one *configured* provider, so the adapter carries
        # that identity rather than its type.
        return EchoProvider(name=configuration.id)

    ai = ai_from_ctx(
        ctx,
        adapters={"echo": factory},
        profiles=(AIProfile(id="fast", name="Fast"),),
    )

    assert resolved and resolved[0].credential_ref == "env.ANTHROPIC_API_KEY"
    assert ctx not in vars(ai).values()
    assert not any("ctx" in name for name in vars(ai))
    assert ai.execute(AIRequest.prompt("hello", profile_id="fast")).text


def test_a_profile_is_linked_to_its_configured_provider_by_identity() -> None:
    """The public projection references configuration; it does not carry it."""
    ai = ai_from_ctx(
        _Ctx(),
        adapters={"echo": lambda c: EchoProvider(name=c.id)},
        profiles=(AIProfile(id="fast", name="Fast"),),
    )

    profile = ai.profiles()[0]

    assert profile.configured_provider_id == "fast"
    assert profile.provider == "echo"
    assert not hasattr(profile, "credential_ref")
    assert not hasattr(profile, "provider_capability")


def test_an_unbuildable_provider_is_refused_at_construction() -> None:
    """Configuration naming an adapter nobody supplies fails now, not later."""
    with pytest.raises(AIConfigurationError, match="no adapter factory builds"):
        ai_from_ctx(_Ctx(), adapters={})


def test_missing_configuration_is_refused_rather_than_guessed() -> None:
    """No path-guessing fallback when ctx carries no AI configuration."""

    class _Empty:
        pass

    with pytest.raises(AIConfigurationError, match="ctx.llm is not set"):
        configured_providers_from_ctx(_Empty())


def test_capability_truth_is_the_adapters_not_the_profiles() -> None:
    """A profile cannot advertise what its adapter does not implement."""
    ai = runtime(
        provider=EchoProvider(capability=AICapabilitySet.of(AICapability.TEXT)),
        profiles=(AIProfile(
            id="p", provider="echo", model="m1",
            policy=AICapabilitySet.of(AICapability.TEXT, AICapability.VISION),
        ),),
    )

    assert ai.capabilities("p").has(AICapability.TEXT)
    assert not ai.capabilities("p").has(AICapability.VISION)


# -- the shape production actually configures --------------------------------
#
# These exist because the unit fixtures above use a mapping, and production
# holds the estate's named collection: a list of records each carrying a name.
# Every test passed while every launch failed, because the fixtures invented a
# configuration shape rather than using the configured one.

def _configured_llm() -> list[Any]:
    """`ctx.llm` exactly as configuration finalizes it.

    Two entries naming two different providers, as the Console's own
    installation does. One adapter is registered per provider, so two entries
    sharing a provider is a separate limitation and not what this exercises.
    """
    from argparse import Namespace

    return [
        Namespace(
            name="anthropic", provider="echo", model="claude-sonnet-4-6",
            api_key="env.ANTHROPIC_API_KEY",
            profile_access=Namespace(allowed=["redacted"], default="redacted"),
        ),
        Namespace(
            name="primary", provider="ollama", model="gpt-4o",
            api_key="env.OPENAI_API_KEY",
            profile_access=Namespace(allowed=["redacted", "unredacted"],
                                     default="redacted"),
        ),
    ]


def test_the_configured_named_collection_is_read() -> None:
    """A list of named entries, which is what ctx.llm holds."""
    from argparse import Namespace

    configured = configured_providers_from_ctx(Namespace(llm=_configured_llm()))

    assert [entry.id for entry in configured] == ["anthropic", "primary"]
    assert configured[0].credential_ref == "env.ANTHROPIC_API_KEY"
    assert configured[1].model == "gpt-4o"


def test_an_entry_without_a_name_is_refused() -> None:
    """A named collection whose entry has no name selects nothing."""
    from argparse import Namespace

    with pytest.raises(AIConfigurationError, match="carries no name"):
        configured_providers_from_ctx(
            Namespace(llm=[Namespace(provider="echo", model="m")]),
        )


def test_a_shape_that_is_neither_is_refused_rather_than_guessed() -> None:
    """The fault that broke every launch: walking a shape nobody configured."""
    from argparse import Namespace

    with pytest.raises(AIConfigurationError, match="named collection"):
        configured_providers_from_ctx(Namespace(llm="not-a-collection"))


def test_bootstrap_builds_a_shared_ai_from_the_configured_shape() -> None:
    """The bootstrap contract, against the real configuration shape."""
    from argparse import Namespace

    from rey_lib.config.bootstrap import _open_ai

    ai = _open_ai(Namespace(llm=_configured_llm(), env=[]))

    assert ai is not None
    assert [profile.id for profile in ai.profiles()] == ["anthropic", "primary"]
    # The declared access policy travels with the profile, into AI-owned state.
    assert ai.profile("primary").access_policy()["allowed"] == [
        "redacted", "unredacted",
    ]


def test_bootstrap_returns_none_when_no_ai_is_configured() -> None:
    """Absent AI is an ordinary state, and builds nothing."""
    from argparse import Namespace

    from rey_lib.config.bootstrap import _open_ai

    assert _open_ai(Namespace()) is None
    assert _open_ai(Namespace(llm=[])) is None


def test_bootstrap_raises_when_configured_ai_cannot_be_built() -> None:
    """Configured and broken is not reported as absent.

    An installation that names providers has asked for an AI. Answering
    "unavailable" would turn a configuration defect into silent capability loss.
    """
    from argparse import Namespace

    from rey_lib.config.bootstrap import _open_ai
    from rey_lib.errors.error_utils import ConfigError

    with pytest.raises(ConfigError, match="configures AI but one could not be built"):
        _open_ai(Namespace(
            llm=[Namespace(name="x", provider="no-such-adapter", model="m")],
            env=[],
        ))
