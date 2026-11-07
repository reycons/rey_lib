"""WorkflowStep -- one ApplicationOperation instantiated in a workflow.

The leaf of the workflow object model (backlog 950). A step is one fully
configured ApplicationOperation: bound once, at construction, to an operation
its application publishes and to the implementation the application's approved
catalog holds for it, with its resolved settings (already composed by
hydration) and its declarations (``apply_only``, ``always_run``).

It owns its own state: PENDING until it first runs, RUNNING while its Run
executes, then that Run's outcome. The Workflow decides when it is released; the
released step creates its own Run, calls it, and takes its state from the Run's
outcome when the Run returns. Running a step changes its state; the step stays
in its Workflow's graph, and a later release changes its state again. It tells
its one observer, its Workflow, each time its state changes.

Built beside the recovered procedural engine (backlog 949); nothing here imports
it.

Public API
----------
WorkflowStep    A bound, configured operation instance.
"""

from __future__ import annotations

import csv
import io
import json
import re
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from rey_lib.config.applications import Application, ApplicationOperation
from rey_lib.config.inventory import to_plain_data
from rey_lib.config.operation_contract import contract_review
from rey_lib.content import Content
from rey_lib.errors.error_utils import AppError
from rey_lib.formatting import markdown_to_html
from rey_lib.logs import AppLogger
from rey_lib.run.run import Run

if TYPE_CHECKING:
    from rey_lib.workflow.workflow_process import WorkflowProcess

__all__ = ["WorkflowStep", "definition_section", "fixed_field", "form_fields", "setting_fields",
           "setting_form"]

_logger = AppLogger(__name__)

#: The subject type of a step's child Run.
_STEP_SUBJECT = "step"

#: A step's state before it has run.
PENDING = "PENDING"
#: A step's state while its Run executes.
RUNNING = "RUNNING"


