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

import json
import threading
from copy import deepcopy
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
from rey_lib.errors.error_utils import AppError
from rey_lib.logs.app_logger import AppLogger

if TYPE_CHECKING:
    from pydantic_ai.models import Model

logger = AppLogger(__name__)

#: The in-process tool server the Claude CLI reaches a task's tools through: its
#: tools are named ``mcp__rey__<tool>`` to the CLI (backlog 1136).
_CLAUDE_TOOL_SERVER = "rey"

#: How many times a tool call the model got wrong is handed back to it.
_MAX_TOOL_CORRECTIONS = 2
#: How many times an answer that fails its contract is handed back to the model.
_MAX_OUTPUT_CORRECTIONS = 0

#: The two representations a configured engine may be permitted to receive.
_ACCESS_MODES = frozenset({"redacted", "unredacted"})

#: The scope the group's defaults are addressed by; a task scope is its task_key.
_DEFAULT_SCOPE = "default"
#: The settings a scope may change (#1191). Compose before run is not one of them.
_EDITABLE_SETTINGS = ("profile_id", "instruction_id", "temperature", "representation",
                      "connection", "max_turns")
#: Each editable setting, as the AITask choice a new task takes it as.
_CHOICE_OF_SETTING = {"profile_id": "profile_key", "instruction_id": "instruction_key",
                      "temperature": "temperature", "representation": "representation",
                      "connection": "connection", "max_turns": "max_turns"}
