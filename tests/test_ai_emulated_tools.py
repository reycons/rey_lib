"""Tools carried over an engine that cannot call them.

The claim under test is one sentence: **above ``CanonicalToolLoop``, a native
engine and an emulated one are indistinguishable.** Everything here either
proves that, or proves one of the boundaries that keeps it honest --

    resolution decides the mechanism, execution obeys it;
    a provider's capability set stays raw provider truth;
    an unreadable decision is corrected, never counterfeited as a tool failure.

Nothing in here is attached to a real engine, and nothing names ``tree_expand``:
the mechanism is generic over any tool that declares an input schema, and the
Console's tree tool is merely the first one to use it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from rey_lib.ai import (
    AI,
    AICapability,
    AICapabilityError,
    AICapabilitySet,
    AIOutputSpec,
    AIProfile,
    AIRegistry,
    AIRequest,
    AISettings,
    AITool,
    AIToolCall,
)
from rey_lib.ai.errors import AIOutputError
from rey_lib.ai.policies import ReplayClassification, ReplayFacts
from rey_lib.ai.providers.base import AIProvider, ProviderCall, ProviderReply
from rey_lib.ai.results import AIUsage
from rey_lib.ai.tools import AIToolResult
from rey_lib.ai.turn_strategy import (
    EmulatedTurnStrategy,
    NativeTurnStrategy,
    decision_schema,
    resolve_tool_mechanism,
)

#: An engine that shapes output and calls nothing -- the local case.
LOCAL = AICapabilitySet.of(
    AICapability.TEXT,
    AICapability.STREAMING,
    AICapability.STRUCTURED_OUTPUT,
)
#: An engine with its own tool protocol.
NATIVE = AICapabilitySet.of(
    AICapability.TEXT,
    AICapability.STREAMING,
    AICapability.STRUCTURED_OUTPUT,
    AICapability.NATIVE_TOOLS,
)
#: An engine that can only talk.
PLAIN = AICapabilitySet.of(AICapability.TEXT)

EXPAND = AITool(
    name="expand",
    description="List the children of one node.",
    input_schema={
        "type": "object",
        "properties": {"node_key": {"type": "string"}},
        "required": ["node_key"],
    },
)
COUNT = AITool(
    name="count",
    description="Count something.",
    input_schema={
        "type": "object",
        "properties": {"depth": {"type": "integer"}},
        "required": ["depth"],
    },
)
UNDECLARED = AITool(name="undeclared", description="Declares no shape.")


class _Scripted(AIProvider):
    """Answers with what a test wrote for it, recording what it was asked.

    A double rather than a stub: the shaped ``ProviderCall`` is most of what
    the emulated mechanism produces, so a provider that discarded it could not
    prove anything about the shaping.
    """

    def __init__(self, *replies: str, capability: AICapabilitySet = LOCAL) -> None:
        self._replies = list(replies)
        self._capability = capability
        self.calls: list[ProviderCall] = []
        self.streamed: list[str] = []

    @property
    def name(self) -> str:
        return "local"

    def capability_for(self, model: str) -> AICapabilitySet:  # noqa: ARG002
        return self._capability

    def replay_facts(self, failure: BaseException) -> ReplayFacts:  # noqa: ARG002
        return ReplayFacts(classification=ReplayClassification.SAFE)

    def invoke(
        self,
        call: ProviderCall,
        *,
        cancelled: Any = None,  # noqa: ARG002
        on_text: Any = None,
    ) -> ProviderReply:
        self.calls.append(call)
        said = self._replies.pop(0) if self._replies else ""
        if on_text is not None:
            # Reported the way a streaming adapter reports it. Whether any of
            # it reaches a reader is the mechanism's to decide, not this one's.
            self.streamed.append(said)
            on_text(said)
        return ProviderReply(text=said, model=call.model, usage=AIUsage())


def runtime(provider: _Scripted) -> AI:
    """One runtime over this provider, built the way a bootstrap builds one."""
    return AI(
        registry=AIRegistry(
            profiles=(AIProfile(id="local", name="Local", provider="local", model="m"),),
            providers=(provider,),
        ),
        settings=AISettings(profile_id="local"),
    )


def decision(**fields: Any) -> str:
    return json.dumps(fields)


# -- the mechanism is resolved, and resolution is the only place it is --------

class TestResolution:
    """Which mechanism can carry these tools, decided once."""

    def test_a_native_engine_keeps_its_own_protocol(self) -> None:
        chosen = resolve_tool_mechanism(NATIVE, (EXPAND,))

        assert isinstance(chosen.strategy, NativeTurnStrategy)

    def test_a_structured_engine_carries_tools_by_emulation(self) -> None:
        """The whole point: no native tool calling, and still admitted."""
        chosen = resolve_tool_mechanism(LOCAL, (EXPAND,))

        assert isinstance(chosen.strategy, EmulatedTurnStrategy)

    def test_a_tool_declaring_no_shape_cannot_be_emulated(self) -> None:
        """The only eligibility condition there is.

        Not a name list: a tool is representable in a decision because it
        declares an input schema, which is a property it already carries. This
        is what stops STRUCTURED_OUTPUT from quietly meaning "supports any
        tool".
        """
        with pytest.raises(AICapabilityError) as refused:
            resolve_tool_mechanism(LOCAL, (EXPAND, UNDECLARED))

        assert "undeclared" in str(refused.value)

    def test_a_plain_engine_is_refused(self) -> None:
        with pytest.raises(AICapabilityError):
            resolve_tool_mechanism(PLAIN, (EXPAND,))

    def test_an_ask_offering_nothing_is_always_satisfiable(self) -> None:
        assert isinstance(
            resolve_tool_mechanism(PLAIN, ()).strategy, NativeTurnStrategy,
        )

    def test_emulation_states_a_correction_allowance_and_native_states_none(
        self,
    ) -> None:
        """A local engine misses a shape and recovers when told; a native one
        keeps whatever posture the runtime was built with, untouched."""
        assert resolve_tool_mechanism(LOCAL, (EXPAND,)).validation_correction \
            .max_corrections == 2
        assert resolve_tool_mechanism(NATIVE, (EXPAND,)).validation_correction is None

    def test_deciding_builds_no_request(self) -> None:
        """The surface that says whether a run is possible asks this same
        function. Painting a banner must not resolve an execution."""
        chosen = resolve_tool_mechanism(LOCAL, (EXPAND,))

        assert not hasattr(chosen, "input")
        assert not hasattr(chosen, "profile")

    def test_readiness_and_resolution_reach_the_same_verdict(self) -> None:
        provider = _Scripted(capability=LOCAL)
        ai = runtime(provider)

        asked = ai.tool_mechanism("local", (EXPAND,))
        resolved = ai.resolve(
            AIRequest(input=AIRequest.prompt("go").input, tools=(EXPAND,)),
        )

        assert type(asked.strategy) is type(resolved.tool_mechanism.strategy)


# -- the envelope ------------------------------------------------------------

class TestTheDecisionSchema:
    """What one emulated turn must satisfy."""

    def test_a_branch_per_offered_tool_and_one_for_the_answer(self) -> None:
        schema = decision_schema(
            ({"name": "expand", "input_schema": EXPAND.input_schema},
             {"name": "count", "input_schema": COUNT.input_schema}),
        )

        assert len(schema["oneOf"]) == 3

    def test_each_tool_is_bound_to_its_own_arguments(self) -> None:
        """The regression test for the binding.

        An enum of names beside one ``arguments`` schema would admit this
        document: both halves are independently valid and only their pairing is
        wrong. The ``const`` on ``tool`` is what refuses it.
        """
        jsonschema = pytest.importorskip("jsonschema")
        schema = decision_schema(
            ({"name": "expand", "input_schema": EXPAND.input_schema},
             {"name": "count", "input_schema": COUNT.input_schema}),
        )
        validator = jsonschema.Draft7Validator(schema)

        assert validator.is_valid(
            {"kind": "tool_call", "tool": "expand", "arguments": {"node_key": "1"}},
        )
        assert validator.is_valid(
            {"kind": "tool_call", "tool": "count", "arguments": {"depth": 2}},
        )
        # Tool A's name carrying tool B's arguments.
        assert not validator.is_valid(
            {"kind": "tool_call", "tool": "expand", "arguments": {"depth": 2}},
        )
        assert not validator.is_valid(
            {"kind": "tool_call", "tool": "count", "arguments": {"node_key": "1"}},
        )

    def test_a_text_ask_answers_with_a_string(self) -> None:
        schema = decision_schema(({"name": "expand", "input_schema": {}},))

        answer = schema["oneOf"][-1]
        assert answer["properties"]["content"] == {"type": "string"}

    def test_a_structured_ask_keeps_its_own_contract(self) -> None:
        """Two contracts meet in the envelope, and the ask's is not discarded.

        Without this the engine is asked for a string and the ask's shape is
        only checked afterwards -- enforcement at generation thrown away for
        exactly the engines that need it most.
        """
        contract = {"type": "object", "required": ["total"]}
        schema = decision_schema(
            ({"name": "expand", "input_schema": {}},), answer_schema=contract,
        )

        assert schema["oneOf"][-1]["properties"]["content"] == contract

    def test_the_mechanism_names_no_tool(self) -> None:
        """Generic over offered tools, provably.

        An allowlist inside the strategy would make "the scope is one tool"
        true of the implementation rather than of what is wired up.
        """
        from pathlib import Path

        import rey_lib.ai.turn_strategy as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "tree_expand" not in source


# -- one turn, read ----------------------------------------------------------

class TestReadingADecision:
    """What comes back, in the subsystem's terms."""

    def test_a_tool_call_becomes_one_ai_tool_call(self) -> None:
        read = EmulatedTurnStrategy().read(ProviderReply(
            text=decision(kind="tool_call", tool="expand",
                          arguments={"node_key": "7"}),
        ))

        assert len(read.tool_calls) == 1
        assert read.tool_calls[0].name == "expand"
        assert read.tool_calls[0].arguments == {"node_key": "7"}
        assert read.tool_calls[0].id

    def test_an_answer_ends_the_loop(self) -> None:
        """No tool calls is how ``CanonicalToolLoop`` knows to stop, and it is
        the same signal a native reply gives."""
        read = EmulatedTurnStrategy().read(ProviderReply(
            text=decision(kind="answer", content="the answer"),
        ))

        assert read.tool_calls == ()
        assert read.text == "the answer"

    def test_a_structured_answer_arrives_as_a_value(self) -> None:
        """The output parser prefers a value over text. Handing it only text
        would decode what is already decoded."""
        read = EmulatedTurnStrategy().read(ProviderReply(
            text=decision(kind="answer", content={"total": 3}),
        ))

        assert read.value == {"total": 3}

    def test_prose_around_the_object_is_tolerated_once(self) -> None:
        read = EmulatedTurnStrategy().read(ProviderReply(
            text='Sure! {"kind": "answer", "content": "hi"} Hope that helps.',
        ))

        assert read.text == "hi"

    @pytest.mark.parametrize(
        "said",
        ["", "not json at all", '{"kind": "shrug"}',
         '{"kind": "tool_call", "tool": "expand"}'],
    )
    def test_an_unreadable_decision_refuses_as_invalid_output(self, said: str) -> None:
        """``AIOutputError`` specifically, because that is what the correction
        path catches. Any other error would end the execution instead."""
        with pytest.raises(AIOutputError):
            EmulatedTurnStrategy().read(ProviderReply(text=said))

    def test_a_native_reply_is_returned_as_given(self) -> None:
        reply = ProviderReply(
            text="hello", tool_calls=(AIToolCall(id="1", name="expand"),),
        )

        assert NativeTurnStrategy().read(reply) is reply


