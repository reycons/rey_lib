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
from typing import Any, Callable, Iterator

from rey_lib.ai.capabilities import AICapabilitySet
from rey_lib.ai.contracts import ContractResolver
from rey_lib.ai.errors import AISelectionError
from rey_lib.ai.execution import AIExecutor
from rey_lib.ai.instructions import AIInstruction, AIInstructionKind
from rey_lib.ai.output import OutputParser
from rey_lib.ai.profiles import AIProfile
from rey_lib.ai.registry import AIRegistry
from rey_lib.ai.requests import AIRequest, AIRequestOptions, ResolvedAIRequest
from rey_lib.ai.results import AIResult
from rey_lib.ai.policies import DEFAULT_EXECUTION_POLICY, AIExecutionPolicy
from rey_lib.ai.sessions import AISession
from rey_lib.ai.settings import AISettings, AISettingsTask
from rey_lib.ai.streaming import AIEvent

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
    ) -> None:
        self._registry = registry
        self._executor = executor or AIExecutor(
            registry=registry,
            contracts=contracts,
            parser=parser,
            policy=policy,
        )
        self._observers: list[Callable[[AISettings], None]] = []
        self._settings = self._validated(settings or AISettings())

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
        return ResolvedAIRequest(
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

    def execute(self, request: AIRequest, *, session_id: str = "") -> AIResult:
        """Resolve this request and run it."""
        return self._executor.execute(self.resolve(request, session_id=session_id))

    def stream(self, request: AIRequest, *, session_id: str = "") -> Iterator[AIEvent]:
        """Resolve this request and run it, reporting canonical events."""
        return self._executor.stream(self.resolve(request, session_id=session_id))

    def session(self, session_id: str = "") -> AISession:
        """A conversation against this runtime."""
        return AISession(self, session_id=session_id)

    # -- internals ---------------------------------------------------------

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
        self._offered(settings.profile_id, settings.instruction_id, "")
        for task in settings.tasks:
            self._offered(
                task.profile_id, task.instruction_id, f" for task '{task.name}'",
            )
        return settings

    def _offered(self, profile_id: str, instruction_id: str, where: str) -> None:
        """Refuse a profile or instruction this runtime does not offer."""
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
