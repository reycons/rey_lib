"""The canonical AI capability for one resolved runtime.

One ``AI`` per resolved runtime or installation context. Two installations never
share one, because profiles, contracts and permissions are installation-scoped
configuration and a shared object would make one installation's selection the
other's.

Constructed from explicit resolved inputs, and afterwards it reaches back for
nothing: no ``ctx`` discovery, no ambient lookup, no module-level state. That is
the lesson the estate's own runtime objects already teach -- ``RunLog`` owns its
state and reads no context once built, ``ConnectionOwner`` owns one durable
relationship, ``Run`` is handed its dependencies.

It is the **aggregate root**, not the implementation. It owns the state and the
domain -- what is available, what is selected, how a request resolves against
that -- and delegates the mechanisms:

    AI ─ settings, profiles, instructions, sessions, observation
     └─ resolve(AIRequest) -> ResolvedAIRequest
          └─ AIExecutor ─ orders one execution, delegating every mechanism:
               TurnExecutor      transport retry, replay safety, fallback
               ToolLoop          continuation through tool calls
               OutputParser      validation
               OutputNormalizer  the caller-facing payload

``execute`` therefore reads as it should: resolve, then delegate. The provider,
retry, tool, validation and normalization code is not here, and no
``if provider ==`` ever will be.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator, Mapping

from rey_lib.ai.capabilities import AICapabilitySet
from rey_lib.ai.content import AIContentKind
from rey_lib.ai.contracts import ContractResolver
from rey_lib.ai.credentials import CredentialResolver
from rey_lib.ai.errors import (
    AICancelled,
    AIConfigurationError,
    AIError,
    AIExecutionError,
    AIOutputError,
    AIProviderError,
    AIRequestError,
    AISelectionError,
)
from rey_lib.ai.execution import AIExecutor
from rey_lib.ai.instructions import AIInstruction, AIInstructionKind
from rey_lib.ai.output import OutputParser
from rey_lib.ai.profiles import AIProfile
from rey_lib.ai.registry import AIRegistry
from rey_lib.ai.requests import AIOutputKind, AIRequest, AIRequestOptions, ResolvedAIRequest
from rey_lib.ai.results import (
    AIExecutionInfo,
    AIFinishReason,
    AIOutput,
    AIResult,
    AIUsage,
)
from rey_lib.ai.policies import DEFAULT_EXECUTION_POLICY, AIExecutionPolicy
from rey_lib.ai.objects import AIObject
from rey_lib.ai.sessions import AISession
from rey_lib.ai.settings import AISettings, AISettingsTask
from rey_lib.ai.streaming import AIEvent
from rey_lib.ai.task import AITask
from rey_lib.ai.tools import AITool, AIToolCall, AIToolResult, AIToolSet, Operation
from rey_lib.ai.turn_strategy import ToolMechanism, resolve_tool_mechanism
from rey_lib.logs.app_logger import AppLogger

if TYPE_CHECKING:
    from pydantic_ai.models import Model

logger = AppLogger(__name__)

#: The in-process tool server the Claude CLI reaches a task's tools through: its
#: tools are named ``mcp__rey__<tool>`` to the CLI (backlog 1136).
_CLAUDE_TOOL_SERVER = "rey"

__all__ = ["AI", "AISnapshot"]


class AISnapshot:
    """What this runtime offers and has selected, at one moment.

    Immutable, and complete enough that a presentation layer renders from it
    without interpreting configuration or asking a second question. It is the
    whole of what a Console projection needs.
    """

    __slots__ = ("instructions", "profiles", "settings")

    def __init__(
        self,
        *,
        settings: AISettings,
        profiles: tuple[AIProfile, ...],
        instructions: tuple[AIInstruction, ...],
    ) -> None:
        self.settings = settings
        self.profiles = profiles
        self.instructions = instructions


class AI:
    """The AI capability of one runtime.

    Args:
        registry: What this runtime offers -- its profiles, instructions and
            provider adapters, already resolved from configuration.
        settings: The starting selection. Validated on construction, so a
            runtime cannot begin holding a selection it does not offer.
        contracts: How a referenced contract's body is obtained.
        parser: How a structured output is produced and checked.
        policy: The control domains for this runtime -- turn budget, transport
            retry, replay safety, fallback and the two correction budgets.
        executor: Supplied only where a caller needs to substitute the execution
            owner. Absent, one is built from the parts above.
        credentials: How a configured credential reference becomes its value at
            the moment a model is built. Holds no secret and no context.
        connection_operations: Per connection name, the operations runtime
            composition bound for it (``capability_key -> operation``), so a
            task's governed connection selects how its tools reach their owners
            (backlog 741). Callables only: no connection is held here.
    """

    def __init__(
        self,
        *,
        registry: AIRegistry,
        settings: AISettings | None = None,
        contracts: ContractResolver | None = None,
        parser: OutputParser | None = None,
        policy: AIExecutionPolicy = DEFAULT_EXECUTION_POLICY,
        executor: AIExecutor | None = None,
        connections: Iterable[str] = (),
        credentials: CredentialResolver | None = None,
        connection_operations: Mapping[str, Mapping[str, Operation]] | None = None,
    ) -> None:
        self._registry = registry
        self._credentials = credentials or CredentialResolver()
        self._connection_operations = {
            str(name): dict(operations)
            for name, operations in (connection_operations or {}).items()
        }
        # Which connections a task here may query through, as **names**.
        #
        # Whether a connection may be used by a language model is the
        # connection's own declaration, and connections are the database
        # layer's. So what crosses into this one is the answer rather than the
        # objects: a set of permitted names, supplied by whatever composed both.
        # Nothing here opens, holds or imports a connection.
        self._connections = frozenset(
            str(name).strip() for name in connections if str(name).strip()
        )
        # Kept, not only passed on. Resolution states a correction allowance
        # for the mechanism it chooses, which it can only do against the
        # posture this runtime was built with.
        self._policy = policy
        # The validator for a task this runtime executes through Pydantic AI
        # (backlog 1020): OutputParser stays the owner of validation.
        self._parser = parser or OutputParser()
        self._executor = executor or AIExecutor(
            registry=registry,
            contracts=contracts,
            parser=parser,
            policy=policy,
        )
        self._observers: list[Callable[[AISettings], None]] = []
        self._settings = self._validated(settings or AISettings())
        # The asks composed against this runtime. Held here because this object
        # owns domain state and an ask is domain state -- a surface holding
        # them would be a second answer to what a reader is composing, and a
        # second one per open panel.
        self._objects: dict[str, AIObject] = {}
        # The session's AITasks by id (backlog 1037): composed, edited by
        # rehydration and executed here, within the lifetime of the application
        # session that holds this AI.
        self._tasks: dict[str, AITask] = {}
        # Each running execution's withdrawal, by task id (backlog 1039): a
        # Pydantic AI CancellationToken, fresh per execution and held only while
        # it runs. Typed loosely so Pydantic AI stays an execution-time import.
        # One execution per task id at a time (#1018 D-2): the threads executing
        # tasks and cancel_task read and write it under this lock, which guards
        # only the check, the registration and the release -- never a run.
        self._running: dict[str, Any] = {}
        self._running_lock = threading.Lock()

    # -- what is available -------------------------------------------------

    def profiles(self) -> tuple[AIProfile, ...]:
        """Every profile this runtime offers."""
        return self._registry.profiles()

    def instructions(self) -> tuple[AIInstruction, ...]:
        """Every instruction this runtime offers."""
        return self._registry.instructions()

    def capabilities(self, profile_id: str = "") -> AICapabilitySet:
        """What an application actually gets from a profile.

        The effective capability -- provider capability narrowed by installation
        policy. Consumers ask this rather than reasoning from a provider or
        model name, which is the inference this model exists to remove.
        """
        return self._registry.effective_capability(self._profile_for(profile_id))

    def instruction(self, instruction_id: str) -> AIInstruction:
        """One instruction this runtime offers, by id."""
        return self._registry.instruction(str(instruction_id or ""))

    def profile(self, profile_id: str = "") -> AIProfile:
        """One profile this runtime offers, by id, or the selected one."""
        return self._profile_for(profile_id)

    def permitted_access(
        self, profile_id: str = "", requested: str = "", task: str = "",
    ) -> str:
        """Which representation a configured model may receive.

        Authorization, answered from AI-owned state that was consumed at
        construction. Nothing re-reads an application context per request, which
        is what the old ``resolve_profile_for_llm`` did on every call.

        The representation itself is produced elsewhere: this says *which* one is
        allowed, and ``rey_lib.logs.profile_library`` produces it. That
        separation is why an operator inspecting both presentations does not
        depend on this layer.

        Settings choose which representation is *asked* for, with the same
        precedence as everything else -- the caller's, then the task's, then the
        default. They cannot widen what is permitted: the envelope is
        ``profile_access.allowed`` on the profile, and the profile still refuses
        anything outside it. That is why a settings mutation can change the
        request and never the authorization.
        """
        configured = self._settings.task(task)
        wanted = (
            str(requested or "")
            or (configured.representation if configured else "")
            or self._settings.representation
        )
        return self._profile_for(profile_id, configured).permitted_access(wanted)

    def snapshot(self) -> AISnapshot:
        """Everything a presentation layer needs, in one immutable answer."""
        return AISnapshot(
            settings=self._settings,
            profiles=self.profiles(),
            instructions=self.instructions(),
        )

    # -- what is selected --------------------------------------------------

    @property
    def settings(self) -> AISettings:
        """The one canonical answer to what is selected."""
        return self._settings

    def select_profile(self, profile_id: str) -> AISettings:
        """Select a profile, or refuse one this runtime does not offer."""
        if profile_id and not self._registry.has_profile(profile_id):
            raise AISelectionError(
                f"No AI profile is configured as '{profile_id}'."
            )
        return self._replace(self._settings.with_profile(profile_id))

    def select_instruction(self, instruction_id: str) -> AISettings:
        """Select an instruction, or refuse one this runtime does not offer."""
        if instruction_id and not self._registry.has_instruction(instruction_id):
            raise AISelectionError(
                f"No AI instruction is configured as '{instruction_id}'."
            )
        return self._replace(self._settings.with_instruction(instruction_id))

    def update_settings(self, settings: AISettings) -> AISettings:
        """Replace the whole selection at once, validated as one change."""
        return self._replace(self._validated(settings))

    def set_instruction_text(self, instruction_id: str, text: str) -> AIInstruction:
        """Replace what one offered instruction says.

        The canonical instruction content is this runtime's. A consumer that
        edited text and kept it would be a second answer to what the instruction
        says, which is the state one owner exists to prevent.

        ``AIInstruction`` is immutable, so this builds the replacement and puts
        it in place of the old one rather than mutating anything.

        Raises:
            AISelectionError: when this runtime offers no such instruction, or
                the instruction is not one whose text a caller may set. A
                contract's body comes from the contract it references, and
                overwriting it here would make the stored contract and what was
                sent disagree.
        """
        current = self._registry.instruction(str(instruction_id or ""))
        if current.kind is not AIInstructionKind.RAW:
            raise AISelectionError(
                f"AI instruction '{current.id}' is a {current.kind.value} "
                "instruction, and its text is not a caller's to set."
            )
        replaced = replace(current, text=str(text or ""))
        self._registry.replace_instruction(replaced)
        return replaced

    def observe(self, listener: Callable[[AISettings], None]) -> Callable[[], None]:
        """Watch the selection, and get back the way to stop.

        An observer holds no state: it is told the new canonical value, and it
        can always ask for the current one. There is no second owner, which is
        the whole of the guarantee this offers.
        """
        self._observers.append(listener)

        def stop() -> None:
            if listener in self._observers:
                self._observers.remove(listener)

        return stop

    # -- doing the work ----------------------------------------------------

    def resolve(self, request: AIRequest, *, session_id: str = "") -> ResolvedAIRequest:
        """What this request would actually execute as.

        Explicit request settings beat the task's, which beat the defaults, so a
        governed operation never silently depends on an operator's selection.
        The request is not mutated: a resolved value is built beside it, which is
        what makes an in-flight execution immune to a later settings change.

        The task's settings are read here, once, and what they produced is what
        executes. A later mutation reaches the next run and never this one.
        """
        effective = self._settings.task(request.task)
        profile = self._profile_for(request.profile_id, effective)
        instruction = self._instruction_for(request, effective)
        resolved = ResolvedAIRequest(
            input=request.input,
            profile=profile,
            instruction=instruction,
            output=request.output,
            tools=request.tools,
            options=self._options_for(request, effective),
            context=dict(request.context),
            cancelled=request.cancelled,
            tool_runner=request.tool_runner,
            session_id=session_id,
        )
        # How the selected engine can carry this ask's tools. Decided here,
        # where the engine is chosen, rather than inside execution: an ask that
        # nothing can carry is refused before there is an execution to refuse,
        # and execution obeys the answer instead of reaching a second one.
        #
        # Asked after the request is built because the answer branch of an
        # emulated decision is the ask's own output shape, and ``schema``
        # resolves where that shape comes from.
        mechanism = resolve_tool_mechanism(
            self._registry.effective_capability(profile),
            resolved.tools,
            answer_schema=resolved.schema,
            # Every tool call spends a turn, and the last turn has to be the
            # answer. So what the ask can afford is one fewer than the budget
            # -- stated here, because resolution is where this runtime's
            # posture and the chosen mechanism meet.
            max_tool_calls=max(0, self._policy.budget.max_turns - 1),
        )
        logger.debug(
            "AI resolved: profile=%s model=%s provider=%s instruction=%s(%s) "
            "tools=%s mechanism=%s corrections=%s",
            profile.id, profile.model, profile.provider,
            instruction.id or "(none)", instruction.kind.value,
            [tool.name for tool in resolved.tools] or "(none)",
            type(mechanism.strategy).__name__,
            getattr(mechanism.validation_correction, "max_corrections", "(runtime)"),
        )
        return replace(resolved, tool_mechanism=mechanism)

    def tool_mechanism(
        self, profile_id: str, tools: tuple[AITool, ...],
    ) -> ToolMechanism:
        """How a profile would carry these tools, without resolving an ask.

        The same question ``resolve`` asks, answered by the same function, so a
        surface saying whether a run is possible cannot drift from the rule
        that admits one. It builds no request: painting a banner is not an
        execution.

        Raises:
            AICapabilityError: when no mechanism can carry them.
        """
        return resolve_tool_mechanism(
            self._registry.effective_capability(self._profile_for(profile_id)),
            tuple(tools),
        )

    def execute(self, request: AIRequest, *, session_id: str = "") -> AIResult:
        """Resolve this request and run it."""
        return self._executor.execute(self.resolve(request, session_id=session_id))

    def stream(self, request: AIRequest, *, session_id: str = "") -> Iterator[AIEvent]:
        """Resolve this request and run it, reporting canonical events."""
        return self._executor.stream(self.resolve(request, session_id=session_id))

    def projection(self, control: Any, installation: str) -> dict[str, Any]:
        """What this AI offers an installation, as plain data (backlog 1041).

        The tasks its governed configuration offers, in configured order, each as
        its key, its label and whether it is composed before it runs. What an
        application session serving this AI sends its presentation, as a Workflow
        projects itself for workflow_app: the AI states it, nobody re-derives it.

        With them, the enabled engines and the instructions a task's choices may
        name (backlog 1047, D2): what may be chosen, so a presentation offers
        overrides without reading configuration itself. Each in the governed
        reads' own order.
        """
        return {
            "tasks": [
                {
                    "task_key": str(row.get("task_key") or ""),
                    "label": str(row.get("task_label") or row.get("task_key") or ""),
                    "composed_first": bool(row.get("composed_first") or False),
                }
                for row in control.ai_configuration(installation)
                if str(row.get("row_type")) == "task" and row.get("is_enabled") is not False
            ],
            "engines": [
                {
                    "profile_key": str(row.get("profile_key") or ""),
                    "model_identifier": str(row.get("model_identifier") or ""),
                    # The representations this engine may be sent, as its
                    # governed row states (#1189 C5).
                    "representations": [str(one) for one in (row.get("access_allowed") or ())],
                    "representation_default": str(row.get("access_default") or ""),
                }
                for row in control.ai_engine_profiles()
                if row.get("is_enabled") is not False
            ],
            "instructions": [
                {
                    "instruction_key": str(row.get("instruction_key") or ""),
                    "kind": str(row.get("kind") or ""),
                }
                for row in control.ai_instructions()
            ],
        }

    def task_projection(
        self, task: AITask, operations: Mapping[str, Operation] | None = None,
    ) -> dict[str, Any]:
        """One held task as plain data, with what this runtime settles about running it.

        ``AITask.projection`` states the task. Added here is what only this AI can
        answer, by the very rules :meth:`execute_task` applies (#1189 C5):

        - ``capabilities``: each governed tool's capability and whether it is
          bound -- the task's connection's operations, then ``operations``, the
          same merge an execution binds;
        - ``readiness``: whether a run would be admitted, and every reason one
          would be refused -- an execution mechanism AI does not run, or a
          declared capability bound to nothing;
        - ``representations``: what the task's engine may be sent, as its
          governed row states;
        - ``contract``: the governed instruction the task follows, with its
          contract body, so a presentation shows the contract it would send.

        Engine credentials stay out, as in ``AITask.projection``.
        """
        bound = self._bound_operations(task, operations)
        capabilities = [
            {
                "tool": tool.name,
                "capability_key": tool.capability_key,
                "description": tool.description,
                "bound": bool(tool.capability_key) and tool.capability_key in bound,
            }
            for tool in task.tools
        ]
        reasons = []
        if task.execution_mechanism not in self._run_steps():
            reasons.append(
                f"Its engine's execution mechanism '{task.execution_mechanism or '(none)'}' "
                "is not one AI runs."
            )
        unbound = [one["capability_key"] or one["tool"]
                   for one in capabilities if not one["bound"]]
        if unbound:
            reasons.append(
                f"It needs {', '.join(unbound)}, which nothing here can carry out."
            )
        return {
            **task.projection(),
            "capabilities": capabilities,
            "readiness": {"ready": not reasons, "reasons": reasons},
            "representations": {
                "allowed": list(task.access_allowed),
                "default": task.access_default,
            },
            "contract": {
                "instruction_kind": task.instruction_kind.value,
                "contract_key": task.contract_key,
                "contract_version": task.contract_version,
                "body": task.contract_body,
            },
        }

    def _run_steps(self) -> dict[str, Callable[..., Any]]:
        """The execution mechanisms AI runs, by the name a governed engine states."""
        return {
            "pydantic_ai": self._run_pydantic_ai,
            "claude_agent_sdk": self._run_claude_agent_sdk,
        }

    def _bound_operations(
        self, task: AITask, operations: Mapping[str, Operation] | None,
    ) -> dict[str, Operation]:
        """What carries out a task's capabilities: its connection's, then the caller's.

        The connection selects; the caller may add. One merge, used by every
        execution and by every account of whether one would be admitted.
        """
        return {
            **self._connection_operations.get(task.connection, {}),
            **(operations or {}),
        }

    def execute_task(
        self,
        task: AITask,
        operations: Mapping[str, Operation] | None = None,
        observe: Callable[[AIEvent], None] | None = None,
    ) -> AIResult:
        """Perform one hydrated unit of work (backlog 1020).

        With ``observe``, the run is streamed and each canonical ``AIEvent`` is
        handed to it as it happens (backlog 1038): started, content as the model
        produces it, each tool call requested and its result, then completed or
        failed. An engine's own events stop here. The observer only relays: its
        first failure is reported once and it is detached for the rest of this
        execution, which continues -- a consumer of the events cannot decide the
        task's outcome.

        The task's choices are settled; this runs it once through the execution
        mechanism its engine states (backlog 1136) -- Pydantic AI, or Claude
        tooling on the user's Claude subscription -- and records the outcome
        through the task's own transitions. Opening, closing, the transitions,
        the events and the result are this method's whatever the mechanism; only
        the run itself is the mechanism's private step. The task's governed
        tools are bound here to ``operations`` -- the runtime's
        ``capability_key -> operation`` mapping -- as this execution's
        ``AIToolSet`` (backlog 1023); hydration never supplies them. Synchronous:
        every caller runs on a thread without an event loop, and from inside a
        running loop this refuses rather than starting one of its own.

        Whatever comes back, ``OutputParser`` validates it, and a failure is
        handed back to the model as a correction within this runtime's
        allowance.

        Raises:
            AIRequestError: when the task carries input other than text, or is
                already running -- refused before anything about it changes.
            AIExecutionError: when called inside a running event loop, or a
                usage limit stops the run.
            AIOutputError: when the output never satisfied its contract.
            AIProviderError: when the engine refused or could not be reached.
            AIConfigurationError: when the engine or the run cannot be set up,
                its execution mechanism is one AI does not run, or a tool's
                capability is bound to nothing.
            AICancelled: when :meth:`cancel_task` withdrew the work; the task
                has recorded cancelled (backlog 1039).
        """
        import asyncio
        import uuid
        from datetime import datetime, timezone

        from pydantic_ai import CancellationToken

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise AIExecutionError(
                f"AI task '{task.task_key or '(default)'}' was asked to run inside a "
                "running event loop; execute it from a thread that has none."
            )
        prompt = self._prompt_of(task)
        schema = (
            task.output.schema if task.output.schema is not None
            else task.output_schema if task.output.kind is AIOutputKind.CONTRACT
            else None
        )

        key = task.task_key or "(default)"
        execution_id = uuid.uuid4().hex
        observer = observe

        def tell(event: AIEvent) -> None:
            """Hand one event to the observer, which cannot decide the outcome."""
            nonlocal observer
            if observer is None:
                return
            try:
                observer(event)
            except Exception as exc:  # noqa: BLE001 — canonical execution boundary
                # The observer is external behaviour (backlog 1038, as 1045):
                # converted to its AppError and reported once. Detached first, so
                # nothing further -- not even word that it broke -- reaches it.
                observer = None
                error = AIExecutionError(f"AI task '{key}': event observer failed", cause=exc)
                error.__cause__ = exc
                error.report(f"AI task '{key}' event notification", source=__name__)

        # Fresh for this execution: a cancelled token cancels any later run it
        # is given. Held only while this execution runs, whatever ends it.
        token = CancellationToken()
        with self._running_lock:
            if task.id in self._running:
                raise AIRequestError(f"AI task '{task.id}' is already running.")
            self._running[task.id] = token
        task.started()
        tell(AIEvent.started(execution_id))
        started = datetime.now(timezone.utc)
        try:
            step = self._run_steps().get(task.execution_mechanism)
            if step is None:
                raise AIConfigurationError(
                    f"AI task '{key}' names execution mechanism "
                    f"'{task.execution_mechanism or '(none)'}', which AI does not run."
                )
            toolset = AIToolSet(task.tools, self._bound_operations(task, operations))
            # Streamed only for an observer: without one the run is exactly the
            # unstreamed request it always was.
            output, usage = step(
                task, prompt, schema, toolset, token,
                tell if observe is not None else None, execution_id,
            )
        except AICancelled:
            # A withdrawal, not a fault: the task records it as its own outcome
            # and nothing is reported at ERROR.
            task.cancelled()
            tell(AIEvent.cancelled(execution_id))
            raise
        except AIError as error:
            task.failed(str(error))
            tell(AIEvent.failed(execution_id, str(error)))
            raise
        else:
            result = AIResult(
                output=output,
                execution=AIExecutionInfo(
                    execution_id=execution_id,
                    profile_id=task.profile_key,
                    model=task.model_identifier,
                    instruction_id=task.instruction_key,
                    started_at=started.isoformat(),
                    ended_at=datetime.now(timezone.utc).isoformat(),
                    finish_reason=AIFinishReason.COMPLETED,
                ),
                usage=usage,
            )
            task.answered(result)
            tell(AIEvent.completed(execution_id, result))
            return result
        finally:
            with self._running_lock:
                del self._running[task.id]

    def _run_pydantic_ai(
        self,
        task: AITask,
        prompt: str,
        schema: dict[str, Any] | None,
        toolset: AIToolSet,
        token: Any,
        tell: Callable[[AIEvent], None] | None,
        execution_id: str,
    ) -> tuple[AIOutput, AIUsage]:
        """Run one execution through Pydantic AI: :meth:`execute_task`'s step.

        Builds the task's model, runs it once and returns the validated output
        and what it used; every failure leaves as its AIError. Structured output
        asks Pydantic AI for the model's native JSON-schema output where the
        model's own profile supports it, prompted output otherwise, and never
        tool-call output -- a profile's tool flag is not a promise a local
        engine keeps. ``OutputParser``'s rejection goes back to the model as
        ``ModelRetry`` within the validation allowance.

        Raises:
            AICancelled: when ``token`` was cancelled. A synchronous tool call
                already in progress is not interrupted; its owner finishes it
                and the result is discarded.
            AIConfigurationError, AIExecutionError, AIOutputError,
            AIProviderError: as :meth:`execute_task` states them.
        """
        import pydantic_ai
        from pydantic_ai import (
            Agent,
            ModelRetry,
            NativeOutput,
            PromptedOutput,
            RunCancelled,
            StructuredDict,
            UsageLimits,
        )
        from pydantic_ai.exceptions import (
            ModelAPIError,
            UnexpectedModelBehavior,
            UsageLimitExceeded,
            UserError,
        )

        key = task.task_key or "(default)"

        async def relay(_context: Any, events: Any) -> None:
            """Pydantic AI's events for this run, told as canonical AIEvents."""
            async for event in events:
                translated = self._translated(execution_id, event)
                if translated is not None and tell is not None:
                    tell(translated)

        try:
            model = self._model(task.model_identifier, task.base_url, task.credential_ref)
            output_type: Any = str
            if task.output.is_structured() and schema is not None:
                shaped = StructuredDict(schema, name="answer")
                output_type = (
                    NativeOutput(shaped)
                    if model.profile.get("supports_json_schema_output")
                    else PromptedOutput(shaped)
                )
            agent = Agent(
                model,
                output_type=output_type,
                instructions=task.contract_body or None,
                tools=toolset.pydantic_tools(),
                retries={
                    "output": self._policy.validation_correction.max_corrections,
                    "tools": self._policy.tool_correction.max_corrections,
                },
            )

            @agent.output_validator
            def _satisfies(answer: Any) -> Any:
                """Rey's validation, the model told what was wrong when it fails."""
                try:
                    self._parse_task_output(task, schema, answer)
                except AIOutputError as invalid:
                    raise ModelRetry(str(invalid)) from invalid
                return answer

            settings: dict[str, Any] = {}
            if task.temperature is not None:
                settings["temperature"] = task.temperature
            if task.timeout_seconds is not None:
                settings["timeout"] = task.timeout_seconds
            # Pydantic AI's first-run banner goes to stderr only; it is kept off
            # entirely so no Rey process ever carries it.
            pydantic_ai.BANNER_ENABLED = False
            logger.debug(
                "executing AI task %s on %s (instruction %s, output %s)",
                key, task.model_identifier,
                task.instruction_key or "(none)", type(output_type).__name__,
            )
            run = agent.run_sync(
                prompt, model_settings=settings or None,
                usage_limits=UsageLimits(request_limit=self._policy.budget.max_turns),
                event_stream_handler=relay if tell is not None else None,
                cancellation_token=token,
            )
            output = self._parse_task_output(task, schema, run.output)
        except RunCancelled as withdrawn:
            raise AICancelled(f"AI task '{key}' was cancelled.", cause=withdrawn) from withdrawn
        except UserError as error:
            raise AIConfigurationError(
                f"AI task '{key}' could not be run.", cause=error,
            ) from error
        except UsageLimitExceeded as error:
            raise AIExecutionError(
                f"AI task '{key}' reached a usage limit.", cause=error,
            ) from error
        except UnexpectedModelBehavior as error:
            raise AIOutputError(
                f"AI task '{key}' produced no output that satisfies its contract.",
                cause=error,
            ) from error
        except ModelAPIError as error:
            raise AIProviderError(
                f"The engine for AI task '{key}' refused or could not be reached.",
                cause=error,
            ) from error
        return output, AIUsage(
            input_tokens=int(run.usage.input_tokens or 0),
            output_tokens=int(run.usage.output_tokens or 0),
        )

    def _run_claude_agent_sdk(
        self,
        task: AITask,
        prompt: str,
        schema: dict[str, Any] | None,
        toolset: AIToolSet,
        token: Any,
        tell: Callable[[AIEvent], None] | None,
        execution_id: str,
    ) -> tuple[AIOutput, AIUsage]:
        """Run one execution through Claude tooling: :meth:`execute_task`'s step.

        The Claude Agent SDK drives the Claude CLI, which authenticates with
        the user's Claude subscription (backlog 1136). The CLI is isolated to
        this task: none of Claude Code's own tools and none of its settings
        files, only the task's governed tools served in-process, its contract
        as the whole system prompt, and the budget's turn limit. Rey supplies
        no credential: the CLI authenticates as it is logged in, and its
        environment is neither inspected nor changed here. The engine's
        temperature and timeout are not settings this mechanism takes.

        ``OutputParser``'s rejection is sent back in the same session as the
        next message, at most the validation allowance's number of times.
        ``token`` is watched while the session runs and its cancellation
        interrupts the CLI.

        Raises:
            AICancelled: when ``token`` was cancelled. A synchronous tool call
                already in progress is not interrupted; its owner finishes it
                and the result is discarded.
            AIConfigurationError: when the ``claude`` extra or the CLI is not
                installed here.
            AIExecutionError: when the turn limit stopped the session.
            AIOutputError: when no answer satisfied the contract.
            AIProviderError: when the CLI refused or could not be reached.
        """
        import asyncio

        key = task.task_key or "(default)"
        try:
            from claude_agent_sdk import (
                ClaudeAgentOptions,
                ClaudeSDKClient,
                CLIConnectionError,
                CLINotFoundError,
                ProcessError,
                ResultMessage,
                create_sdk_mcp_server,
            )
        except ImportError as missing:
            raise AIConfigurationError(
                f"AI task '{key}' runs through Claude tooling, and rey_lib's 'claude' "
                "extra is not installed.", cause=missing,
            ) from missing

        structured = task.output.is_structured() and schema is not None
        tools = toolset.sdk_tools()
        options = ClaudeAgentOptions(
            model=task.model_identifier,
            system_prompt=task.contract_body or None,
            tools=[],
            setting_sources=[],
            mcp_servers=(
                {_CLAUDE_TOOL_SERVER: create_sdk_mcp_server(_CLAUDE_TOOL_SERVER, tools=tools)}
                if tools else {}
            ),
            allowed_tools=[f"mcp__{_CLAUDE_TOOL_SERVER}__{tool.name}" for tool in tools],
            max_turns=self._policy.budget.max_turns,
            output_format={"type": "json_schema", "schema": schema} if structured else None,
            include_partial_messages=tell is not None,
        )

        async def withdraw(client: Any) -> None:
            """Interrupt the session once the execution's token is cancelled."""
            while not token.cancelled:
                await asyncio.sleep(0.05)
            await client.interrupt()

        async def converse(client: Any) -> tuple[AIOutput, AIUsage]:
            """Ask, then correct in the same session until the answer is valid."""
            corrections = self._policy.validation_correction.max_corrections
            used = AIUsage()
            message = prompt
            while True:
                await client.query(message)
                outcome: Any = None
                async for item in client.receive_response():
                    if isinstance(item, ResultMessage):
                        outcome = item
                    elif tell is not None:
                        for event in self._sdk_translated(execution_id, item):
                            tell(event)
                if token.cancelled:
                    raise AICancelled(f"AI task '{key}' was cancelled.")
                if outcome is None:
                    raise AIProviderError(
                        f"The engine for AI task '{key}' ended without a result."
                    )
                spent = dict(outcome.usage or {})
                used = AIUsage(
                    input_tokens=used.input_tokens + int(spent.get("input_tokens") or 0),
                    output_tokens=used.output_tokens + int(spent.get("output_tokens") or 0),
                )
                if outcome.is_error:
                    if outcome.subtype == "error_max_turns":
                        raise AIExecutionError(f"AI task '{key}' reached a usage limit.")
                    raise AIProviderError(
                        f"The engine for AI task '{key}' refused or could not be reached "
                        f"({outcome.subtype})."
                    )
                answer = (
                    outcome.structured_output
                    if structured and outcome.structured_output is not None
                    else outcome.result or ""
                )
                try:
                    return self._parse_task_output(task, schema, answer), used
                except AIOutputError as invalid:
                    if corrections <= 0:
                        raise AIOutputError(
                            f"AI task '{key}' produced no output that satisfies its "
                            "contract.", cause=invalid,
                        ) from invalid
                    corrections -= 1
                    message = str(invalid)

        async def run() -> tuple[AIOutput, AIUsage]:
            """One session, the token watched for as long as it is open."""
            async with ClaudeSDKClient(options=options) as client:
                watcher = asyncio.create_task(withdraw(client))
                try:
                    return await converse(client)
                finally:
                    watcher.cancel()

        logger.debug(
            "executing AI task %s on %s through Claude tooling (instruction %s, output %s)",
            key, task.model_identifier, task.instruction_key or "(none)",
            "structured" if structured else "text",
        )
        try:
            return asyncio.run(run())
        except CLINotFoundError as error:
            raise AIConfigurationError(
                f"AI task '{key}' runs through Claude tooling, and the Claude CLI is not "
                "installed here.", cause=error,
            ) from error
        except (CLIConnectionError, ProcessError) as error:
            raise AIProviderError(
                f"The engine for AI task '{key}' refused or could not be reached.",
                cause=error,
            ) from error

    @staticmethod
    def _sdk_translated(execution_id: str, item: Any) -> list[AIEvent]:
        """One Claude Agent SDK message as canonical AIEvents.

        Text as the model streams it, each tool call the model makes and each
        result it is given, and the engine's rate-limit report. A complete
        assistant message's text is not told again: it arrived as the stream.
        Anything else tells an observer nothing it does not learn from these.
        """
        from claude_agent_sdk import (  # noqa: PLC0415 -- AI's dependency only
            AssistantMessage,
            RateLimitEvent,
            StreamEvent,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )

        if isinstance(item, StreamEvent):
            event = item.event or {}
            delta = event.get("delta") or {}
            if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                text = str(delta.get("text") or "")
                return [AIEvent.content(execution_id, text)] if text else []
            return []
        if isinstance(item, RateLimitEvent):
            info = item.rate_limit_info
            return [AIEvent.rate_limited(execution_id, {
                "status": info.status,
                "resets_at": info.resets_at,
                "rate_limit_type": info.rate_limit_type,
            })]
        prefix = f"mcp__{_CLAUDE_TOOL_SERVER}__"
        if isinstance(item, AssistantMessage):
            return [
                AIEvent.tool_requested(execution_id, AIToolCall(
                    id=block.id, name=block.name.removeprefix(prefix),
                    arguments=dict(block.input or {}),
                ))
                for block in item.content if isinstance(block, ToolUseBlock)
            ]
        if isinstance(item, UserMessage) and isinstance(item.content, list):
            return [
                AIEvent.tool_accepted(execution_id, AIToolResult(
                    call_id=block.tool_use_id,
                    value=None if block.is_error else block.content,
                    failed=bool(block.is_error),
                    message=str(block.content) if block.is_error else "",
                ))
                for block in item.content if isinstance(block, ToolResultBlock)
            ]
        return []

    @staticmethod
    def _translated(execution_id: str, event: Any) -> AIEvent | None:
        """One Pydantic AI run event as the canonical AIEvent, or nothing.

        Text the model produces, each function tool call and each result. A
        result is failed when the owner refused the call (``ToolFailed``) or it
        was sent back for correction (``ModelRetry``), with that message.
        Anything else -- part boundaries, the final-result marker -- tells an
        observer nothing it does not learn from these.
        """
        from pydantic_ai.messages import (  # noqa: PLC0415 -- AI's dependency only
            FunctionToolCallEvent,
            FunctionToolResultEvent,
            PartDeltaEvent,
            PartStartEvent,
            RetryPromptPart,
            TextPart,
            TextPartDelta,
        )

        if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
            return AIEvent.content(execution_id, event.part.content) \
                if event.part.content else None
        if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
            return AIEvent.content(execution_id, event.delta.content_delta) \
                if event.delta.content_delta else None
        if isinstance(event, FunctionToolCallEvent):
            return AIEvent.tool_requested(execution_id, AIToolCall(
                id=event.part.tool_call_id, name=event.part.tool_name,
                arguments=event.part.args_as_dict(),
            ))
        if isinstance(event, FunctionToolResultEvent):
            part = event.part
            if isinstance(part, RetryPromptPart):
                return AIEvent.tool_accepted(execution_id, AIToolResult(
                    call_id=part.tool_call_id, failed=True, message=str(part.content),
                ))
            failed = getattr(part, "outcome", "") == "failed"
            return AIEvent.tool_accepted(execution_id, AIToolResult(
                call_id=part.tool_call_id,
                value=None if failed else part.content,
                failed=failed,
                message=str(part.content) if failed else "",
            ))
        return None

    def _parse_task_output(
        self, task: AITask, schema: dict[str, Any] | None, answer: Any,
    ) -> AIOutput:
        """The task's output, validated by OutputParser in the form it asked for."""
        if isinstance(answer, str):
            parsed = self._parser.parse_output(task.output, schema, answer)
        else:
            parsed = self._parser.parse_output(task.output, schema, "", dict(answer))
        if task.output.media_type:
            return replace(parsed, media_type=task.output.media_type)
        return parsed

    @staticmethod
    def _prompt_of(task: AITask) -> str:
        """What is sent: the subject, then the task's text, as one prompt.

        Both, because both were asked: a prompt about a subject is not a prompt
        instead of one. Text only in this slice; anything else is refused.
        """
        if task.input.messages or any(
            part.kind is not AIContentKind.TEXT for part in task.input.content
        ):
            raise AIRequestError(
                f"AI task '{task.task_key or '(default)'}' carries input other than "
                "text, which this execution does not send."
            )
        parts = [task.subject, *(str(part.value) for part in task.input.content)]
        prompt = "\n\n".join(part for part in parts if part.strip())
        if not prompt:
            raise AIRequestError(
                f"AI task '{task.task_key or '(default)'}' carries nothing to send."
            )
        return prompt

    # -- the session's tasks (backlog 1037) --------------------------------

    #: What an edit may change besides the choices: which task, and what the
    #: caller sends and addresses. Never the identity, which an edit carries,
    #: and never the output requirement, which is a choice (backlog 1047, D4).
    _TASK_FACTS = (
        "task_key", "input", "subject", "subject_id", "subject_kind", "origin_id",
    )

    def hydrate_task(self, control: Any, installation: str, **facts: Any) -> AITask:
        """Compose one unit of work for this session, and hold it, idle.

        ``facts`` are which task (``task_key``), its choices (``AITask.CHOICES``)
        and the caller's runtime facts, as ``AITask.from_governed`` takes them;
        the identity is minted, never given.

        Raises:
            AIRequestError: when a fact is named that a task does not take.
            AIConfigurationError: as ``AITask.from_governed`` does.
        """
        self._refuse_unknown(facts)
        task = AITask.from_governed(control, installation, **facts)
        self._tasks[task.id] = task
        return task

    def task(self, task_id: str) -> AITask | None:
        """The task one id addresses in this session, or nothing.

        Nothing is an ordinary answer -- an id from a session that has since
        ended addresses no task -- and is not a failure here.
        """
        return self._tasks.get(str(task_id or ""))

    def cancel_task(self, task_id: str) -> bool:
        """Withdraw the running execution of one task (backlog 1039).

        Signals that execution's token; ``execute_task`` then records the task
        cancelled and raises ``AICancelled``. Callable from any thread. An id
        with nothing running -- idle, finished or unknown -- is an ordinary
        ``False`` and changes nothing; a cancel racing the end of a run leaves
        that run's own outcome. A synchronous tool call already in progress is
        not interrupted: its owner finishes it and the result is discarded.

        Returns:
            Whether a running execution was signalled.
        """
        with self._running_lock:
            token = self._running.get(str(task_id or ""))
        if token is None:
            return False
        logger.debug("cancelling AI task %s", task_id)
        token.cancel()
        return True

    def rehydrate_task(
        self, control: Any, task_id: str, changes: Mapping[str, Any],
    ) -> AITask:
        """Edit a held task by replacing it: a new idle task under the same id.

        ``changes`` updates the task's stored choices -- an empty value clears
        one back to the governed value -- and may replace its runtime facts.
        Hydration then resolves the whole task again from the governed rows and
        the resulting choices, so a value nobody chose follows configuration.
        The governed configuration itself is never changed.

        Raises:
            AISelectionError: when this session holds no task under ``task_id``.
            AIRequestError: when the task is running, or a change names
                something a task does not take.
            AIConfigurationError: as ``AITask.from_governed`` does.
        """
        current = self.task(task_id)
        if current is None:
            raise AISelectionError(f"This session holds no AI task '{task_id}'.")
        if current.status == AITask.RUNNING:
            raise AIRequestError(
                f"AI task '{current.id}' is running and cannot be edited until it ends."
            )
        self._refuse_unknown(changes)
        choices = {
            **current.choices,
            **{name: value for name, value in changes.items() if name in AITask.CHOICES},
        }
        facts = {name: changes.get(name, getattr(current, name)) for name in self._TASK_FACTS}
        task = AITask.from_governed(
            control, current.installation, task_id=current.id, **facts, **choices,
        )
        self._tasks[task.id] = task
        return task

    def _refuse_unknown(self, given: Mapping[str, Any]) -> None:
        """Refuse a name that is neither a task's choice nor its runtime fact."""
        unknown = sorted(set(given) - set(AITask.CHOICES) - set(self._TASK_FACTS))
        if unknown:
            raise AIRequestError(f"An AI task takes no {', '.join(unknown)}.")

    # -- composed asks -----------------------------------------------------

    def create_object(
        self, *, subject: str = "", subject_kind: str = "",
        subject_id: str = "", task: str = "",
    ) -> AIObject:
        """Compose one ask against this runtime, and remember it.

        **Seeded, not resolved.** The object carries no engine or instruction of
        its own: absent means the task's, then the defaults', which is what
        every override already means. So creating an ask cannot pin a model that
        configuration would later have changed.

        Args:
            subject: What is being asked about, already trusted by the caller.
                This runtime does not check it and never re-reads its source.
            subject_kind: What that subject is, for whatever draws it.
            subject_id: How the caller addresses that subject, if it has an
                identity apart from its text. Carried, never read here.
            task: Which configured task this ask is.

        Returns:
            The composed ask, addressable by its id.
        """
        composed = AIObject(
            subject=subject, subject_kind=subject_kind,
            # Where it began and what it is about start as the same thing, and
            # part company the moment a reader points it somewhere else.
            subject_id=subject_id, origin_id=subject_id, task=task,
        )
        self._objects[composed.id] = composed
        return composed

    def ai_object(self, object_id: str) -> AIObject | None:
        """The ask one id addresses, or nothing.

        Nothing is an ordinary answer -- an id from a page that has since been
        replaced addresses no ask -- and is not a failure here.
        """
        return self._objects.get(str(object_id or ""))

    def replace_object(self, composed: AIObject) -> AIObject:
        """Hold a changed ask under the id it already had.

        Every change is a new value, so this is how one becomes current. An ask
        this runtime never composed is refused rather than adopted: an id it
        does not know is a caller naming something that does not exist.
        """
        if composed.id not in self._objects:
            raise AISelectionError(f"This runtime composed no AI object '{composed.id}'.")
        self._objects[composed.id] = composed
        return composed

    def session(self, session_id: str = "") -> AISession:
        """A conversation against this runtime."""
        return AISession(self, session_id=session_id)

    # -- internals ---------------------------------------------------------

    def _model(
        self, model_identifier: str, base_url: str = "", credential_ref: str = "",
    ) -> Model:
        """The Pydantic AI model for one engine's configuration.

        Pydantic AI interprets ``<provider>:<model>`` and chooses the model and
        provider it names; this supplies only where the engine is reached and
        the credential, resolved now and handed straight to the provider. No
        provider or model naming is interpreted here.

        Raises:
            AIConfigurationError: when Pydantic AI cannot build a model from this
                configuration, or the credential reference cannot be resolved.
        """
        # Imported here, not at module level: Pydantic AI is needed only when a
        # model is built, and every application that composes an AI imports this.
        from pydantic_ai.exceptions import UserError
        from pydantic_ai.models import infer_model
        from pydantic_ai.providers import infer_provider_class

        api_key = self._credentials(credential_ref) or None

        def reached(provider_name: str) -> Any:
            """The provider Pydantic AI named, reached at this engine's base URL."""
            return infer_provider_class(provider_name)(
                base_url=base_url or None, api_key=api_key,
            )

        logger.debug("building the model for engine %s", model_identifier)
        try:
            return infer_model(model_identifier, provider_factory=reached)
        except (UserError, ValueError, TypeError) as error:
            raise AIConfigurationError(
                f"The engine '{model_identifier}' does not resolve to a model."
            ) from error

    def _profile_for(
        self, profile_id: str, task: AISettingsTask | None = None,
    ) -> AIProfile:
        """The profile a request named, then the task's, then the selected one."""
        wanted = (
            str(profile_id or "")
            or (task.profile_id if task else "")
            or self._settings.profile_id
        )
        if not wanted:
            available = self._registry.profiles()
            if not available:
                raise AISelectionError("This runtime offers no AI profiles.")
            raise AISelectionError(
                "No AI profile is selected and the request named none."
            )
        return self._registry.profile(wanted)

    def connections(self) -> tuple[str, ...]:
        """Every connection a task here may be pointed at, in name order.

        What a surface offers a reader to choose from. Names only: which of them
        reads what, and under whose authority, is not this object's to know.
        """
        return tuple(sorted(self._connections))

    def connection_for(self, connection: str = "", task: str = "") -> str:
        """The connection an ask queries through: its own, its task's, the default.

        Empty is an ordinary answer and means this ask queries nothing. The same
        precedence the profile follows, so a reader overriding one setting is not
        surprised by the other.

        Named by task key rather than by a settings object, because that is what
        an ask carries and the settings are this object's to look in.
        """
        configured = self._settings.task(task) if task else None
        return (
            str(connection or "")
            or (configured.connection if configured else "")
            or self._settings.connection
        )

    def _instruction_for(
        self, request: AIRequest, task: AISettingsTask | None = None,
    ) -> AIInstruction:
        """The instruction that applies: the request's, then the selection's.

        A request may carry an instruction outright rather than name one, which
        is how a caller sends a one-off system instruction without configuring
        it. Absent both, there is no instruction -- expressed as the value for
        that, never as a magic string.
        """
        if request.instruction is not None:
            return request.instruction
        wanted = (
            str(request.instruction_id or "")
            or (task.instruction_id if task else "")
            or self._settings.instruction_id
        )
        if not wanted:
            return AIInstruction(kind=AIInstructionKind.NONE)
        return self._registry.instruction(wanted)

    def _options_for(
        self, request: AIRequest, task: AISettingsTask | None,
    ) -> AIRequestOptions:
        """The request's options, with the effective temperature filled in.

        ``None`` on the request means "unset", not "send no temperature", so it
        is the point where a configured value applies. A request that states one
        keeps it, including an explicit ``0`` -- which is why this tests for
        ``None`` rather than falsiness, a configured zero being the value this
        estate actually uses.
        """
        if request.options.temperature is not None:
            return request.options
        temperature = task.temperature if task else None
        if temperature is None:
            temperature = self._settings.temperature
        if temperature is None:
            return request.options
        return replace(request.options, temperature=temperature)

    def _validated(self, settings: AISettings) -> AISettings:
        """Refuse a selection this runtime does not offer.

        Every level, not only the defaults: a task override naming an absent
        profile fails the same way a default does. Refusing one and accepting
        the other would let a task be configured into a state that only fails
        when that task next runs.
        """
        self._offered(
            settings.profile_id, settings.instruction_id, "", settings.connection,
        )
        for task in settings.tasks:
            self._offered(
                task.profile_id, task.instruction_id,
                f" for task '{task.name}'", task.connection,
            )
        return settings

    def _offered(
        self,
        profile_id: str,
        instruction_id: str,
        where: str,
        connection: str = "",
    ) -> None:
        """Refuse a profile, instruction or connection this runtime does not offer."""
        if connection and connection not in self._connections:
            # Not "no such connection": one may well exist. It has not said a
            # language model may use it, which is a different refusal and the
            # one a reader needs to hear.
            raise AISelectionError(
                f"No connection is offered to AI as '{connection}'{where}."
            )
        if profile_id and not self._registry.has_profile(profile_id):
            raise AISelectionError(
                f"No AI profile is configured as '{profile_id}'{where}."
            )
        if instruction_id and not self._registry.has_instruction(instruction_id):
            raise AISelectionError(
                f"No AI instruction is configured as '{instruction_id}'{where}."
            )

    def _replace(self, settings: AISettings) -> AISettings:
        """One canonical answer, replaced, and everyone interested told.

        An observer that raises does not stop the change or the other
        observers: the selection has already changed, and a listener's failure
        is its own.
        """
        self._settings = settings
        for listener in tuple(self._observers):
            try:
                listener(settings)
            except Exception:  # noqa: BLE001 -- an observer cannot break the owner
                continue
        return settings