class TestShapingATurn:
    """What the provider is asked for."""

    def test_a_native_call_is_passed_through_untouched(self) -> None:
        """Identity, which is the regression test for every provider that
        already worked."""
        call = ProviderCall(model="m", messages=(), tools=({"name": "expand"},))

        assert NativeTurnStrategy().shape(call) is call

    def test_an_emulated_call_carries_the_envelope_and_offers_no_tools(self) -> None:
        """The adapter is told about no tools, because it cannot use them --
        it would either ignore them or fail on them."""
        shaped = EmulatedTurnStrategy().shape(ProviderCall(
            model="m", messages=(),
            tools=({"name": "expand", "description": "",
                    "input_schema": EXPAND.input_schema},),
        ))

        assert shaped.tools == ()
        assert shaped.json_output is True
        assert "oneOf" in shaped.schema

    def test_the_protocol_is_stated_to_the_engine(self) -> None:
        shaped = EmulatedTurnStrategy().shape(ProviderCall(
            model="m", messages=(),
            tools=({"name": "expand", "description": "Children of a node.",
                    "input_schema": EXPAND.input_schema},),
        ))

        said = "\n".join(
            str(part.value)
            for message in shaped.messages for part in message.content
        )
        assert "expand" in said
        assert "tool_call" in said

    def test_what_the_model_asked_for_stays_visible_to_it(self) -> None:
        """A tool-call turn carries no content, so an adapter that renders text
        drops it -- leaving the engine an answer to a question it has no record
        of asking. The mechanism renders its own decisions back.
        """
        from rey_lib.ai.content import AIMessage, AIRole

        shaped = EmulatedTurnStrategy().shape(ProviderCall(
            model="m",
            messages=(
                AIMessage.asked_for_tools(
                    (AIToolCall(id="1", name="expand",
                                arguments={"node_key": "7"}),),
                ),
            ),
            tools=({"name": "expand", "description": "",
                    "input_schema": EXPAND.input_schema},),
        ))

        assistant = [
            part.value
            for message in shaped.messages if message.role is AIRole.ASSISTANT
            for part in message.content
        ]
        assert any("node_key" in str(said) for said in assistant)

    def test_the_ask_is_the_last_thing_the_model_reads(self) -> None:
        """The protocol is a statement about shape, not about what to do.

        Stated last it outranked the question: asked to say hi about a tree
        node, the model read a tool manual as its most recent instruction and
        enumerated the tree until the budget was spent. It belongs with the
        other standing instructions, before the first turn that is not one.
        """
        from rey_lib.ai.content import AIMessage, AIRole, text as said_text

        shaped = EmulatedTurnStrategy().shape(ProviderCall(
            model="m",
            messages=(
                AIMessage(role=AIRole.SYSTEM, content=(said_text("a contract"),)),
                AIMessage(role=AIRole.USER, content=(said_text("say hi"),)),
            ),
            tools=({"name": "expand", "description": "",
                    "input_schema": EXPAND.input_schema},),
        ))

        last = shaped.messages[-1]
        assert last.role is AIRole.USER
        assert "say hi" in str(last.content[0].value)
        # And it is still told the protocol, before that.
        assert any(
            "tool_call" in str(part.value)
            for message in shaped.messages[:-1] for part in message.content
        )

    def test_it_is_told_what_it_has_left(self) -> None:
        """An engine that cannot see a budget spends it.

        Given a tool and told to answer, a local model read its way down a tree
        until the execution budget was gone and answered nothing. It was not
        disobeying -- it had no way of knowing there was a limit to work
        within.
        """
        from rey_lib.ai.content import AIMessage

        shaped = EmulatedTurnStrategy(max_tool_calls=7).shape(ProviderCall(
            model="m",
            messages=(
                AIMessage.asked_for_tools(
                    (AIToolCall(id="1", name="expand"),),
                ),
                AIMessage.tool_answer("1", {"children": []}),
            ),
            tools=({"name": "expand", "description": "",
                    "input_schema": EXPAND.input_schema},),
        ))

        said = "\n".join(
            str(part.value)
            for message in shaped.messages for part in message.content
        )
        assert "used 1 of 7" in said
        assert "6 left" in said

    def test_a_spent_budget_is_told_to_answer_from_what_it_has(self) -> None:
        from rey_lib.ai.content import AIMessage

        shaped = EmulatedTurnStrategy(max_tool_calls=1).shape(ProviderCall(
            model="m",
            messages=(
                AIMessage.asked_for_tools((AIToolCall(id="1", name="expand"),)),
            ),
            tools=({"name": "expand", "description": "",
                    "input_schema": EXPAND.input_schema},),
        ))

        said = "\n".join(
            str(part.value)
            for message in shaped.messages for part in message.content
        )
        assert "no tool calls left" in said

    def test_no_budget_says_nothing_about_one(self) -> None:
        """A caller with no budget to state must not have one invented for it."""
        shaped = EmulatedTurnStrategy().shape(ProviderCall(
            model="m", messages=(),
            tools=({"name": "expand", "description": "",
                    "input_schema": EXPAND.input_schema},),
        ))

        said = "\n".join(
            str(part.value)
            for message in shaped.messages for part in message.content
        )
        assert "tool calls" not in said

    def test_the_count_comes_from_the_history_not_from_a_counter(self) -> None:
        """Two calls in the history is two spent, whatever order they arrived.

        A counter inside the strategy would be a second authority on how many
        turns an execution has taken, and it would disagree the first time it
        missed one.
        """
        from rey_lib.ai.content import AIMessage

        shaped = EmulatedTurnStrategy(max_tool_calls=7).shape(ProviderCall(
            model="m",
            messages=(
                AIMessage.asked_for_tools((AIToolCall(id="1", name="expand"),)),
                AIMessage.tool_answer("1", {}),
                AIMessage.asked_for_tools((AIToolCall(id="2", name="expand"),)),
                AIMessage.tool_answer("2", {}),
            ),
            tools=({"name": "expand", "description": "",
                    "input_schema": EXPAND.input_schema},),
        ))

        said = "\n".join(
            str(part.value)
            for message in shaped.messages for part in message.content
        )
        assert "used 2 of 7" in said

    def test_it_is_told_to_answer_rather_than_to_explore(self) -> None:
        shaped = EmulatedTurnStrategy().shape(ProviderCall(
            model="m", messages=(),
            tools=({"name": "expand", "description": "",
                    "input_schema": EXPAND.input_schema},),
        ))

        said = "\n".join(
            str(part.value)
            for message in shaped.messages for part in message.content
        )
        assert "Do not explore." in said

    def test_a_result_is_said_in_a_role_the_engine_has(self) -> None:
        """The ``tool`` role belongs to a protocol this engine does not have.

        A server that requires a tool turn to follow a real tool call either
        refuses it or ignores it -- and an ignored result is a model asking for
        the same tool again until the execution budget is spent.
        """
        from rey_lib.ai.content import AIMessage, AIRole

        shaped = EmulatedTurnStrategy().shape(ProviderCall(
            model="m",
            messages=(AIMessage.tool_answer("1", {"children": ["a"]}),),
            tools=({"name": "expand", "description": "",
                    "input_schema": EXPAND.input_schema},),
        ))

        assert all(
            message.role is not AIRole.TOOL for message in shaped.messages
        )
        said = "\n".join(
            str(part.value)
            for message in shaped.messages for part in message.content
        )
        assert "children" in said
        assert "Result of your last tool_call" in said