#: Save, declared while an edit is unsaved: the command that persists the settings.
_SAVE_SETTINGS_ACTION = {"role": {"act": "save", "on": "settings"},
                         "command": {"command": "save_settings"}}
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
        # The installation's AI settings as this session holds them (#1191): per
        # installation, each scope's STATED values -- "default" and each task_key --
        # as last read from governed configuration, and the working copy edits
        # change. Save writes the working copy and re-reads; a new task hydrates
        # from the working copy, so an unsaved edit applies to it.
        self._workbench_selection: dict[str, str] = {}
        self._workbench_tasks: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        self._governed_settings: dict[str, dict[str, dict[str, Any]]] = {}
        self._working_settings: dict[str, dict[str, dict[str, Any]]] = {}

    def projection(self, control: Any, installation: str) -> dict[str, Any]:
        """What this AI offers an installation, as plain data (backlog 1041).

        Every section is read from the governed AI configuration through
        ``control``, each read once. Surveying creates no task.

        The tasks its governed configuration offers, in configured order, each as
        its key, its label and whether it is composed before it runs; the enabled
        engines and the instructions a task's choices may name (backlog 1047,
        D2); and ``settings``, the defaults and each configured task as this
        session holds them, with the edits and the Save they declare (#1191).
        """
        configuration = control.ai_configuration(installation)
        engines = control.ai_engine_profiles(installation)
        instructions = control.ai_instructions()
        stated = self._settings_of(control, installation, configuration)
        tasks = [
            {
                "task_key": str(row.get("task_key") or ""),
                "label": str(row.get("task_label") or row.get("task_key") or ""),
                "composed_first": bool(row.get("composed_first") or False),
            }
            for row in configuration
            if row.get("is_enabled") is not False
        ]
        return {
            "workbench": {
                "scope": self._workbench_selection.get(installation, _DEFAULT_SCOPE),
                "select": {"role": {"act": "select", "on": "task"},
                           "command": {"command": "select_task"}, "inputs": {"scope": ["scope"]}},
                "open": {"role": {"act": "open", "on": "workbench"},
                         "command": {"command": "open_workbench"}},
            },
            "settings": self._settings_projection(
                configuration, engines, instructions, stated,
                stated != self._governed_settings[installation],
            ),
            "tasks": tasks,
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
        stated: Mapping[str, Mapping[str, Any]],
        unsaved: bool,
    ) -> dict[str, Any]:
        """The AI settings this session holds, scope by scope, with what may be done.

        The default scope and each configured task, in the governed rows' order.
        A scope's ``settings`` are what it states for itself -- for a task, empty
        means inherit -- and its ``effective`` values are computed here: a browser
        overlaying one on the other would be a second resolver. Each scope
        declares the edits it takes; Save is declared while an edit is unsaved.
        Compose before run is read from the governed rows and is not editable.
        Credentials never leave.

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
            held = stated.get(task or _DEFAULT_SCOPE) or {}
            profile_id = str(held.get("profile_id") or "").strip()
            instruction_id = str(held.get("instruction_id") or "").strip()
            connection = str(held.get("connection") or "").strip()
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
            temperature = held.get("temperature")
            return {"profile_id": profile_id, "instruction_id": instruction_id,
                    "temperature": None if temperature is None else float(temperature),
                    "max_turns": AITask.turn_limit(held.get("max_turns")),
                    "representation": str(held.get("representation") or "").strip(),
                    "connection": connection,
                    "composed_first": bool(row.get("composed_first") or False)}

        # EVERY ROW CARRIES THE INSTALLATION'S DEFAULTS (ai.ai_installation_vw): the
        # default scope is read off the first row, and each row is one task.
        first = rows[0] if rows else {}
        tasks = rows
        default = values({"composed_first": first.get("default_composed_first")}, ".default")
        scopes: list[dict[str, Any]] = []
        for row in (None, *tasks):
            name = str(row.get("task_key") or "").strip() if row is not None else ""
            scope_stated = (values(row, f".tasks['{name}']", name) if row is not None
                            else dict(default))
            effective = dict(scope_stated)
            if row is not None:
                for field in ("profile_id", "instruction_id", "representation", "connection"):
                    effective[field] = scope_stated[field] or default[field]
                effective["temperature"] = (default["temperature"]
                                            if scope_stated["temperature"] is None
                                            else scope_stated["temperature"])
                effective["max_turns"] = (default["max_turns"] if scope_stated["max_turns"] is None
                                          else scope_stated["max_turns"])
            engine = profiles.get(str(effective["profile_id"])) if effective["profile_id"] else None
            policy = (_access_policy(engine) if engine is not None
                      else {"allowed": [], "default": ""})
            instruction = (offered.get(str(effective["instruction_id"]))
                           if effective["instruction_id"] else None)
            resource = None
            if instruction is not None and instruction["kind"] != "none":
                resource = {"content": instruction["text"],
                            "language": "json" if instruction["kind"] == "contract" else "text",
                            "title": instruction["label"], "read_only": True,
                            "capabilities": {"edit": False, "save": False}}
            label = (str(row.get("task_label") or row.get("task_key") or "")
                     if row is not None else "Default")
            scope_id = name if row is not None else _DEFAULT_SCOPE
            scopes.append({"id": scope_id,
                           "label": label, "is_default": row is None, "settings": scope_stated,
                           "effective": effective, "representations": list(policy["allowed"]),
                           "instruction": resource,
                           "actions": [_set_setting_action(scope_id, field)
                                       for field in _EDITABLE_SETTINGS]})
        return {"available": True, "read_only": False, "scopes": scopes,
                "actions": [_SAVE_SETTINGS_ACTION] if unsaved else [],
                # Labelled by the profile's own key (user, 2026-11-03): profiles
                # sharing one provider and model are told apart by it.
                "profiles": [{"id": key,
                              "label": key,
                              "provider": str(row.get("provider") or ""),
                              "model": str(row.get("model") or ""),
                              "access_policy": _access_policy(row)}
                             for key, row in profiles.items()],
                "instructions": [{"id": key, "label": one["label"], "kind": one["kind"]}
                                 for key, one in offered.items()],
                "connections": list(self.connections())}

    # -- the installation's AI settings (#1191) -----------------------------

    def select_workbench_task(self, control: Any, installation: str, scope: str = "") -> None:
        """Select the configured task for this AI's Workbench; unset means Default."""
        scope = scope or _DEFAULT_SCOPE
        if scope not in self._settings_of(control, installation):
            raise AISelectionError(f"This installation's AI settings hold no scope {scope!r}.")
        self._workbench_selection[installation] = scope

    def workbench_scope(self, installation: str) -> str:
        """The scope this AI's Workbench is selected on: a task_key, or "" for Default."""
        scope = self._workbench_selection.get(installation, _DEFAULT_SCOPE)
        return "" if scope == _DEFAULT_SCOPE else scope

    def workbench_task(self, control: Any, installation: str) -> AITask:
        """The selected live task, with the working settings, never implicitly saved.

        The AI owns selection, default fallback, resolution and task lifetime.
        Reopening retains the task's identity and prompt. Changed settings edit
        that task through the existing hydration boundary; unrelated tasks stay as-is.
        """
        scope = self._workbench_selection.get(installation, _DEFAULT_SCOPE)
        effective = _effective(self._settings_of(control, installation), scope)
        choices = {choice: effective[field] for field, choice in _CHOICE_OF_SETTING.items()}
        key = (installation, scope)
        held = self._workbench_tasks.get(key)
        if held is None:
            task = self.hydrate_task(control, installation,
                                     task_key="" if scope == _DEFAULT_SCOPE else scope, **choices)
        else:
            task = self.task(held[0])
            if task is None:
                raise AISelectionError(f"This session holds no AI task {held[0]!r}.")
            if held[1] != choices:
                task = self.rehydrate_task(control, task.id, choices)
        self._workbench_tasks[key] = (task.id, dict(choices))
        return task

    def set_setting(self, control: Any, installation: str, scope: str, field: str,
                    value: Any) -> None:
        """Change one setting of one scope in this session's working settings.

        Nothing is written: Save persists the working settings. An empty value is
        no value -- for a task, inheritance of the default. A task created after
        this hydrates with the edit; a task already held keeps its configuration.

        Raises:
            AIRequestError: when the field is not an editable setting, or the
                temperature is not a number.
            AISelectionError: when the scope is not one of this installation's,
                or the value names something the configuration does not offer.
        """
        if field not in _EDITABLE_SETTINGS:
            raise AIRequestError(f"AI settings take no editable field {field!r}.")
        scopes = self._settings_of(control, installation)
        if scope not in scopes:
            raise AISelectionError(f"This installation's AI settings hold no scope {scope!r}.")
        if field == "max_turns" and scope == _DEFAULT_SCOPE and AITask.turn_limit(value) is None:
            raise AIRequestError("The default maximum AI turns must be a positive integer.")
        scopes[scope][field] = self._offered_setting(control, installation, field, value)

    def save_settings(self, control: Any, installation: str) -> None:
        """Persist this session's working settings, then re-read them as governed.

        Only a scope whose working values differ from what was last read is
        written, and each with exactly its stated values: an inherited task
        value is written as NULL, never as the default it resolves to. Every
        changed value is validated again before anything is written.

        Raises:
            AIRequestError, AISelectionError: as :meth:`set_setting` does, before
                any write.
        """
        scopes = self._settings_of(control, installation)
        governed = self._governed_settings[installation]
        changed = [scope for scope, values in scopes.items() if values != governed.get(scope)]
        for scope in changed:
            for field in _EDITABLE_SETTINGS:
                self._offered_setting(control, installation, field, scopes[scope][field])
        for scope in changed:
            values = {
                "profile_key": scopes[scope]["profile_id"] or None,
                "instruction_key": scopes[scope]["instruction_id"] or None,
                "temperature": scopes[scope]["temperature"],
                "max_turns": scopes[scope]["max_turns"],
                "representation": scopes[scope]["representation"] or None,
                "connection_name": scopes[scope]["connection"] or None,
            }
            logger.debug("saving AI settings %s for installation %s", scope, installation)
            if scope == _DEFAULT_SCOPE:
                control.update_ai_installation_defaults(installation, **values)
            else:
                control.update_ai_installation_task(installation, scope, **values)
        self._read_settings(control, installation)

    def _settings_of(self, control: Any, installation: str,
                     configuration: Iterable[Mapping[str, Any]] | None = None,
                     ) -> dict[str, dict[str, Any]]:
        """This session's working settings for one installation, read once.

        ``configuration`` is the installation's rows where the caller has them
        already (``projection``), so the one read is not repeated.
        """
        if installation not in self._working_settings:
            self._read_settings(control, installation, configuration)
        return self._working_settings[installation]

    def _read_settings(self, control: Any, installation: str,
                       configuration: Iterable[Mapping[str, Any]] | None = None) -> None:
        """Read the installation's stated settings; the working copy becomes them."""
        rows = control.ai_configuration(installation) if configuration is None else configuration
        governed = _stated_scopes(rows)
        self._governed_settings[installation] = governed
        self._working_settings[installation] = deepcopy(governed)

    def _offered_setting(self, control: Any, installation: str, field: str,
                         value: Any) -> Any:
        """One setting's value as the installation's configuration offers it, or a refusal."""
        if field == "max_turns":
            return AITask.turn_limit(value)
        if field == "temperature":
            if value is None or str(value).strip() == "":
                return None
            try:
                return float(value)
            except (TypeError, ValueError) as error:
                raise AIRequestError(f"An AI temperature is a number, not {value!r}.") from error
        chosen = str(value or "").strip()
        if not chosen:
            return ""
        if field == "profile_id" and chosen not in {
                str(row.get("profile_key") or "")
                for row in control.ai_engine_profiles(installation)}:
            raise AISelectionError(f"No AI engine profile is configured as {chosen!r}.")
        if field == "instruction_id" and chosen not in _offered_instructions(
                control.ai_instructions()):
            raise AISelectionError(f"No AI instruction is configured as {chosen!r}.")
        if field == "representation" and chosen not in _ACCESS_MODES:
            raise AISelectionError(f"No data representation is named {chosen!r}.")
        if field == "connection" and chosen not in self._connections:
            raise AISelectionError(f"No connection is offered to AI as {chosen!r}.")
        return chosen

    def _unsaved_choices(self, installation: str, task_key: str) -> dict[str, Any]:
        """What a new task takes from this session's unsaved edits, as its choices.

        Only a value whose effective working setting differs from the effective
        governed one: the governed rows already carry the rest.
        """
        working = self._working_settings.get(installation)
        governed = self._governed_settings.get(installation)
        if working is None or working == governed:
            return {}
        scope = task_key if task_key in working else _DEFAULT_SCOPE
        now, before = _effective(working, scope), _effective(governed or {}, scope)
        return {
            choice: now[field]
            for field, choice in _CHOICE_OF_SETTING.items()
            if now[field] != before[field] and now[field] not in ("", None)
        }

    def execute_task(
        self,
        task: AITask,
        operations: Mapping[str, Operation] | None = None,
        observe: Callable[[AIEvent], None] | None = None,
        *,
        record: Callable[[AIEvent], None] | None = None,
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
        observers = [sink for sink in (record, observe) if sink is not None]

        def tell(event: AIEvent) -> None:
            """Notify independent sinks: a broken reader cannot stop durable evidence."""
            for sink in tuple(observers):
                try:
                    sink(event)
                except Exception as exc:  # noqa: BLE001 — canonical execution boundary
                    observers.remove(sink)
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
            output, usage, (tool_calls, tool_results) = step(
                task, prompt, schema, toolset, token,
                tell if observe is not None or record is not None else None, execution_id,
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
        except Exception as cause:
            error = AIExecutionError(f"AI task '{key}' execution failed.", cause=cause)
            task.failed(str(error))
            tell(AIEvent.failed(execution_id, str(error)))
            raise error from cause
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
                # The run's tool exchange, as the engine reported it, whether or
                # not anyone observed the run: beside the payload, never in it.
                tool_calls=tool_calls,
                tool_results=tool_results,
            )
            task.answered(result)
            tell(AIEvent.completed(execution_id, result))
            return result
        finally:
            with self._running_lock:
                del self._running[task.id]

    def execute_recorded(
        self,
        task: AITask,
        operations: Mapping[str, Operation] | None = None,
        evidence: Any = None,
        *,
        source: Mapping[str, Any] | None = None,
        source_record_type: str = "",
        runtime: Any = None,
        observe: Callable[[AIEvent], None] | None = None,
    ) -> AIResult:
        """Run ``task`` as :meth:`execute_task` does, recording what was sent and what
        came back under the parent its caller supplied (#1305).

        The task's ``origin_id`` is the calling application's place -- its run, the
        record a new record is parented to, its nesting and correlation. With one,
        and ``evidence`` (the run log of the process executing, the vehicle only),
        this records before the engine is called the package it is sent -- the
        task's contract beside ``source``, the record the application sent, in the
        structure the finalization stage used to write -- and, when the run ends,
        its result (LLM_INTERPRETATION for the log interpretation, LLM_ANALYSIS_RESULT
        for any other task) or its failure or cancellation (LLM_ANALYSIS_FAILURE).
        Every record is that caller's run's: ``RunLog.append_under``.

        Tool executions also journal canonical lifecycle/call/result events as
        INFO records, incrementally, so failure does not discard the exchange.
        These use the caller's parent when supplied, otherwise the hosting
        application's own RunLog, linked by task_run_id and execution_id.
        They are independent of the live presentation observer.

        Without an origin no caller-owned package/result is written. An origin
        that is not an execution context is refused before the engine is called. A
        write that fails is reported by the run log and changes nothing here: the
        answer, the events and the raised error are what ``execute_task`` gives.
        ``runtime`` only resolves the paths of the references the task's contract
        declares.

        Raises:
            AIRequestError: When the origin is malformed or incomplete; and as
                ``execute_task`` does.
            AIError, AICancelled: As ``execute_task`` does, after the failure or
                cancellation has been recorded.
        """
        from rey_lib.ai import evidence as recording

        parent = recording.origin_of(task)
        task_run_id = task.run_id

        def trace(event: AIEvent) -> None:
            payload = recording.trace_of(task, event, task_run_id=task_run_id)
            if payload is None:
                return
            fields = {"record_group": "results", "ai_trace": payload}
            if parent is not None:
                evidence.append_under(parent, "INFO", message="AI execution trace", **fields)
            else:
                # With no caller parent, the hosting application's own RunLog
                # remains the owner; task_run_id links its INFO to the task run.
                evidence.append("INFO", message="AI execution trace", **fields)

        tracing = {"record": trace} if evidence is not None and task.tools else {}
        if parent is None or evidence is None:
            return self.execute_task(task, operations, observe=observe, **tracing)
        evidence.append_under(
            parent, recording.PACKAGE_RECORD, record_group="results",
            **recording.package_of(task, source, source_record_type, runtime))
        try:
            result = self.execute_task(task, operations, observe=observe, **tracing)
        except AICancelled as withdrawn:
            evidence.append_under(parent, recording.FAILURE_RECORD, record_group="results",
                                  **recording.failure_of(task, "cancelled", str(withdrawn)))
            raise
        except AIError as error:
            evidence.append_under(parent, recording.FAILURE_RECORD, record_group="results",
                                  **recording.failure_of(task, "failed", str(error)))
            raise
        record_type = (recording.INTERPRETATION_RECORD
                       if task.task_key == recording.INTERPRETATION_TASK
                       else recording.RESULT_RECORD)
        evidence.append_under(parent, record_type, record_group="results",
                              **recording.result_of(task, result))
        return result

    def _run_pydantic_ai(
        self,
        task: AITask,
        prompt: str,
        schema: dict[str, Any] | None,
        toolset: AIToolSet,
        token: Any,
        tell: Callable[[AIEvent], None] | None,
        execution_id: str,
    ) -> tuple[AIOutput, AIUsage, tuple[tuple[AIToolCall, ...], tuple[AIToolResult, ...]]]:
        """Run one execution through Pydantic AI: :meth:`execute_task`'s step.

        Builds the task's model, runs it once and returns the validated output,
        what it used, and its tool exchange -- each function tool call and its
        result, read from the run's own messages, so it is kept whether or not
        the run was streamed. Every failure leaves as its AIError. Structured output
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
        import asyncio
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
        from pydantic_ai.messages import RetryPromptPart, ToolCallPart, ToolReturnPart

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
            # The synchronous SDK entry uses the current loop. Own that loop
            # explicitly instead of relying on deprecated implicit creation.
            with asyncio.Runner() as runtime:
                runtime.get_loop()
                run = agent.run_sync(
                    prompt, model_settings=settings or None,
                    usage_limits=UsageLimits(request_limit=task.max_turns),
                    event_stream_handler=relay if tell is not None else None,
                    cancellation_token=token,
                )
            output = self._parse_task_output(task, schema, run.output)
            # The run's tool exchange, from its own messages in their order --
            # as _translated states it for an observer: a result is failed where
            # the owner refused the call (ToolFailed) or it was sent back for
            # correction (ModelRetry). An output correction answers no tool call.
            calls: list[AIToolCall] = []
            results: list[AIToolResult] = []
            for message in run.all_messages():
                for part in getattr(message, "parts", ()):
                    if isinstance(part, ToolCallPart):
                        calls.append(AIToolCall(id=part.tool_call_id, name=part.tool_name,
                                                arguments=part.args_as_dict()))
                    elif isinstance(part, RetryPromptPart) and part.tool_name:
                        results.append(AIToolResult(call_id=part.tool_call_id, failed=True,
                                                    message=str(part.content)))
                    elif isinstance(part, ToolReturnPart):
                        failed = getattr(part, "outcome", "") == "failed"
                        results.append(AIToolResult(
                            call_id=part.tool_call_id,
                            value=None if failed else part.content,
                            failed=failed,
                            message=str(part.content) if failed else "",
                        ))
            exchange = (tuple(calls), tuple(results))
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
            retry = error.__cause__
            refused = getattr(retry, "__cause__", None)
            if isinstance(retry, ModelRetry) and isinstance(refused, AppError) and not isinstance(refused, AIOutputError):
                raise AIExecutionError(
                    f"AI task '{key}' exhausted its tool-correction allowance: {refused}",
                    cause=error,
                ) from error
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
        ), exchange

    def _run_claude_agent_sdk(
        self,
        task: AITask,
        prompt: str,
        schema: dict[str, Any] | None,
        toolset: AIToolSet,
        token: Any,
        tell: Callable[[AIEvent], None] | None,
        execution_id: str,
    ) -> tuple[AIOutput, AIUsage, tuple[tuple[AIToolCall, ...], tuple[AIToolResult, ...]]]:
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
        interrupts the CLI. Its tool exchange is every tool block the session
        received, translated as for an observer and kept whether or not there
        is one.

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
            max_turns=task.max_turns,
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
            calls.clear()
            results.clear()
            while True:
                await client.query(message)
                outcome: Any = None
                async for item in client.receive_response():
                    if isinstance(item, ResultMessage):
                        outcome = item
                        continue
                    for event in self._sdk_translated(execution_id, item):
                        # Kept for the result; told only to an observer.
                        if event.tool_call is not None:
                            calls.append(event.tool_call)
                        if event.tool_result is not None:
                            results.append(event.tool_result)
                        if tell is not None:
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

        calls: list[AIToolCall] = []
        results: list[AIToolResult] = []

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
            output, used = asyncio.run(run())
            return output, used, (tuple(calls), tuple(results))
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
        """What is sent: the subject, then the task's context, then its text, as one prompt.

        All of them, because all were given: a prompt about a subject is not a
        prompt instead of one, and the context is what the live object holds. A
        structured part -- the context a launcher supplied -- is sent as its JSON;
        text as it is. Any other kind of input is refused.
        """
        if any(part.kind not in (AIContentKind.TEXT, AIContentKind.STRUCTURED)
               for part in task.input.content):
            raise AIRequestError(
                f"AI task '{task.task_key or '(default)'}' carries input other than "
                "text or structured data, which this execution does not send."
            )
        parts = [
            task.subject,
            *(json.dumps(part.value, indent=2, default=str) for part in task.input.content
              if part.kind is AIContentKind.STRUCTURED),
            *(str(part.value) for part in task.input.content
              if part.kind is AIContentKind.TEXT),
        ]
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
        # The session's unsaved edits, beneath anything the caller chose (#1191).
        facts = {**self._unsaved_choices(installation, str(facts.get("task_key") or "")),
                 **facts}
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

    def connection_capabilities(self, task: AITask) -> frozenset[str]:
        """The capability keys this AI binds itself from ``task``'s governed connection.

        Read-only (recipe ai.give_a_task_a_tool step 5): the operations
        :meth:`execute_task` binds from this AI's connection operations for the task's
        connection. A host that binds its own operations for one execution binds only
        the task's other granted capabilities, so it never replaces one this AI binds.
        """
        return frozenset(self._connection_operations.get(task.connection, {}))

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


def _set_setting_action(scope: str, field: str) -> dict[str, Any]:
    """The declared edit of one setting of one scope: its command and where its value goes."""
    return {"role": {"act": "set", "on": "settings", "field": field},
            "command": {"command": "set_setting", "scope": scope, "field": field},
            "inputs": {"value": ["value"]}}


def _stated_scopes(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each scope's stated settings, from control.f_ai_configuration_get rows.

    Every row (ai.ai_installation_vw) carries the installation's stated defaults
    and that task's own stated values, NULL where it inherits (#1296).
    """
    listed = list(rows)
    first = listed[0] if listed else {}

    def stated(row: Mapping[str, Any], prefix: str) -> dict[str, Any]:
        temperature = row.get(f"{prefix}temperature")
        return {"profile_id": str(row.get(f"{prefix}profile_key") or ""),
                "instruction_id": str(row.get(f"{prefix}instruction_key") or ""),
                "temperature": None if temperature is None else float(temperature),
                "max_turns": AITask.turn_limit(row.get(f"{prefix}max_turns")),
                "representation": str(row.get(f"{prefix}representation") or ""),
                "connection": str(row.get(f"{prefix}connection_name") or "")}

    scopes = {_DEFAULT_SCOPE: stated(first, "default_")}
    for row in listed:
        if row.get("task_key"):
            scopes[str(row["task_key"])] = stated(row, "task_")
    return scopes


def _effective(scopes: Mapping[str, Mapping[str, Any]], scope: str) -> dict[str, Any]:
    """One scope's effective settings: its own values, else the default's."""
    default = dict(scopes.get(_DEFAULT_SCOPE) or {})
    own = dict(scopes.get(scope) or {})
    return {field: (own.get(field) if own.get(field) not in ("", None) else default.get(field))
            for field in _EDITABLE_SETTINGS}
