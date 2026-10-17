"""One unit of work for AI: its governed facts, its input, and its own outcome.

``AITask`` is the canonical unit of work (backlog 1021, ai.redesign). It carries
everything one execution needs -- which engine, which contract and instruction,
which settings, what is sent and what comes back -- so that AI receives a task
whose choices are already settled and performs it.

Two kinds of state, deliberately held by one object:

    the facts       governed configuration, input and caller context. Fixed at
                    construction: hydration (backlog 1022) decides them, and a
                    different choice -- another engine, another instruction --
                    is a different task, built again, never an edit to this one.
    the lifecycle   status, answer, error and run id. This task's own, changed
                    only by its transitions: started, answered, failed,
                    cancelled, and naming the run that is executing it.

Immutable facts do not mean an immutable lifecycle. Every field is read-only to
callers; the transitions below are the only code that writes the lifecycle, so
AI executes a task and invokes its transitions, and owns none of its state.

It reads nothing, resolves nothing and executes nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, ClassVar, Mapping, Sequence

from rey_lib.ai.content import AIContentKind, AIInput
from rey_lib.ai.errors import AIConfigurationError
from rey_lib.ai.instructions import AIInstructionKind
from rey_lib.ai.requests import AIOutputSpec
from rey_lib.ai.tools import AITool
from rey_lib.logs.app_logger import AppLogger

logger = AppLogger(__name__)

__all__ = ["AITask"]


@dataclass(frozen=True, eq=False, kw_only=True)
class AITask:
    """One unit of work, as hydration settled it.

    Attributes:
        id: What addresses this unit of work. Minted here.
        task_key, installation, label: Which governed task this is, and where.
        composed_first: Whether this task is composed before it runs. An
            interaction fact of the task, carried and not interpreted here.
        profile_key: Which governed engine was chosen, for evidence.
        model_identifier: The engine as Pydantic AI resolves it,
            ``<provider>:<model>``. Required: a task reaches one engine.
        base_url: Where that engine is reached; empty is the provider default.
        credential_ref: A reference to the engine's credential, never the value.
        timeout_seconds: How long one call may take; ``None`` is the default.
        access_allowed, access_default: Which representations the engine may
            receive, as its governed row states them.
        instruction_key, instruction_kind: The instruction this task follows.
        contract_key, contract_version, contract_body: The contract that
            instruction binds, and its text.
        output_schema: The shape the contract requires, when it states one.
        temperature, representation, connection: The settings that apply,
            already resolved from the group default and the task's override.
        input: What is sent.
        output: The output this unit of work requires.
        subject: What is being asked about, already resolved by the caller.
        subject_id, subject_kind, origin_id: How the caller addresses this
            unit of work's subject and where it began. Opaque: carried and
            preserved, never interpreted.
        tools: The governed tools this task offers (backlog 1023) -- facts
            only. What carries each out is bound by the runtime at execution,
            never by hydration.
        choices: The overrides the caller hydrated this task with, as given
            and unresolved (backlog 1037): what a rehydration keeps, so a
            governed value nobody chose is never carried forward as though it
            had been.
        status, answer, error, run_id: The lifecycle. Read-only to callers and
            changed only by this task's transitions.
    """

    IDLE: ClassVar[str] = "idle"
    RUNNING: ClassVar[str] = "running"
    COMPLETED: ClassVar[str] = "completed"
    FAILED: ClassVar[str] = "failed"
    CANCELLED: ClassVar[str] = "cancelled"

    #: The overrides a caller may choose, by their hydration argument names.
    #: ``output`` is one (backlog 1047, D4): stated, it is kept across edits;
    #: unstated, hydration derives it again at every rehydration.
    CHOICES: ClassVar[tuple[str, ...]] = (
        "profile_key", "instruction_key", "temperature", "representation", "connection",
        "output",
    )

    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    task_key: str = ""
    installation: str = ""
    label: str = ""
    composed_first: bool = False

    profile_key: str = ""
    model_identifier: str
    base_url: str = ""
    credential_ref: str = ""
    timeout_seconds: float | None = None
    access_allowed: tuple[str, ...] = ()
    access_default: str = ""

    instruction_key: str = ""
    instruction_kind: AIInstructionKind = AIInstructionKind.NONE
    contract_key: str = ""
    contract_version: str = ""
    contract_body: str = ""
    output_schema: dict[str, Any] | None = None

    temperature: float | None = None
    representation: str = ""
    connection: str = ""

    input: AIInput = field(default_factory=AIInput)
    output: AIOutputSpec = field(default_factory=AIOutputSpec)

    subject: str = ""
    subject_id: str = ""
    subject_kind: str = ""
    origin_id: str = ""

    tools: tuple[AITool, ...] = ()

    choices: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    status: str = field(default=IDLE, init=False)
    answer: Any = field(default=None, init=False)
    error: str = field(default="", init=False)
    run_id: str = field(default="", init=False)

    def __post_init__(self) -> None:
        """Refuse a unit of work that names no engine."""
        if not str(self.model_identifier or "").strip():
            raise AIConfigurationError(
                f"AI task '{self.task_key or '(default)'}' names no engine."
            )

    # -- hydration (backlog 1022) -------------------------------------------

    @classmethod
    def from_governed(
        cls,
        control: Any,
        installation: str,
        task_key: str = "",
        *,
        profile_key: str = "",
        instruction_key: str = "",
        temperature: float | None = None,
        representation: str = "",
        connection: str = "",
        input: AIInput | None = None,  # noqa: A002 -- the fact's own name
        output: AIOutputSpec | None = None,
        subject: str = "",
        subject_id: str = "",
        subject_kind: str = "",
        origin_id: str = "",
        task_id: str = "",
    ) -> "AITask":
        """One unit of work, hydrated from this installation's governed rows.

        As a Workflow or an HTTP operation is established: the runtime's Control
        reads the rows, and :meth:`hydrate` builds the task from them. Takes the
        Control rather than a context, because nothing in this subsystem holds a
        context. Reads once; a changed configuration is a new hydration.

        Raises:
            AIConfigurationError: as :meth:`hydrate` does.
        """
        logger.debug("hydrating AI task %s for installation %s",
                     task_key or "(default)", installation)
        return cls.hydrate(
            control.ai_configuration(installation),
            control.ai_engine_profiles(),
            control.ai_instructions(),
            installation=installation, task_key=task_key,
            profile_key=profile_key, instruction_key=instruction_key,
            temperature=temperature, representation=representation,
            connection=connection, input=input, output=output,
            subject=subject, subject_id=subject_id, subject_kind=subject_kind,
            origin_id=origin_id, task_id=task_id,
        )

    @classmethod
    def hydrate(
        cls,
        configuration: Sequence[Mapping[str, Any]],
        engines: Sequence[Mapping[str, Any]],
        instructions: Sequence[Mapping[str, Any]],
        *,
        installation: str,
        task_key: str = "",
        profile_key: str = "",
        instruction_key: str = "",
        temperature: float | None = None,
        representation: str = "",
        connection: str = "",
        input: AIInput | None = None,  # noqa: A002 -- the fact's own name
        output: AIOutputSpec | None = None,
        subject: str = "",
        subject_id: str = "",
        subject_kind: str = "",
        origin_id: str = "",
        task_id: str = "",
    ) -> "AITask":
        """The unit of work the governed rows state, with the caller's choices.

        ``task_id`` is not a choice: it is the runtime identity a rehydration
        carries, so the replacement task stands where the one it replaces stood
        (backlog 1037). Empty mints a new identity. The overrides actually given
        -- a non-empty text, a temperature that is not ``None`` -- are recorded
        as the task's ``choices``; an empty one is no choice.

        The output requirement, when the caller states none, is hydration's to
        decide from what the governed instruction is (backlog 1047, D4): a
        contract's body defines its own textual answer, so a contract-backed task
        asks plain text with no media type; any other task asks Markdown. No
        structured output is inferred -- no governed configuration states a
        shape.

        ``configuration`` is the installation's control.f_ai_configuration_get
        rows: the group row is the default scope and each task row one task, its
        inheritance already resolved by ai.ai_vw. ``engines`` and
        ``instructions`` are the estate's engine and instruction rows.

        Precedence, for every choice: the caller's override, then the task's
        row, then the group's. An override is an input here and never an edit
        afterwards: a different choice is a different hydration. A task key no
        row configures takes the default scope, as it always has.

        Raises:
            AIConfigurationError: when the task is disabled, or names an engine
                or instruction that is not configured or not usable.
        """
        key = str(task_key or "").strip()
        group = next(
            (row for row in configuration if str(row.get("row_type")) == "group"), {},
        )
        row = next(
            (one for one in configuration
             if str(one.get("row_type")) == "task" and str(one.get("task_key")) == key),
            None,
        ) if key else None
        if row is not None and row.get("is_enabled") is False:
            raise AIConfigurationError(f"AI task '{key}' is disabled.")
        scope = row if row is not None else group

        def chosen(override: Any, column: str) -> str:
            """The override, else the task's row, else the group's."""
            return str(
                override or (row or {}).get(column) or group.get(column) or ""
            ).strip()

        engine = cls._engine(engines, chosen(profile_key, "profile_key"), key)
        instruction = cls._instruction(
            instructions, chosen(instruction_key, "instruction_key"), key,
        )
        stated = (
            temperature if temperature is not None
            else next((value for value in ((row or {}).get("temperature"),
                                          group.get("temperature")) if value is not None),
                      None)
        )
        timeout = engine.get("timeout_seconds")
        kind = AIInstructionKind(str(instruction.get("kind") or "none"))
        required = output if output is not None else (
            AIOutputSpec.text() if kind is AIInstructionKind.CONTRACT
            else AIOutputSpec.markdown()
        )
        identity = {"id": str(task_id)} if str(task_id or "").strip() else {}
        # What the caller stated, before anything was derived: a derived value
        # recorded here would come back as a choice nobody made.
        given = {
            "profile_key": profile_key, "instruction_key": instruction_key,
            "temperature": temperature, "representation": representation,
            "connection": connection, "output": output,
        }
        return cls(
            **identity,
            task_key=key,
            installation=str(installation or ""),
            label=str(scope.get("task_label") or scope.get("label") or ""),
            # The row's own column, never inherited: ai.ai_vw coalesces nothing
            # for it, so a silent scope is false.
            composed_first=bool(scope.get("composed_first") or False),
            profile_key=str(engine.get("profile_key") or ""),
            model_identifier=str(engine.get("model_identifier") or ""),
            base_url=str(engine.get("base_url") or ""),
            credential_ref=str(engine.get("credential_ref") or ""),
            timeout_seconds=None if timeout is None else float(timeout),
            access_allowed=tuple(str(one) for one in (engine.get("access_allowed") or ())),
            access_default=str(engine.get("access_default") or ""),
            instruction_key=str(instruction.get("instruction_key") or ""),
            instruction_kind=kind,
            contract_key=str(instruction.get("contract_key") or ""),
            contract_version=str(instruction.get("contract_version") or ""),
            contract_body=str(instruction.get("body") or ""),
            temperature=None if stated is None else float(stated),
            representation=chosen(representation, "representation"),
            connection=chosen(connection, "connection_name"),
            input=input if input is not None else AIInput(),
            output=required,
            subject=subject, subject_id=subject_id,
            subject_kind=subject_kind, origin_id=origin_id,
            # The task's own row only: tools are offered per task, and the
            # group row and an unconfigured task offer none.
            tools=tuple(
                AITool(
                    name=str(one.get("tool_key") or ""),
                    description=str(one.get("description") or ""),
                    input_schema=dict(one.get("input_schema") or {}),
                    replay_safe=bool(one.get("replay_safe")),
                    capability_key=str(one.get("capability_key") or ""),
                    result_row_limit=(
                        None if one.get("result_row_limit") is None
                        else int(one["result_row_limit"])
                    ),
                )
                for one in ((row or {}).get("tools") or ())
            ),
            choices=MappingProxyType({
                name: value for name, value in given.items()
                if value is not None and not (isinstance(value, str) and not value.strip())
            }),
        )

    @staticmethod
    def _engine(
        engines: Sequence[Mapping[str, Any]], profile_key: str, task_key: str,
    ) -> Mapping[str, Any]:
        """The configured, enabled engine this task chose."""
        where = f"AI task '{task_key or '(default)'}'"
        if not profile_key:
            raise AIConfigurationError(f"{where} chooses no engine.")
        engine = next(
            (one for one in engines if str(one.get("profile_key")) == profile_key), None,
        )
        if engine is None:
            raise AIConfigurationError(
                f"{where} chooses engine '{profile_key}', which is not configured."
            )
        if engine.get("is_enabled") is False:
            raise AIConfigurationError(
                f"{where} chooses engine '{profile_key}', which is disabled."
            )
        return engine

    @staticmethod
    def _instruction(
        instructions: Sequence[Mapping[str, Any]], instruction_key: str, task_key: str,
    ) -> Mapping[str, Any]:
        """The instruction this task chose, or none when it chose none."""
        if not instruction_key:
            return {}
        where = f"AI task '{task_key or '(default)'}'"
        instruction = next(
            (one for one in instructions
             if str(one.get("instruction_key")) == instruction_key), None,
        )
        if instruction is None:
            raise AIConfigurationError(
                f"{where} chooses instruction '{instruction_key}', which is not configured."
            )
        if str(instruction.get("kind")) == AIInstructionKind.CONTRACT.value \
                and not instruction.get("body"):
            raise AIConfigurationError(
                f"{where} chooses instruction '{instruction_key}', a contract that "
                "carries no body, so nothing could be sent for it."
            )
        return instruction

    # -- what it shows ------------------------------------------------------

    def projection(self) -> dict[str, Any]:
        """This task as plain data, for whatever displays it (backlog 1047, D1).

        Its identity, which governed task it is, the caller's choices and what
        was asked, its lifecycle, and its answer as AI stated it -- text or
        value with the media type, never rendered. Engine credentials and the
        contract body stay out: they are execution facts, not display.
        """
        answer = self.answer
        return {
            "id": self.id,
            "task_key": self.task_key,
            "label": self.label,
            "composed_first": self.composed_first,
            "profile_key": self.profile_key,
            "instruction_key": self.instruction_key,
            "temperature": self.temperature,
            "representation": self.representation,
            "connection": self.connection,
            # A chosen output is shown by what it means -- its media type --
            # never as the requirement object itself.
            "choices": {
                name: (value.media_type if name == "output" else value)
                for name, value in self.choices.items()
            },
            "prompt": "\n\n".join(
                str(part.value) for part in self.input.content
                if part.kind is AIContentKind.TEXT
            ),
            "media_type": self.output.media_type,
            "subject": self.subject,
            "subject_id": self.subject_id,
            "subject_kind": self.subject_kind,
            "origin_id": self.origin_id,
            "status": self.status,
            "error": self.error,
            "run_id": self.run_id,
            "answer": None if answer is None else {
                "text": answer.text, "value": answer.value, "media_type": answer.media_type,
            },
        }

    # -- the lifecycle -----------------------------------------------------

    def started(self, run_id: str = "") -> None:
        """Running, with the previous outcome cleared.

        The outcome goes with the run that produced it: keeping it would let a
        reader see the last answer beside a running state as though the new one
        had already arrived.

        Args:
            run_id: How the execution now under way is addressed.
        """
        self._become(status=self.RUNNING, answer=None, error="", run_id=str(run_id or ""))

    def record_run(self, run_id: str) -> None:
        """Name the run executing this task, and change nothing else.

        Not ``started``, which also clears the outcome: a run can finish before
        whoever launched it has recorded its name, and resetting here would
        erase an answer that had already arrived.
        """
        self._become(run_id=str(run_id or ""))

    def answered(self, answer: Any) -> None:
        """Completed, carrying what came back."""
        self._become(status=self.COMPLETED, answer=answer, error="")

    def failed(self, message: str) -> None:
        """Failed, carrying why."""
        self._become(status=self.FAILED, answer=None, error=str(message))

    def cancelled(self) -> None:
        """Cancelled. Not a failure, and not an answer."""
        self._become(status=self.CANCELLED, answer=None, error="")

    def _become(self, **lifecycle: Any) -> None:
        """Write the lifecycle -- the only place it is written.

        The dataclass is frozen so that no caller can assign any field; the
        lifecycle is this object's own, so its transitions write it here. A
        governed fact is never written, even from inside.
        """
        facts = set(lifecycle) - {"status", "answer", "error", "run_id"}
        if facts:
            raise AttributeError(f"AITask facts are fixed: {', '.join(sorted(facts))}")
        for name, value in lifecycle.items():
            object.__setattr__(self, name, value)
