"""How a chosen engine carries a tool-bearing ask, and nothing else.

    AIObject      what the ask may do
    AI.resolve    WHICH mechanism can satisfy it
    AIExecutor    carries that decision out
    CanonicalToolLoop  sees only AIToolCall / AIToolResult

Two mechanisms, and the loop above them cannot tell which one ran:

    native      the provider emits tool calls itself
    emulated    the provider emits a constrained JSON *decision* and Rey
                turns it into the same AIToolCall

The emulated one exists because a local engine can be made to answer in a
shape without being able to call anything. Rey owns the protocol either way --
the declaration, the decision, the execution, the correction and the loop --
which is why this is a seam here and not an agent framework underneath.

**It is generic over offered tools.** Nothing here names a tool. A tool is
representable when it carries an ``input_schema``, which is a property it
already has, and the decision schema is generated from whatever was offered.
"""

from __future__ import annotations

import json
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import Any

from rey_lib.ai.capabilities import AICapability, AICapabilitySet
from rey_lib.ai.content import AIMessage, AIRole, text
from rey_lib.ai.errors import AIOutputError
from rey_lib.ai.policies import ValidationCorrectionPolicy
from rey_lib.ai.providers.base import ProviderCall, ProviderReply
from rey_lib.ai.tools import AITool, AIToolCall

__all__ = [
    "EmulatedTurnStrategy",
    "NativeTurnStrategy",
    "ToolMechanism",
    "TurnStrategy",
    "decision_schema",
    "resolve_tool_mechanism",
]

#: What an emulated decision may be. Stated once; the schema and the reader
#: both use it, so they cannot come to different answers about the vocabulary.
TOOL_CALL = "tool_call"
ANSWER = "answer"

#: How many malformed decisions an emulated execution may be corrected through.
#: Local engines miss a shape and recover when told; zero makes the mechanism
#: brittle and a large budget lets a weak one spin.
EMULATED_VALIDATION_CORRECTIONS = 2


class TurnStrategy(ABC):
    """One turn, as the resolved mechanism performs it.

    Two operations and no state: the call going out may need shaping, and the
    reply coming back may need reading. Everything else about a turn --
    budget, transport, replay, fallback -- belongs to the executor and stays
    there.
    """

    @abstractmethod
    def shape(self, call: ProviderCall) -> ProviderCall:
        """The call as this mechanism needs the provider to receive it."""

    @abstractmethod
    def read(self, reply: ProviderReply) -> ProviderReply:
        """The reply in the subsystem's terms, whatever shape it arrived in.

        Raises:
            AIOutputError: when the reply is not something this mechanism can
                read. It is corrected, not retried: the model is told what was
                wrong and answers again.
        """

    @property
    def buffers(self) -> bool:
        """Whether a turn must arrive whole before any of it is content.

        True where the answer is carried *inside* a structure, because until
        that structure is complete nothing knows whether the turn is an answer
        at all -- so no fragment of it is content a reader should see.
        """
        return False


class NativeTurnStrategy(TurnStrategy):
    """The provider's own tool protocol. Identity, deliberately.

    It exists so that every execution goes through one seam rather than the
    seam being a branch. A native call is the call as built and a native reply
    is the reply as given, which is exactly what makes this the regression test
    for every provider that already worked.
    """

    def shape(self, call: ProviderCall) -> ProviderCall:
        return call

    def read(self, reply: ProviderReply) -> ProviderReply:
        return reply


