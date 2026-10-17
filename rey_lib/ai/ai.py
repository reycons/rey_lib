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

from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator

from rey_lib.ai.capabilities import AICapabilitySet
from rey_lib.ai.content import AIContentKind
from rey_lib.ai.contracts import ContractResolver
from rey_lib.ai.credentials import CredentialResolver
from rey_lib.ai.errors import (
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
from rey_lib.ai.tools import AITool
from rey_lib.ai.turn_strategy import ToolMechanism, resolve_tool_mechanism
from rey_lib.logs.app_logger import AppLogger

if TYPE_CHECKING:
    from pydantic_ai.models import Model

logger = AppLogger(__name__)

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
    ) -> None:
        self._registry = registry
        self._credentials = credentials or CredentialResolver()
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

    def execute_task(self, task: AITask) -> AIResult:
        """Perform one hydrated unit of work through Pydantic AI (backlog 1020).

        The task's choices are settled; this builds its model, runs it once and
        records the outcome through the task's own transitions. Synchronous:
        every caller runs on a thread without an event loop, and from inside a
        running loop this refuses rather than starting one of its own.

        Structured output asks Pydantic AI for the model's native JSON-schema
        output where the model's own profile supports it, prompted output
        otherwise, and never tool-call output -- tools are not this slice's, and
        a profile's tool flag is not a promise a local engine keeps. Whatever
        comes back, ``OutputParser`` validates it, and a failure is handed back
        to the model as a correction within this runtime's allowance.

        Raises:
            AIRequestError: when the task carries input other than text.
            AIExecutionError: when called inside a running event loop, or a
                usage limit stops the run.
            AIOutputError: when the output never satisfied its contract.
            AIProviderError: when the engine refused or could not be reached.
            AIConfigurationError: when the engine or the run cannot be set up.
        """
        import asyncio
        import uuid
        from datetime import datetime, timezone

        import pydantic_ai
        from pydantic_ai import Agent, ModelRetry, NativeOutput, PromptedOutput, StructuredDict
        from pydantic_ai.exceptions import (
            ModelAPIError,
            UnexpectedModelBehavior,
            UsageLimitExceeded,
            UserError,
        )

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

        task.started()
        started = datetime.now(timezone.utc)
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
                retries={"output": self._policy.validation_correction.max_corrections},
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
                task.task_key or "(default)", task.model_identifier,
                task.instruction_key or "(none)", type(output_type).__name__,
            )
            run = agent.run_sync(prompt, model_settings=settings or None)
            output = self._parse_task_output(task, schema, run.output)
        except UserError as error:
            failure: AIError = AIConfigurationError(
                f"AI task '{task.task_key or '(default)'}' could not be run.", cause=error,
            )
        except UsageLimitExceeded as error:
            failure = AIExecutionError(
                f"AI task '{task.task_key or '(default)'}' reached a usage limit.", cause=error,
            )
        except UnexpectedModelBehavior as error:
            failure = AIOutputError(
                f"AI task '{task.task_key or '(default)'}' produced no output that "
                "satisfies its contract.", cause=error,
            )
        except ModelAPIError as error:
            failure = AIProviderError(
                f"The engine for AI task '{task.task_key or '(default)'}' refused or "
                "could not be reached.", cause=error,
            )
        except AIError as error:
            task.failed(str(error))
            raise
        else:
            result = AIResult(
                output=output,
                execution=AIExecutionInfo(
                    execution_id=uuid.uuid4().hex,
                    profile_id=task.profile_key,
                    model=task.model_identifier,
                    instruction_id=task.instruction_key,
                    started_at=started.isoformat(),
                    ended_at=datetime.now(timezone.utc).isoformat(),
                    finish_reason=AIFinishReason.COMPLETED,
                ),
                usage=AIUsage(
                    input_tokens=int(run.usage.input_tokens or 0),
                    output_tokens=int(run.usage.output_tokens or 0),
                ),
            )
            task.answered(result)
            return result
        task.failed(str(failure))
        raise failure from failure.cause

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