# -- end to end, through the real executor -----------------------------------

class TestAnExecutionThatCannotCallTools:
    """One ask, one loop, and nothing above it knows which mechanism ran."""

    @staticmethod
    def _ask(tools: tuple[AITool, ...] = (EXPAND,)) -> AIRequest:
        def runner(call: AIToolCall) -> AIToolResult:
            return AIToolResult(call_id=call.id, value={"children": ["a", "b"]})

        return AIRequest(
            input=AIRequest.prompt("what is under this node?").input,
            tools=tools,
            tool_runner=runner,
        )

    def test_a_local_engine_expands_and_then_answers(self) -> None:
        """The acceptance, in miniature: decision, real tool, decision, answer
        -- all of it through ``CanonicalToolLoop``, which sees only calls and
        results."""
        provider = _Scripted(
            decision(kind="tool_call", tool="expand", arguments={"node_key": "7"}),
            decision(kind="answer", content="it has two children"),
        )

        result = runtime(provider).execute(self._ask())

        assert result.text == "it has two children"
        assert [call.name for call in result.tool_calls] == ["expand"]
        assert result.tool_results[0].value == {"children": ["a", "b"]}

    def test_the_answer_is_reported_once_whole(self) -> None:
        """Nothing is reported while a decision arrives, because until it is
        complete nothing knows whether any of it is content at all."""
        from rey_lib.ai.streaming import AIEventKind

        provider = _Scripted(decision(kind="answer", content="all of it"))

        said = [
            event.text
            for event in runtime(provider).stream(self._ask())
            if event.kind is AIEventKind.CONTENT_DELTA
        ]

        assert said == ["all of it"]

    def test_a_malformed_decision_is_corrected_rather_than_counterfeited(self) -> None:
        """It produced no call, so there is no tool failure to report. The
        model is told what was wrong and answers again."""
        provider = _Scripted(
            "I think you should look at the children.",
            decision(kind="answer", content="recovered"),
        )

        result = runtime(provider).execute(self._ask())

        assert result.text == "recovered"
        assert result.tool_calls == ()
        assert result.tool_results == ()

    def test_the_correction_tells_the_model_what_was_wrong(self) -> None:
        provider = _Scripted(
            "not a decision",
            decision(kind="answer", content="recovered"),
        )

        runtime(provider).execute(self._ask())

        second = provider.calls[1]
        said = "\n".join(
            str(part.value)
            for message in second.messages for part in message.content
        )
        assert "could not be read" in said

    def test_a_spent_allowance_ends_the_execution(self) -> None:
        provider = _Scripted("no", "still no", "and again", "one more")

        with pytest.raises(AIOutputError):
            runtime(provider).execute(self._ask())

    def test_execution_obeys_the_resolved_mechanism(self) -> None:
        """The boundary, stated as a test: a resolved request is a fact.

        Handed one that names the emulated mechanism, the executor uses it --
        it does not go back to the profile's capability and decide again.
        """
        from dataclasses import replace

        from rey_lib.ai.turn_strategy import ToolMechanism

        provider = _Scripted(
            decision(kind="answer", content="obeyed"), capability=NATIVE,
        )
        ai = runtime(provider)
        resolved = ai.resolve(self._ask())
        assert isinstance(resolved.tool_mechanism.strategy, NativeTurnStrategy)

        forced = replace(
            resolved,
            tool_mechanism=ToolMechanism(strategy=EmulatedTurnStrategy()),
        )
        result = ai._executor.execute(forced)

        assert result.text == "obeyed"
        assert provider.calls[0].tools == ()

    def test_a_native_engine_is_untouched_by_any_of_this(self) -> None:
        """Nothing shaped, nothing read, tools offered as they always were."""
        provider = _Scripted("plain answer", capability=NATIVE)

        result = runtime(provider).execute(self._ask())

        assert result.text == "plain answer"
        assert provider.calls[0].tools == (
            {"name": "expand", "description": EXPAND.description,
             "input_schema": EXPAND.input_schema},
        )
        assert provider.calls[0].schema is None

    def test_an_ask_offering_no_tools_reaches_a_plain_engine(self) -> None:
        provider = _Scripted("hello", capability=PLAIN)

        assert runtime(provider).execute(
            AIRequest.prompt("hi"),
        ).text == "hello"

    def test_an_unsatisfiable_ask_is_refused_at_resolution(self) -> None:
        """Before a request exists, not once one is being executed."""
        provider = _Scripted(capability=PLAIN)

        with pytest.raises(AICapabilityError):
            runtime(provider).resolve(self._ask())

        assert provider.calls == []