@dataclass(frozen=True)
class EmulatedTurnStrategy(TurnStrategy):
    """Rey's own tool protocol, over an engine that can only shape output.

    The provider is asked for one constrained object per turn and is told about
    no tools at all -- ``ProviderCall.tools`` is emptied, because an adapter
    that cannot use them would either ignore them or fail on them.
    """

    #: The shape the ask's own output must satisfy, where it states one. It
    #: becomes the ``answer`` branch, so a structured ask keeps its contract
    #: enforced at generation rather than only checked afterwards.
    answer_schema: dict[str, Any] | None = None
    #: How many tool calls this execution can afford, stated at resolution.
    #:
    #: An engine that cannot see a budget spends it. Told to answer and given a
    #: tool, a local model reads its way down a tree until the execution budget
    #: is gone and answers nothing -- not disobedience, just no way of knowing
    #: there was a limit to work within. Zero says nothing, for a caller that
    #: has no budget to state.
    max_tool_calls: int = 0

    @property
    def buffers(self) -> bool:
        return True

    def shape(self, call: ProviderCall) -> ProviderCall:
        """The same turn, asked for as a decision."""
        return replace(
            call,
            messages=_legible(call.messages, call.tools, self.max_tool_calls),
            tools=(),
            json_output=True,
            schema=decision_schema(call.tools, self.answer_schema),
        )

    def read(self, reply: ProviderReply) -> ProviderReply:
        """One decision, as either a tool call or an answer."""
        decided = _decoded(reply.text, reply.value)
        kind = decided.get("kind")

        if kind == TOOL_CALL:
            name = str(decided.get("tool") or "")
            arguments = decided.get("arguments")
            if not isinstance(arguments, dict):
                raise AIOutputError(
                    "A tool_call decision must carry 'arguments' as an object."
                )
            return replace(
                reply,
                text="",
                tool_calls=(
                    AIToolCall(id=uuid.uuid4().hex, name=name, arguments=arguments),
                ),
            )

        if kind == ANSWER:
            content = decided.get("content")
            if isinstance(content, str):
                return replace(reply, text=content, value=None, tool_calls=())
            # A structured answer reaches the output parser as the value it is.
            # Re-deriving it from text there would decode what is already
            # decoded, and would lose a legitimate string-shaped value.
            return replace(
                reply,
                text=json.dumps(content, ensure_ascii=False),
                value=content,
                tool_calls=(),
            )

        raise AIOutputError(
            f"A decision must state kind '{TOOL_CALL}' or '{ANSWER}', not "
            f"{kind!r}."
        )


@dataclass(frozen=True)
class ToolMechanism:
    """What resolution decided about carrying this ask's tools.

    A fact recorded at resolution and obeyed downstream. The correction
    allowance travels with it because it is part of that same decision: how a
    mechanism is corrected is not a separate choice from which mechanism runs.
    """

    strategy: TurnStrategy
    validation_correction: ValidationCorrectionPolicy | None = None


def resolve_tool_mechanism(
    capability: AICapabilitySet,
    tools: tuple[AITool, ...],
    *,
    answer_schema: dict[str, Any] | None = None,
    max_tool_calls: int = 0,
) -> ToolMechanism:
    """Which mechanism can carry these tools on an engine with this capability.

    Pure. It takes facts and answers with a decision, so resolution and any
    surface that wants to say whether a run is possible ask the same question
    of the same function rather than each interpreting a capability set.

    Args:
        capability: The effective capability of the selected profile.
        tools: What the request offers. Empty is always satisfiable.
        answer_schema: The ask's own output shape, where it states one.
        max_tool_calls: How many calls this execution can afford. Stated to an
            emulated engine, which otherwise cannot see that it has a limit.

    Returns:
        The mechanism to execute under.

    Raises:
        AICapabilityError: when no mechanism can carry them.
    """
    from rey_lib.ai.errors import AICapabilityError  # noqa: PLC0415

    if not tools:
        return ToolMechanism(strategy=NativeTurnStrategy())

    if capability.has(AICapability.NATIVE_TOOLS):
        return ToolMechanism(strategy=NativeTurnStrategy())

    if capability.has(AICapability.STRUCTURED_OUTPUT):
        unrepresentable = tuple(
            tool.name for tool in tools if not tool.input_schema
        )
        if unrepresentable:
            raise AICapabilityError(
                "This engine has no native tool calling, so its tools are "
                "carried as structured decisions -- and "
                f"{', '.join(sorted(unrepresentable))} declares no input "
                "schema, so it cannot be represented in one."
            )
        return ToolMechanism(
            strategy=EmulatedTurnStrategy(
                answer_schema=answer_schema, max_tool_calls=max_tool_calls,
            ),
            validation_correction=ValidationCorrectionPolicy(
                max_corrections=EMULATED_VALIDATION_CORRECTIONS,
            ),
        )

    raise AICapabilityError(
        "This request offers tools and the selected engine can neither call "
        "them natively nor produce the structured output Rey would carry them "
        "in."
    )