class WorkflowStep:
    """One ApplicationOperation instantiated in a workflow, with its own settings.

    Attributes
    ----------
    id : str
        The step's authored identity.
    process_name : str
        The authored process the step was declared under.
    label : str
        The step's human label; the step name in run records.
    ordinal : int
        The step's position in its workflow's execution order.
    operation : ApplicationOperation
        The published operation this step instantiates.
    implementation : Callable
        The approved implementation bound to that operation.
    settings : dict
        This instance's configured settings, already composed.
    apply_only : bool
        Held for the Workflow: the step is not run in a dry run.
    always_run : bool
        Held for the Workflow: the step stays eligible after an earlier failure.
    process : WorkflowProcess | None
        The WorkflowProcess that owns this step; set by that process when it is
        constructed, None before.
    state : str
        ``PENDING``, ``RUNNING`` while its Run executes, then that Run's status.
    current_run : Run | None
        The Run this step last created, None before it first runs.
    governed_id : Any
        The governed row this step was hydrated from (control.workflow_step id),
        None for a step built otherwise (backlog 1126).
    """

    def __init__(
        self,
        *,
        id: str,
        process_name: str,
        label: str,
        ordinal: int,
        application: Application,
        operation_name: str,
        approved: Mapping[str, Callable[..., Any]],
        settings: Mapping[str, Any],
        apply_only: bool = False,
        always_run: bool = False,
        governed_id: Any = None,
        configured_settings: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Bind the step to its published operation and approved implementation.

        Parameters
        ----------
        id, process_name, label, ordinal
            The step's authored identity and position.
        application : Application
            The application the step's process instantiates.
        operation_name : str
            The name of the operation, as the application publishes it.
        approved : Mapping[str, Callable]
            The application's approved implementation catalog.
        settings : Mapping[str, Any]
            The step's composed settings for this operation instance.
        apply_only, always_run : bool
            Engine flags, held for the Workflow.
        governed_id : Any
            The governed row it was hydrated from, where it was.

        Raises
        ------
        AppError
            When the application does not publish the operation, or publishes
            it without approving an implementation for it.
        """
        if not id:
            raise AppError("a workflow step is missing required 'id'.")
        self.id = id
        self.governed_id = governed_id
        self.process_name = process_name
        self.label = label or id
        self.ordinal = ordinal
        self.operation = _published_operation(application, operation_name, id)
        self.implementation = _approved_implementation(
            application, approved, operation_name, id
        )
        # Deep-copied: a handler mutating a nested setting must not reach the
        # mapping hydration passed in, or any other holder of it.
        self.settings: dict[str, Any] = deepcopy(dict(settings))
        # The settings as governed, {} expressions intact, beside the resolved ones
        # the run reads (#1325); the resolved ones when none were given apart.
        self.configured_settings: dict[str, Any] = deepcopy(dict(
            settings if configured_settings is None else configured_settings))
        self.apply_only = apply_only
        self.always_run = always_run
        self.process: Optional["WorkflowProcess"] = None
        self.state = PENDING
        self.current_run: Optional[Run] = None
        self._observer: Any = None
        # How configured settings resolve: the Workflow's own paths, bound by it.
        self._resolve: Optional[Callable[[Mapping[str, Any]], dict[str, Any]]] = None
        # How its operation's setting references are read, bound by its Workflow (#1330).
        self._read_references: Optional[Callable[[], Any]] = None
        # Those references by setting path: None until a field is first selected.
        self._references: Optional[dict[str, dict[str, Any]]] = None
        # The field its editor selected, whose reference it projects (#1330).
        self._selected: Optional[str] = None

    @classmethod
    def hydrate(
        cls,
        row: Mapping[str, Any],
        *,
        application: Application,
        approved: Mapping[str, Callable[..., Any]],
    ) -> "WorkflowStep":
        """The step one governed row states (backlog 963).

        Takes the row as control.workflow_flat_vw shapes it, with its
        ``step_settings`` already resolved by the workflow's hydration; those
        are the operation's settings only and are passed through unchanged.

        ``apply_only`` and ``always_run`` are this step's orchestration
        properties, read from
        ``row.workflow_config.execution.steps[row.workflow_step_name]``. Both
        must be booleans; a missing or invalid value is refused, never filled
        from this constructor's defaults.

        Raises:
            AppError: Naming the step, when either control is missing or not a
                boolean, or when the constructor refuses the binding.
        """
        step_id = str(row.get("workflow_step_name") or "")
        execution = (row.get("workflow_config") or {}).get("execution") or {}
        controls = (execution.get("steps") or {}).get(step_id) or {}
        for name in ("apply_only", "always_run"):
            if not isinstance(controls.get(name), bool):
                raise AppError(
                    f"step '{step_id}': workflow_config.execution.steps states no "
                    f"boolean '{name}' for it."
                )
        return cls(
            id=step_id,
            process_name=str(row.get("configured_process_name") or ""),
            label=str(row.get("step_label") or ""),
            ordinal=int(row["step_ordinal"]),
            application=application,
            operation_name=str(row.get("application_operation_name") or ""),
            approved=approved,
            settings=row.get("step_settings") or {},
            configured_settings=row.get("configured_step_settings"),
            apply_only=controls["apply_only"],
            always_run=controls["always_run"],
            governed_id=row.get("workflow_step_id"),
        )

    def contract_refusal(self, workflow_name: str) -> str:
        """Why this step's settings do not satisfy its operation's contract, or ''.

        The settings are reviewed whole against the operation's
        ``contract_definition`` (JSON Schema, backlog 939). Unsupported keys are
        tolerated with a warning naming them; a violation refuses. The Workflow
        calls this before releasing the step.
        """
        unsupported, violations = contract_review(
            self.operation.contract_definition or {},
            to_plain_data(self.settings),
        )
        if unsupported:
            _logger.warning(
                "workflow '%s': process '%s' carries settings the contract of "
                "implementation '%s' does not support; they are ignored: %s",
                workflow_name, self.process_name, self.operation.name,
                "; ".join(unsupported),
            )
        if not violations:
            return ""
        return (
            f"workflow '{workflow_name}': process '{self.process_name}' settings do not "
            "satisfy the "
            f"contract of implementation '{self.operation.name}': "
            + "; ".join(violations)
        )

    def bind_references(self, read: Callable[[], Any]) -> None:
        """How its operation's setting references are read (code.reference, #1330).

        Nothing is read here: they are read the first time one of its fields is
        selected (:meth:`apply_selection`), then held.
        """
        self._read_references = read

    def apply_selection(self, name: str) -> None:
        """Select one of its fields, whose reference it then projects (#1330).

        A selection is state (#1323): the editor reports the field selected, and this
        step's next projection carries that field's instructions. Its operation's
        references are read the first time a field is selected, then held.

        Raises:
            AppError: When the step has no field ``name``.
        """
        if name not in {one["name"] for one in self.fields()}:
            raise AppError(f"step '{self.id}' has no field '{name}'.")
        if self._references is None:
            read = self._read_references
            self._references = {str(one.get("setting_path")): dict(one)
                                for one in (read() if read is not None else None) or []}
        self._selected = name

    def bind_resolution(self, resolve: Callable[[Mapping[str, Any]], dict[str, Any]]) -> None:
        """How this step's configured settings resolve: its Workflow's paths (#1325)."""
        self._resolve = resolve

    def facts(self) -> list[dict[str, str]]:
        """What this step is, as its editor states it (#1325): label and value, in order."""
        return [
            {"label": "Step", "value": f"{self.ordinal}  {self.id}"},
            {"label": "Operation", "value": self.operation.name},
            {"label": "Process",
             "value": self.process.name if self.process is not None else self.process_name},
        ]

    def fields(self) -> list[dict[str, Any]]:
        """Every setting this step's operation offers, as its editor draws it (#1325).

        One per property of the operation's published ``contract_definition``, in
        its order, then any configured setting the contract does not name -- each
        configured beside resolved, with the command that edits it where it may be
        edited (:func:`setting_fields`). Nothing is written.
        """
        return form_fields(self.form())

    def form(self) -> list[dict[str, Any]]:
        """Its settings as its editor presents them (#1325), in two sections:

        - **Workflow** -- what the workflow defines for this step: its name, label,
          position, and the engine controls (``apply_only``, ``always_run``) the
          workflow's ``execution.steps`` states. Shown, not edited here.
        - **<application> · <operation>** -- the settings the application exposes to
          the workflow through this operation's contract, in the sections it
          describes (:func:`setting_form`), each editable where the contract allows.
        """
        application = self.process.application.name if self.process is not None else ""
        subject = f"step:{self.ordinal}"
        references = self._references or {}

        def fixed(name: str, label: str, value: Any, hint: str) -> dict[str, Any]:
            """One field the workflow defines, selectable for its reference."""
            return fixed_field(name, label, value, hint, subject, self._selected, references)

        return [
            definition_section("Workflow", [
                fixed("step.name", "name", self.id, "The step's name in the workflow."),
                fixed("step.label", "label", self.label, "What the step is called."),
                fixed("step.ordinal", "ordinal", self.ordinal,
                      "Its position in the workflow's order."),
                fixed("step.apply_only", "apply_only", self.apply_only,
                      "Held back on a dry run (workflow config execution.steps)."),
                fixed("step.always_run", "always_run", self.always_run,
                      "Runs even after an earlier step failed "
                      "(workflow config execution.steps)."),
            ]),
            definition_section(
                " · ".join(one for one in (application, self.operation.name) if one),
                setting_form(self.configured_settings, self.settings,
                             self.operation.contract_definition, subject,
                             references, self._selected),
                self.operation.description),
        ]

    def update(self, name: str, value: Any) -> None:
        """Edit one configured setting of this live step (#1325); nothing is written.

        ``name`` is the setting's path as its field states it -- ``scope``, or
        ``sources[0].source.path`` for one value inside a list of source sets.
        ``value`` arrives as the editor's text and is read as the contract's type
        there; blank clears it (a list item is removed). The configured settings
        keep the {} expressions as given, and the resolved settings the run reads
        follow them. Refused while the Workflow runs, for a path neither the contract
        nor the step holds, and where the edit adds a contract violation.

        Raises:
            AppError: When the edit is refused.
        """
        schema = dict(self.operation.contract_definition or {})
        path = _path_of(self.id, name)
        spec = _spec_at(schema, path)
        if spec is None and _value_at(self.configured_settings, path) is _ABSENT:
            raise AppError(
                f"step '{self.id}': operation '{self.operation.name}' has no setting '{name}'.")
        if getattr(self._observer, "directing", False):
            raise AppError(f"step '{self.id}' cannot be edited while its workflow runs.")
        configured = deepcopy(self.configured_settings)
        if (spec or {}).get("type") == "array" and isinstance(value, str):
            # A list of single values arrives as one comma-separated line.
            item = dict((spec or {}).get("items") or {})
            values = [one.strip() for one in next(csv.reader([value]), []) if one.strip()]
            _set_at(self.id, configured, path,
                    [_from_text(self.id, name, one, item) for one in values])
        elif value is None or value == "":
            _clear_at(configured, path)
        else:
            _set_at(self.id, configured, path, _from_text(self.id, name, value, spec or {}))
        resolved = (self._resolve(configured) if self._resolve is not None
                    else deepcopy(configured))
        _unsupported, before = contract_review(schema, to_plain_data(self.settings))
        _unsupported, after = contract_review(schema, to_plain_data(resolved))
        added = [one for one in after if one not in before]
        if added:
            raise AppError(f"step '{self.id}': {'; '.join(added)}")
        self.configured_settings = configured
        self.settings = resolved

    def observe(self, observer: Any) -> None:
        """Be the one object told when this step's state changes (its Workflow).

        The observer's ``step_changed(step)`` is called after each change.
        """
        self._observer = observer

    def run(
        self,
        *,
        control: Any,
        parent_run_id: Optional[int],
        log: Any,
        apply: bool,
        data: dict[str, Any],
        metadata: dict[str, Any],
        ctx: Any,
        workflow_name: str,
    ) -> Run:
        """Run this released step through its own Run, and return that Run.

        In the released step's order (backlog 964/965): check its contract, create
        its Run as a child of ``parent_run_id`` (the Application Run), become
        RUNNING, execute through that Run, then take the Run's outcome as its
        state. A refused contract raises before any Run exists and leaves the
        state unchanged. ``control`` and the execution values -- ``log``,
        ``apply``, and the ``data`` and ``metadata`` the direction's steps share --
        are the releasing Workflow's; the step does not find or keep them.

        Raises:
            AppError: When the step's settings do not satisfy its contract.
        """
        refusal = self.contract_refusal(workflow_name)
        if refusal:
            raise AppError(refusal)
        child = self._start_run(control, parent_run_id, log=log, apply=apply,
                                data=data, metadata=metadata)
        self._become(RUNNING)
        child.execute(self, ctx, sequence=self.ordinal, workflow_name=workflow_name)
        self._become(child.status)
        return child

    def not_released(
        self,
        *,
        control: Any,
        parent_run_id: Optional[int],
        log: Any,
        apply: bool,
        data: dict[str, Any],
        metadata: dict[str, Any],
        workflow_name: str,
    ) -> Run:
        """Record terminal non-release and retain its Run as this Step's outcome.

        Workflow decides eligibility. This Step owns its Run association and
        state; the existing Run records the unchanged non-release evidence.
        No operation or operation-contract check is performed.
        """
        child = self._start_run(control, parent_run_id, log=log, apply=apply,
                                data=data, metadata=metadata)
        child.not_released(step=self, sequence=self.ordinal, workflow_name=workflow_name)
        self._become(child.status)
        return child

    def _start_run(
        self,
        control: Any,
        parent_run_id: Optional[int],
        *,
        log: Any,
        apply: bool,
        data: dict[str, Any],
        metadata: dict[str, Any],
    ) -> Run:
        """Create this step's own Run, a child of ``parent_run_id``, and keep it.

        The Run works in the execution values it is given: the same ``data`` and
        ``metadata`` objects, not copies, so what one step writes a later step of
        the same direction reads.
        """
        child = Run.start(
            control,
            subject_type=_STEP_SUBJECT,
            subject_id=self.id,
            subject_name=self.label,
            app_name=self.process.application.name if self.process is not None else "",
            settings=self.settings,
            parent_run_id=parent_run_id,
        )
        child.log = log
        child.apply = apply
        child.data = data
        child.metadata = metadata
        self.current_run = child
        return child

    def _become(self, state: str) -> None:
        """Take ``state``, and tell the observer this step changed."""
        self.state = state
        if self._observer is not None:
            self._observer.step_changed(self)


def _published_operation(
    application: Application, operation_name: str, step_id: str
) -> ApplicationOperation:
    """The operation the application publishes under that name, or a refusal.

    Read from the application's own published operations; an operation another
    application publishes is not found here, so it is refused the same way.
    """
    for operation in application.workflow_operations:
        if operation.name == operation_name:
            return operation
    raise AppError(
        f"step '{step_id}' names operation '{operation_name}', which application "
        f"'{application.name}' does not publish."
    )


def _approved_implementation(
    application: Application,
    approved: Mapping[str, Callable[..., Any]],
    operation_name: str,
    step_id: str,
) -> Callable[..., Any]:
    """The implementation the application approves for the operation, or a refusal.

    Published but not approved is a gap in the application's own answer, never
    something to resolve by importing a name.
    """
    implementation = approved.get(operation_name)
    if implementation is None:
        raise AppError(
            f"step '{step_id}': operation '{operation_name}' is published by "
            f"'{application.name}' but is not in its approved implementation catalog."
        )
    return implementation


def _as_text(value: Any) -> str:
    """A setting as the editor's text: blank, a word, or its JSON."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _from_text(step: str, name: str, value: Any, spec: Mapping[str, Any]) -> Any:
    """The editor's text read as the contract's type for this setting.

    Raises:
        AppError: When the text is not a value of that type.
    """
    if not isinstance(value, str):
        return value
    kind = spec.get("type")
    try:
        if kind == "boolean":
            if value not in ("true", "false"):
                raise ValueError(value)
            return value == "true"
        if kind == "integer":
            return int(value)
        if kind == "number":
            return float(value)
        if kind in ("array", "object") or (kind is None and value[:1] in ("[", "{")):
            return json.loads(value)
    except ValueError as exc:
        raise AppError(
            f"step '{step}': '{value}' is not a {kind or 'JSON'} value for '{name}'.") from exc
    return value


def setting_fields(
    configured: Optional[Mapping[str, Any]],
    resolved: Optional[Mapping[str, Any]],
    schema: Optional[Mapping[str, Any]] = None,
    subject: Optional[str] = None,
) -> list[dict[str, Any]]:
    """One live object's settings, one field per individual value (#1325).

    :func:`setting_form` flattened: every field of every section, in order, each
    labelled by its whole path (``sources › feed_inbox › source › path``).
    """
    return _flattened(setting_form(configured, resolved, schema, subject))


def setting_form(
    configured: Optional[Mapping[str, Any]],
    resolved: Optional[Mapping[str, Any]],
    schema: Optional[Mapping[str, Any]] = None,
    subject: Optional[str] = None,
    references: Optional[Mapping[str, Mapping[str, Any]]] = None,
    selected: Optional[str] = None,
) -> list[dict[str, Any]]:
    """One live object's settings as its editor presents them: sections and fields (#1325).

    Walks what the application publishes (``schema``, its operation's contract)
    down to each single value, and states the hierarchy it describes: an object's
    properties (in its order, then any other key the object holds) are a section,
    a list of groups is a section holding one section per item -- a source set is
    labelled by its ``name`` -- and a single value, or a list of single values
    (comma-separated), is a field. Where no schema describes a part, the configured
    values themselves are walked. A value the schema names and the object does not
    hold is still a field, blank.

    A section states its ``name`` (its path), ``label``, ``hint`` and ``summary`` --
    its first values as one line (``name=feed_inbox · enabled=true · …``), drawn
    while it is collapsed -- and its ``items``. A field states its path, label,
    control, configured and resolved values as text, choices, help, and -- only
    where ``subject`` names the object that edits it and the schema describes it --
    the command that edits it. Where ``subject`` is named, every field declares the
    command that selects it (#1330); only the ``selected`` field carries its
    instructions, from ``references``. Everything is the object's; the editor draws it.
    """
    return _items([], [], dict(schema or {}), dict(configured or {}), dict(resolved or {}),
                  False, subject, dict(references or {}), selected)


def form_fields(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every field of a form, in order, labelled by its whole path of section labels."""
    return _flattened(items)


def _flattened(items: list[dict[str, Any]], within: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Every field beneath ``items``, labelled by its whole path of section labels."""
    made: list[dict[str, Any]] = []
    for item in items:
        if "items" in item:
            made.extend(_flattened(item["items"], (*within, str(item["label"]))))
        else:
            made.append({**item, "label": " › ".join((*within, str(item["label"])))})
    return made


#: How many of a section's items its collapsed summary names before "…".
_SUMMARY_ITEMS = 3


def _section(path: list[Any], label: list[str], spec: Mapping[str, Any],
             items: list[dict[str, Any]]) -> dict[str, Any]:
    """One group of settings, with the one-line summary it shows while collapsed."""
    named = []
    for item in items[:_SUMMARY_ITEMS]:
        if "items" in item:
            named.append(str(item["label"]))
        else:
            value = str(item["value"])
            named.append(f"{str(item['label']).removesuffix(' *')}="
                         f"{value if len(value) <= 40 else value[:39] + '…'}")
    if len(items) > _SUMMARY_ITEMS:
        named.append("…")
    return {
        "name": _path_text(path),
        "label": label[-1] if label else "",
        "hint": str(spec.get("description") or ""),
        "summary": " · ".join(named),
        "items": items,
    }


#: A value an object does not hold, as distinct from one it holds as None.
_ABSENT = object()


def _items(path: list[Any], label: list[str], spec: Mapping[str, Any], configured: Any,
           resolved: Any, required: bool, subject: Optional[str],
           references: Mapping[str, Mapping[str, Any]],
           selected: Optional[str]) -> list[dict[str, Any]]:
    """The sections and fields at ``path``: the hierarchy the schema describes."""
    kind = spec.get("type") or (
        "object" if isinstance(configured, Mapping) or isinstance(resolved, Mapping) else
        "array" if isinstance(configured, list) or isinstance(resolved, list) else None)
    if kind == "object" and not spec.get("enum"):
        properties = dict(spec.get("properties") or {})
        needed = set(spec.get("required") or [])
        held = configured if isinstance(configured, Mapping) else {}
        after = resolved if isinstance(resolved, Mapping) else {}
        items: list[dict[str, Any]] = []
        for key in dict.fromkeys([*properties, *held, *after]):
            items.extend(_items([*path, key], [*label, str(key)], dict(properties.get(key) or {}),
                                held.get(key, _ABSENT), after.get(key, _ABSENT), key in needed,
                                subject, references, selected))
        return items if not path else [_section(path, label, spec, items)]
    if kind == "array" and _is_value_list(spec, configured, resolved):
        # A list of single values is one setting: its values, comma-separated.
        return [_field(path, label, spec, _as_csv(configured), _as_csv(resolved),
                       required, subject, references, selected)]
    if kind == "array":
        each = dict(spec.get("items") or {})
        held = configured if isinstance(configured, list) else []
        after = resolved if isinstance(resolved, list) else []
        items = []
        for at in range(max(len(held), len(after))):
            one = held[at] if at < len(held) else _ABSENT
            named = one.get("name") if isinstance(one, Mapping) else None
            items.extend(_items([*path, at], [*label, str(named) if named else f"#{at + 1}"],
                                each, one, after[at] if at < len(after) else _ABSENT, False,
                                subject, references, selected))
        return [_section(path, label, spec, items)]
    return [_field(path, label, spec, _as_text(None if configured is _ABSENT else configured),
                   _as_text(None if resolved is _ABSENT else resolved), required, subject,
                   references, selected)]


#: How each schema type reads in a field's help.
_TYPE_HELP = {
    "string": "text", "boolean": "true or false", "integer": "a whole number",
    "number": "a number", "object": "a group of settings",
}


def _field(path: list[Any], label: list[str], spec: Mapping[str, Any], value: str,
           resolved: str, required: bool, subject: Optional[str],
           references: Optional[Mapping[str, Mapping[str, Any]]] = None,
           selected: Optional[str] = None) -> dict[str, Any]:
    """One setting as its editor draws it: label, control, values, help, its commands,
    and -- only while it is the selected field -- its instructions."""
    choices = spec.get("enum")
    named = _path_text(path)
    return {
        "name": named,
        # Its own name: its sections say where it is, and the full path is in the help.
        "label": f"{label[-1] if label else named}{' *' if required else ''}",
        "control": ("select" if choices else
                    "checkbox" if spec.get("type") == "boolean" else "text"),
        "value": value,
        "resolved": resolved,
        **({"choices": [_as_text(one) for one in choices]} if choices else {}),
        "hint": _help(named, spec, required),
        "required": required,
        **({"instructions": _instructions(
            named, label[-1] if label else named, spec, required, value, resolved,
            (references or {}).get(re.sub(r"\[\d+\]", "", named)))}
           if selected is not None and named == selected else {}),
        **_selection_command(subject, named),
        **({"command": {"command": "operate", "object": subject,
                        "operation": "update", "arguments": {"name": named}},
            "value_input": "arguments.value"} if subject is not None and spec else {}),
    }


def _help(named: str, spec: Mapping[str, Any], required: bool) -> str:
    """What a reader hovering over a setting needs: what it is and what it may be.

    Every line is the application's own contract read aloud -- its description, the
    type it states, its choices, its default, how many values -- never invented.
    """
    lines = [str(spec["description"])] if spec.get("description") else []
    lines.extend(_expectations(spec, required))
    lines.append(f"Setting: {named}")
    return "\n".join(lines)


def _expectations(spec: Mapping[str, Any], required: bool) -> list[str]:
    """What the application's contract says a setting may be, line by line."""
    lines: list[str] = []
    kind = spec.get("type")
    if kind == "array":
        item = dict(spec.get("items") or {})
        of = _TYPE_HELP.get(str(item.get("type")), "values")
        lines.append(f"A list of {of}, comma-separated.")
        if item.get("enum"):
            lines.append("Each one of: " + ", ".join(_as_text(one) for one in item["enum"]) + ".")
        if spec.get("minItems"):
            lines.append(f"At least {spec['minItems']}.")
    elif spec.get("enum"):
        lines.append("One of: " + ", ".join(_as_text(one) for one in spec["enum"]) + ".")
    elif kind in _TYPE_HELP:
        lines.append(f"Expects {_TYPE_HELP[kind]}.")
    if "default" in spec:
        lines.append(f"Default: {_as_text(spec['default']) or '(blank)'}.")
    if required:
        lines.append("Required.")
    if not spec:
        lines.append("Not described by the application; shown as held.")
    return lines


def _instructions(named: str, label: str, spec: Mapping[str, Any], required: bool,
                  value: str, resolved: str,
                  reference: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """The detailed instructions for one setting, as canonical Content (#1330).

    The governed reference (code.reference) says what goes in the setting, how the
    application uses it and an example; the contract says what it may be; the step
    says what it holds. Markdown, with the HTML the shared renderer prepares.
    """
    title = str((reference or {}).get("title") or label)
    parts = [f"## {title}", ""]
    if reference and reference.get("instructions"):
        parts += [str(reference["instructions"]).strip(), ""]
    else:
        parts += ["_No reference is written for this setting yet._", ""]
    if reference and reference.get("example"):
        parts += ["**Example**", "", "```", str(reference["example"]).strip(), "```", ""]
    expected = _expectations(spec, required)
    if spec.get("description"):
        expected.insert(0, str(spec["description"]))
    if expected:
        parts += ["### What the application expects", ""]
        parts += [f"- {one}" for one in expected] + [""]
    parts += ["### In this step", "",
              f"- Configured: `{value}`" if value else "- Configured: _(blank)_",
              f"- Resolved: `{resolved}`" if resolved else "- Resolved: _(blank)_",
              f"- Setting: `{named}`"]
    text = "\n".join(parts)
    return Content(name=title, text=text, language="markdown",
                   html=markdown_to_html(text), html_trusted=True).projection()["content"]


def _is_value_list(spec: Mapping[str, Any], configured: Any, resolved: Any) -> bool:
    """Whether a list holds single values (one setting) rather than groups of settings."""
    item_type = dict(spec.get("items") or {}).get("type")
    if item_type is not None:
        return item_type not in ("object", "array")
    values = [*(configured if isinstance(configured, list) else []),
              *(resolved if isinstance(resolved, list) else [])]
    return all(not isinstance(one, (Mapping, list)) for one in values)


def _as_csv(values: Any) -> str:
    """A list of single values as one comma-separated line; blank for none."""
    if not isinstance(values, list):
        return ""
    line = io.StringIO()
    csv.writer(line, lineterminator="").writerow(_as_text(one) for one in values)
    return line.getvalue()


def _path_text(path: list[Any]) -> str:
    """A setting's path as its field names it: ``sources[0].source.path``."""
    text = ""
    for part in path:
        text += f"[{part}]" if isinstance(part, int) else (f".{part}" if text else str(part))
    return text


def _path_of(step: str, name: str) -> list[Any]:
    """``sources[0].source.path`` as its parts.

    Raises:
        AppError: When the name is not a setting path.
    """
    parts: list[Any] = []
    for key, index in re.findall(r"([^.\[\]]+)|\[(\d+)\]", name or ""):
        parts.append(int(index) if index else key)
    if not parts or _path_text(parts) != name:
        raise AppError(f"step '{step}': '{name}' is not a setting path.")
    return parts


def _spec_at(schema: Mapping[str, Any], path: list[Any]) -> Optional[dict[str, Any]]:
    """The schema describing the value at ``path``, or None where it describes none."""
    spec: Mapping[str, Any] = schema
    for part in path:
        spec = (spec.get("items") if isinstance(part, int)
                else dict(spec.get("properties") or {}).get(part)) or {}
        if not spec:
            return None
    return dict(spec)


def _value_at(data: Any, path: list[Any]) -> Any:
    """The value at ``path``, or ``_ABSENT``."""
    for part in path:
        if isinstance(part, int):
            if not isinstance(data, list) or part >= len(data):
                return _ABSENT
        elif not isinstance(data, Mapping) or part not in data:
            return _ABSENT
        data = data[part]
    return data


def _set_at(step: str, data: dict[str, Any], path: list[Any], value: Any) -> None:
    """Set the value at ``path``, making the objects on the way; a list index must exist.

    Raises:
        AppError: When the path runs through a value that is not a container, or
            names a list item that does not exist.
    """
    place: Any = data
    for at, part in enumerate(path):
        last = at == len(path) - 1
        if isinstance(part, int):
            if not isinstance(place, list) or part >= len(place):
                raise AppError(f"step '{step}': '{_path_text(path)}' names no list item.")
        elif not isinstance(place, dict):
            raise AppError(f"step '{step}': '{_path_text(path)}' runs through a single value.")
        if last:
            place[part] = value
            return
        if not isinstance(part, int) and part not in place:
            place[part] = {}
        place = place[part]


def _clear_at(data: dict[str, Any], path: list[Any]) -> None:
    """Remove the value at ``path``: a key goes, a list item is taken out."""
    parent = _value_at(data, path[:-1]) if len(path) > 1 else data
    last = path[-1]
    if isinstance(last, int) and isinstance(parent, list) and last < len(parent):
        parent.pop(last)
    elif isinstance(parent, dict):
        parent.pop(last, None)


def definition_section(label: str, items: list[dict[str, Any]],
                       hint: str = "") -> dict[str, Any]:
    """One top-level group of an object's settings: who defines them (#1325).

    The Workflow's own definition, or the settings an application exposes. Its
    summary is its first values, as every section's is.
    """
    return _section([f"@{label}"], [label], {"description": hint}, items)


def fixed_field(name: str, label: str, value: Any, hint: str = "",
                subject: Optional[str] = None, selected: Optional[str] = None,
                references: Optional[Mapping[str, Mapping[str, Any]]] = None) -> dict[str, Any]:
    """A setting the workflow defines, shown and not edited here (#1325).

    Where ``subject`` is named it declares the command that selects it, and while it
    is the ``selected`` field it carries its instructions (#1330).
    """
    text = _as_text(value)
    return {
        "name": name,
        "label": label,
        "control": "checkbox" if isinstance(value, bool) else "text",
        "value": text,
        "resolved": text,
        "hint": "\n".join(one for one in (hint, f"Setting: {name}") if one),
        "required": False,
        **({"instructions": _instructions(name, label, {"description": hint} if hint else {},
                                          False, text, text, (references or {}).get(name))}
           if selected is not None and name == selected else {}),
        **_selection_command(subject, name),
    }


def _selection_command(subject: Optional[str], name: str) -> dict[str, Any]:
    """The command that selects field ``name`` of ``subject`` (#1330); none without one."""
    if subject is None:
        return {}
    return {"selection_command": {"command": "operate", "object": subject,
                                  "operation": "apply_selection", "arguments": {"name": name}}}
