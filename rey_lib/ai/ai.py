"""The live AI: the one AI, hosted by ai_app (#1025).

ai_app constructs it and retains it; no other application holds one, and
another application reaches it only as ai_app's live object over the
transports. It runs governed AITasks -- composed from the governed AI
configuration, executed through the mechanism their engine states (Pydantic AI,
or Claude tooling) -- and states, as plain data, what that configuration
offers an installation.

Constructed from explicit resolved inputs, and afterwards it reaches back for
nothing: no ``ctx`` discovery, no ambient lookup, no module-level state.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

from rey_lib.ai.content import AIContentKind
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
from rey_lib.ai.output import OutputParser
from rey_lib.ai.requests import AIOutputKind
from rey_lib.ai.results import (
    AIExecutionInfo,
    AIFinishReason,
    AIOutput,
    AIResult,
    AIUsage,
)
from rey_lib.ai.streaming import AIEvent
from rey_lib.ai.task import AITask
from rey_lib.ai.tools import AIToolCall, AIToolResult, AIToolSet, Operation
from rey_lib.logs.app_logger import AppLogger

if TYPE_CHECKING:
    from pydantic_ai.models import Model

logger = AppLogger(__name__)

#: The in-process tool server the Claude CLI reaches a task's tools through: its
#: tools are named ``mcp__rey__<tool>`` to the CLI (backlog 1136).
_CLAUDE_TOOL_SERVER = "rey"

#: How many model requests one execution may make, tool turns included. The
#: value the live path has always run with; governing it is #1063's.
_MAX_TURNS = 8
#: How many times a tool call the model got wrong is handed back to it.
_MAX_TOOL_CORRECTIONS = 2
#: How many times an answer that fails its contract is handed back to the model.
_MAX_OUTPUT_CORRECTIONS = 0

#: The two representations a configured engine may be permitted to receive.
_ACCESS_MODES = frozenset({"redacted", "unredacted"})

__all__ = ["AI"]


class AI:
    """The live AI of the application that hosts it.

    Args:
        credentials: How a configured credential reference becomes its value at
            the moment a model is built. Holds no secret and no context.
        connections: The names of the connections a task here may query
            through -- each connection's own declaration, resolved by whatever
            composed this AI. Nothing here opens or holds a connection.
        connection_operations: Per connection name, the operations runtime
            composition bound for it (``capability_key -> operation``), so a
            task's governed connection selects how its tools reach their owners
            (backlog 741). Callables only: no connection is held here.
        parser: How a structured output is checked.
    """

    def __init__(
        self,
        *,
        credentials: CredentialResolver | None = None,
        connections: Iterable[str] = (),
        connection_operations: Mapping[str, Mapping[str, Operation]] | None = None,
        parser: OutputParser | None = None,
    ) -> None:
        self._credentials = credentials or CredentialResolver()
        self._connection_operations = {
            str(name): dict(operations)
            for name, operations in (connection_operations or {}).items()
        }
        self._connections = frozenset(
            str(name).strip() for name in connections if str(name).strip()
        )
        # The validator for a task this AI executes (backlog 1020): OutputParser
        # stays the owner of validation.
        self._parser = parser or OutputParser()
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

    def projection(self, control: Any, installation: str) -> dict[str, Any]:
        """What this AI offers an installation, as plain data (backlog 1041).

        Every section is read from the governed AI configuration through
        ``control``, each read once. Surveying creates no task.

        The tasks its governed configuration offers, in configured order, each as
        its key, its label and whether it is composed before it runs; the enabled
        engines and the instructions a task's choices may name (backlog 1047,
        D2); and ``settings``, the read-only survey of the defaults and each
        configured task that the AI Settings presentation draws.
        """
        configuration = control.ai_configuration(installation)
        engines = control.ai_engine_profiles()
        instructions = control.ai_instructions()
        return {
            "settings": self._settings_projection(configuration, engines, instructions),
            "tasks": [
                {
                    "task_key": str(row.get("task_key") or ""),
                    "label": str(row.get("task_label") or row.get("task_key") or ""),
                    "composed_first": bool(row.get("composed_first") or False),
                }
                for row in configuration
                if str(row.get("row_type")) == "task" and row.get("is_enabled") is not False
            ],
            "engines": [
                {
                    "profile_key": str(row.get("profile_key") or ""),
                    "model_identifier": str(row.get("model_identifier") or ""),
                }
                for row in engines
                if row.get("is_enabled") is not False
            ],
            "instructions": [
                {
                    "instruction_key": str(row.get("instruction_key") or ""),
                    "kind": str(row.get("kind") or ""),
                }
                for row in instructions
            ],
        }

    def _settings_projection(
        self,
        configuration: Iterable[Mapping[str, Any]],
        engines: Iterable[Mapping[str, Any]],
        instructions: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """The read-only survey of the governed AI settings, scope by scope.

        The group row is the default scope and each task row one task scope. A
        task states what it sets for itself, empty meaning inherit, and its
        effective values are computed here -- a browser overlaying one on the
        other would be a second resolver. Credentials never leave.

        Raises:
            AIConfigurationError: when a scope names an engine or instruction the
                configuration does not offer, or a contract carries no body.
            AISelectionError: when a scope names a connection no model may use,
                or an engine's access policy is absent or invalid.
        """
        profiles = {str(row.get("profile_key") or "").strip(): row for row in engines}
        offered = _offered_instructions(instructions)
        rows = list(configuration)

        def values(row: Mapping[str, Any], where: str, task: str = "") -> dict[str, Any]:
            profile_id = str(row.get("profile_key") or "").strip()
            instruction_id = str(row.get("instruction_key") or "").strip()
            connection = str(row.get("connection_name") or "").strip()
            if profile_id and profile_id not in profiles:
                raise AIConfigurationError(
                    f"AI configuration{where} names profile '{profile_id}', "
                    "which this runtime does not offer."
                )
            if instruction_id and instruction_id not in offered:
                raise AIConfigurationError(
                    f"AI configuration{where} names instruction '{instruction_id}', "
                    "which this runtime does not offer."
                )
            if connection and connection not in self._connections:
                raise AISelectionError(
                    f"No connection is offered to AI as '{connection}'"
                    f"{f' for task {task!r}' if task else ''}."
                )
            temperature = row.get("temperature")
            return {"profile_id": profile_id, "instruction_id": instruction_id,
                    "temperature": None if temperature is None else float(temperature),
                    "representation": str(row.get("representation") or "").strip(),
                    "connection": connection,
                    "composed_first": bool(row.get("composed_first") or False)}

        group = next((row for row in rows if str(row.get("row_type")) == "group"), {})
        tasks = [row for row in rows if str(row.get("row_type")) == "task"]
        default = values(group, ".default")
        scopes: list[dict[str, Any]] = []
        for row in (None, *tasks):
            name = str(row.get("task_key") or "").strip() if row is not None else ""
            stated = values(row, f".tasks['{name}']", name) if row is not None else dict(default)
            effective = dict(stated)
            if row is not None:
                for field in ("profile_id", "instruction_id", "representation", "connection"):
                    effective[field] = stated[field] or default[field]
                effective["temperature"] = (default["temperature"] if stated["temperature"] is None
                                            else stated["temperature"])
            engine = profiles.get(str(effective["profile_id"])) if effective["profile_id"] else None
            policy = (_access_policy(engine) if engine is not None
                      else {"allowed": [], "default": ""})
            instruction = offered.get(str(effective["instruction_id"])) if effective["instruction_id"] else None
            resource = None
            if instruction is not None and instruction["kind"] != "none":
                resource = {"content": instruction["text"],
                            "language": "json" if instruction["kind"] == "contract" else "text",
                            "title": instruction["label"], "read_only": True,
                            "capabilities": {"edit": False, "save": False}}
            label = (str(row.get("task_label") or row.get("task_key") or "")
                     if row is not None else "Default")
            scopes.append({"id": name if row is not None else "default",
                           "label": label, "is_default": row is None, "settings": stated,
                           "effective": effective, "representations": list(policy["allowed"]),
                           "instruction": resource})
        return {"available": True, "read_only": True, "scopes": scopes,
                "profiles": [{"id": key, "label": f"{row.get('provider') or ''} - {row.get('model') or ''}",
                              "provider": str(row.get("provider") or ""),
                              "model": str(row.get("model") or ""),
                              "access_policy": _access_policy(row)}
                             for key, row in profiles.items()],
                "instructions": [{"id": key, "label": one["label"], "kind": one["kind"]}
                                 for key, one in offered.items()],
                "connections": list(self.connections())}

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
            steps = {
                "pydantic_ai": self._run_pydantic_ai,
                "claude_agent_sdk": self._run_claude_agent_sdk,
            }
            step = steps.get(task.execution_mechanism)
            if step is None:
                raise AIConfigurationError(
                    f"AI task '{key}' names execution mechanism "
                    f"'{task.execution_mechanism or '(none)'}', which AI does not run."
                )
            # What the task's governed connection was composed with, then what
            # this caller binds: the connection selects, the caller may add.
            bound = {
                **self._connection_operations.get(task.connection, {}),
                **(operations or {}),
            }
            toolset = AIToolSet(task.tools, bound)
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
                    "output": _MAX_OUTPUT_CORRECTIONS,
                    "tools": _MAX_TOOL_CORRECTIONS,
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
                usage_limits=UsageLimits(request_limit=_MAX_TURNS),
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
            max_turns=_MAX_TURNS,
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
            corrections = _MAX_OUTPUT_CORRECTIONS
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
        if any(part.kind is not AIContentKind.TEXT for part in task.input.content):
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
        "task_key", "input", "subject", "subject_reference", "origin_id",
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

    def connections(self) -> tuple[str, ...]:
        """Every connection a task here may be pointed at, in name order.

        What a surface offers a reader to choose from. Names only: which of them
        reads what, and under whose authority, is not this object's to know.
        """
        return tuple(sorted(self._connections))


def _offered_instructions(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    """The governed instructions by key, as a reader sees them, in offer order.

    "None", then "Ad hoc text", then each contract as ``<contract> <version>``
    carrying its body -- the order and the labels the settings survey has
    always shown.

    Raises:
        AIConfigurationError: when a contract carries no body, or two
            instructions would show a reader the same label.
    """
    reserved: list[tuple[str, dict[str, str]]] = []
    contracts: list[tuple[str, dict[str, str]]] = []
    shown: dict[str, str] = {}
    for row in rows:
        key = str(row.get("instruction_key") or "").strip()
        kind = str(row.get("kind") or "none")
        if kind != "contract":
            reserved.append((key, {"kind": kind, "text": "",
                                   "label": "None" if kind == "none" else "Ad hoc text"}))
            continue
        body = row.get("body")
        if not body:
            raise AIConfigurationError(
                f"AI instruction '{key}' is a contract and carries no body, so "
                "nothing could be sent for it."
            )
        label = f"{row.get('contract_key')} {row.get('contract_version')}"
        if label in shown:
            raise AIConfigurationError(
                f"AI instructions '{key}' and '{shown[label]}' both show '{label}', "
                "so a reader could not tell them apart."
            )
        shown[label] = key
        contracts.append((key, {"kind": kind, "text": str(body), "label": label}))
    reserved.sort(key=lambda item: 0 if item[1]["kind"] == "none" else 1)
    return dict(reserved + contracts)


def _access_policy(engine: Mapping[str, Any]) -> dict[str, Any]:
    """Which representations an engine may receive, and the one it takes by default.

    The engine's own declaration is the only authority, and an absent or
    invalid one fails closed.

    Raises:
        AISelectionError: when the declaration is absent or invalid.
    """
    key = str(engine.get("profile_key") or "")
    declared = engine.get("access_allowed") or []
    if not isinstance(declared, (list, tuple)) or not declared:
        raise AISelectionError(f"AI profile '{key}' requires profile_access.allowed.")
    allowed: list[str] = []
    for value in declared:
        mode = str(value or "").strip()
        if mode not in _ACCESS_MODES:
            raise AISelectionError(
                "profile_access.allowed may contain only "
                f"{' or '.join(sorted(_ACCESS_MODES))}."
            )
        if mode not in allowed:
            allowed.append(mode)
    default = str(engine.get("access_default") or "").strip()
    if default not in allowed:
        raise AISelectionError(
            "profile_access.default must be a member of profile_access.allowed."
        )
    return {"allowed": allowed, "default": default}