def decision_schema(
    tools: tuple[dict[str, Any], ...],
    answer_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """What one emulated turn must satisfy.

    A **discriminated union**, one branch per offered tool. A single branch
    naming every tool in an enum beside one ``arguments`` schema is not the
    same statement: it admits one tool's name carrying another's arguments,
    because each half is independently valid. The ``const`` on ``tool`` is what
    binds a name to its own schema.

    Two contracts meet here. The tool branches come from what was offered; the
    answer branch is the ask's own output shape, so a structured ask is still
    constrained at generation rather than only checked once it comes back.
    """
    branches: list[dict[str, Any]] = [
        {
            "type": "object",
            "properties": {
                "kind": {"const": TOOL_CALL},
                "tool": {"const": tool["name"]},
                "arguments": dict(tool["input_schema"] or {}),
            },
            "required": ["kind", "tool", "arguments"],
            "additionalProperties": False,
        }
        for tool in tools
    ]
    branches.append({
        "type": "object",
        "properties": {
            "kind": {"const": ANSWER},
            "content": dict(answer_schema) if answer_schema else {"type": "string"},
        },
        "required": ["kind", "content"],
        "additionalProperties": False,
    })
    return {"oneOf": branches}


def _legible(
    messages: tuple[AIMessage, ...],
    tools: tuple[dict[str, Any], ...],
    max_tool_calls: int = 0,
) -> tuple[AIMessage, ...]:
    """The same history, in a form an engine with no tool protocol can read.

    Three things are wrong with the canonical history for such an engine, and
    all of them are fixed here rather than in the adapter or in ``AIMessage``:

    - the turn recording what the model asked for carries ``tool_calls`` and no
      content, so an adapter that renders text drops it entirely and the model
      sees an answer to a question it has no record of asking;
    - the turn carrying a result has the ``tool`` role, which belongs to a
      protocol this engine does not implement. A server that requires a tool
      turn to follow a real tool call either refuses it or ignores it, and an
      ignored result is a model asking for the same tool again until the
      execution budget is spent -- which is exactly what it did;
    - nothing has told it the protocol.

    **The protocol goes before the ask, never after it.** It is a statement
    about the *shape* of a reply and not about what to do, and a small engine
    follows the most recent instruction most reliably -- so stating it last put
    a tool manual after the reader's actual question and the question stopped
    being the thing the model was answering. Asked to say hi about a tree node,
    it enumerated the tree instead: the last thing it had been told was that
    tools existed and it could take several turns using them.

    So it sits with the other standing instructions, before the first turn that
    is not one, and the ask stays the last word.
    """
    rendered: list[AIMessage] = []
    # What it has already spent, counted from the history it is being handed --
    # not tracked here. A counter of its own would be a second authority on how
    # many turns an execution has taken, and the two would disagree the first
    # time one of them missed a turn.
    spent = sum(
        len(message.tool_calls)
        for message in messages
        if message.role is AIRole.ASSISTANT and message.tool_calls
    )
    for message in messages:
        if message.role is AIRole.ASSISTANT and message.tool_calls:
            for call in message.tool_calls:
                rendered.append(AIMessage(
                    role=AIRole.ASSISTANT,
                    content=(text(json.dumps(
                        {
                            "kind": TOOL_CALL,
                            "tool": call.name,
                            "arguments": dict(call.arguments),
                        },
                        ensure_ascii=False,
                    )),),
                ))
            continue
        if message.role is AIRole.TOOL:
            # Said as what it is, by the only participant that could be
            # speaking: Rey ran the tool and is reporting back. Named as a
            # result rather than dropped into the conversation bare, so the
            # model can tell an answer to its own question from a new one.
            rendered.append(AIMessage(
                role=AIRole.USER,
                content=(text(
                    "Result of your last tool_call:\n"
                    + _said(message)
                    + "\n\nNow answer the question you were asked. Ask for "
                    "another tool only if you still cannot answer it."
                ),),
            ))
            continue
        rendered.append(message)

    # Before the first turn that is not a standing instruction, so the ask is
    # what the model read last.
    at = next(
        (index for index, message in enumerate(rendered)
         if message.role is not AIRole.SYSTEM),
        len(rendered),
    )
    rendered.insert(
        at,
        AIMessage(
            role=AIRole.SYSTEM,
            content=(text(_protocol(tools, spent, max_tool_calls)),),
        ),
    )
    return tuple(rendered)


def _said(message: AIMessage) -> str:
    """One message's content as the text an engine with no tool protocol reads."""
    from rey_lib.ai.content import AIContentKind  # noqa: PLC0415

    parts: list[str] = []
    for part in message.content:
        if part.kind is AIContentKind.STRUCTURED:
            parts.append(json.dumps(part.value, ensure_ascii=False))
        else:
            parts.append(str(part.value))
    return "\n".join(parts)


def _protocol(
    tools: tuple[dict[str, Any], ...], spent: int = 0, budget: int = 0,
) -> str:
    """What the engine is told about answering in decisions."""
    offered = "\n".join(
        f"- {tool['name']}: {tool.get('description') or ''}".rstrip()
        for tool in tools
    )
    return (
        _budget(spent, budget)
        + "Answer with one JSON object and nothing else. No prose, no code "
        "fence.\n\n"
        "When you can answer, answer:\n"
        '{"kind": "answer", "content": ...}\n\n'
        "If you cannot answer without looking something up first, ask for a "
        "tool:\n"
        '{"kind": "tool_call", "tool": "<name>", "arguments": { ... }}\n\n'
        "The tools available to you:\n"
        f"{offered}\n\n"
        # Said because the alternative is what a local model does by default.
        # Given a tool and no reason to stop, it explores until the execution
        # budget is spent and answers nothing -- which is a tool manual
        # outranking the question it was asked.
        "Answering is what you are here to do. Use a tool only when the "
        "question genuinely cannot be answered without it, and stop as soon as "
        "you have enough. Do not explore."
    )


def _budget(spent: int, budget: int) -> str:
    """What it has left, said first because it changes what to do next.

    Nothing at all when no budget was stated, so a caller without one is not
    told about a limit that does not exist.
    """
    if budget <= 0:
        return ""
    left = max(0, budget - spent)
    if left == 0:
        return (
            "You have no tool calls left. Answer now from what you already "
            "have, and say what you could not check.\n\n"
        )
    return (
        f"You have used {spent} of {budget} tool calls and have {left} left. "
        "When they are gone you must answer from what you have, so spend them "
        "only where they change the answer.\n\n"
    )


def _decoded(text_said: str, value: Any) -> dict[str, Any]:
    """The decision the engine made, or a refusal that earns a correction.

    A value the adapter already decoded is preferred over its text, for the
    same reason the output parser prefers one: decoding what is decoded can
    only lose.
    """
    if isinstance(value, dict):
        return value
    stripped = (text_said or "").strip()
    if not stripped:
        raise AIOutputError("A decision was asked for and nothing came back.")
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        # A model often wraps its object in prose or a fence. One more attempt
        # at the outermost object, on the same terms the output parser gives a
        # structured answer, before refusing.
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end <= start:
            raise AIOutputError(
                "A decision was asked for and the reply carried no JSON "
                "object."
            ) from None
        try:
            decoded = json.loads(stripped[start:end + 1])
        except json.JSONDecodeError as invalid:
            raise AIOutputError(
                f"A decision was asked for and the reply was not valid JSON: "
                f"{invalid}."
            ) from invalid
    if not isinstance(decoded, dict):
        raise AIOutputError("A decision must be a JSON object.")
    return decoded
