"""Applications, as the runtime objects the estate's other collections already are.

``ctx.tools``, ``ctx.connections`` and ``ctx.data_sources`` are canonical
collections: configuration declares them, the load resolves them once, and every
consumer projects what it needs from the result. Applications were the exception -- each reader re-entered
the registry, re-normalized whatever shape it found, and re-resolved dynamic
choices on every call, against one application named in a branch.

This is the construction that makes an application the same kind of thing. It
runs once, at the end of the config load, and what it produces is what the tree,
the opening path and execution read.

**Declaration in, object out.** ``ctx.apps`` is the declaration and is never
written to. A parameter that declares where its choices come from has that
declaration *consumed*: the object carries the resolved values and not the
instruction that produced them, so nothing downstream can resolve a second time
or resolve differently.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ClassVar, Mapping

from rey_lib.config.operation_contract import check_contract, check_definition
from rey_lib.config.registration import REGISTRATION_GROUP, discover_registrations
from rey_lib.errors.error_utils import ConfigError

__all__ = [
    "Application",
    "ApplicationCapability",
    "ApplicationCommand",
    "ApplicationCommandParameter",
    "ApplicationMode",
    "ApplicationModeGroup",
    "ApplicationOperation",
    "ApplicationParameterGroup",
    "INVOCATION_HOSTED",
    "INVOCATION_RUN",
    "JSON_VALUE_TYPE",
    "build_applications",
    "configured_application",
    "hosted_command",
]

#: The value type of a parameter that carries one governed structured value
#: (#1143). Transported as one canonical JSON argument; what the structure means
#: is the receiving application's to validate.
JSON_VALUE_TYPE = "json"

#: How a command may be invoked (#1166, #1223): ``run`` -- an ordinary command the
#: execution service and the application runner start; ``hosted`` -- it needs
#: the attached app channel of ``LaunchedApp.launch``, so the runner does not
#: offer it. A hosted command exposes the reusable multi-object hosting contract
#: (rey_lib.run.HostedObjects: open / projection / release over its channel);
#: how long an instance is retained is the invoking context's, never the
#: command's. Declared per command; absent means ``run``.
INVOCATION_RUN = "run"
INVOCATION_HOSTED = "hosted"
_INVOCATIONS = frozenset({INVOCATION_RUN, INVOCATION_HOSTED})

#: Where a parameter's choices may be resolved from, and how each collection
#: becomes option values.
#:
#: Closed on purpose. This is not an attribute lookup on ctx: a source nobody
#: implemented is a configuration fault worth seeing at load, not an empty
#: dropdown discovered later. Each entry earns its place when a declaration
#: actually needs it, and each says how a member becomes a value -- the answer
#: the Console's enrichment already gave, kept rather than reinvented.
_CHOICE_SOURCES: dict[str, str] = {
    "tools": "tools",
    "connections": "connections",
    # Every connection that is a database (backlog 673). ``connections`` keeps
    # meaning every provider; a parameter that can only name a database asks
    # for this one.
    "database_connections": "connections",
    # Every HTTP connection, selected positively (backlog 668).
    "http_connections": "connections",
    "data_sources": "data_sources",
}

#: The empty form of each collection a choice source reads, as it is written:
#: a list, except ``tools``, which is a mapping of groups (backlog 293).
_EMPTY_COLLECTION: dict[str, Any] = {"tools": {}}


def with_choice_collections(raw: dict[str, Any]) -> dict[str, Any]:
    """The configuration with every choice-source collection present, empty where undeclared.

    A registration is the same in every installation; the collections its
    choices read are per installation. One that an installation does not
    declare is therefore ordinary, and is answered as an empty collection --
    so a collection MISSING from the context still means one thing only: it
    was read before it was built (backlog 293).

    Args:
        raw: The assembled configuration, before it is wrapped as the context.

    Returns:
        The same configuration with each collection set where it was absent.
        A declared collection is never touched.
    """
    filled = dict(raw)
    for collection in sorted(set(_CHOICE_SOURCES.values())):
        if filled.get(collection) is None:
            empty = _EMPTY_COLLECTION.get(collection, [])
            filled[collection] = type(empty)()
    return filled


#: Sources ANSWERED AT RUNTIME rather than read from a collection on ctx, each
#: given the context: the choices are what the runtime answers, not what an
#: installation's YAML lists.
_REGISTRY_CHOICES: dict[str, Any] = {
    "http_operations": lambda ctx: _http_operations(ctx),
}


def _http_operations(ctx: Any) -> tuple[str, ...]:
    """The installation's enabled HTTP operations, as the control database holds them (813).

    A runtime without a control database offers none: absence of control is an
    ordinary state (``open_shared_control``), and a transform that names an
    operation is refused where it resolves, not where its choices are listed.
    """
    control = getattr(ctx, "shared_control", None)
    if control is None:
        return ()
    return tuple(
        str(job["adapter_key"])
        for job in control.http_operation(ctx.installation.name)
        if job.get("is_enabled")
    )


def _database_members(members: Any) -> list[Any]:
    """The connections[] records that are databases."""
    from rey_lib.db.connection import is_database_connection  # noqa: PLC0415

    return [member for member in (members or []) if is_database_connection(member)]


def _http_members(members: Any) -> list[Any]:
    """The connections[] records that are HTTP connections."""
    from rey_lib.db.connection import is_http_connection  # noqa: PLC0415

    return [member for member in (members or []) if is_http_connection(member)]


#: The sources that offer a SUBSET of their collection, and the selection.
_CHOICE_FILTERS: dict[str, Any] = {
    "database_connections": _database_members,
    "http_connections": _http_members,
}


@dataclass(frozen=True)
class ApplicationMode:
    """One alternative within a mode group.

    ``icons`` names the marks that stand for this alternative, in the order they
    are drawn. A TUPLE because one alternative can be a sequence: a load shape is
    a movement, and the pair of ends with an arrow between them is what says
    which movement it is.

    Names only, and never markup. What the marks look like belongs to the surface
    that draws them, which resolves a name through its own icon library; a
    distribution supplying artwork is refused everywhere in this estate.

    Declaring none is ordinary. The label is what the alternative IS, and a
    surface with no glyph to draw falls back to it.
    """

    name: str
    label: str = ""
    icons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApplicationModeGroup:
    """One exclusive choice a command offers, and the alternatives it holds.

    A command whose parameters are alternatives -- load this one file to a
    configured destination, or to a named table, or load every configured
    source -- can only express that as a runtime refusal today. Declaring it
    lets a surface offer the shapes instead of offering every field at once and
    rejecting the combination afterwards.
    """

    name: str
    label: str = ""
    default: str = ""
    modes: tuple[ApplicationMode, ...] = ()


@dataclass(frozen=True)
class ApplicationCommandParameter:
    """One declared parameter of one command.

    ``possible_values`` is resolved: either stated outright by the declaration
    or read from the collection the declaration named, once, while this object
    was built. Which of the two it was is deliberately not recorded -- a
    consumer sees values, and has nothing to re-resolve with.

    ``mode_membership`` and ``required_when`` map a **group name** to the modes
    of that group. The group is named because ``mode_groups`` is plural and two
    groups may each declare a mode of the same name; a bare list would say
    nothing about which group it meant.

    Empty ``mode_membership`` means the parameter is always active, which is
    what every parameter declared before this existed carries.

    ``required_when`` is separate from ``required`` rather than a conditional
    reading of it. ``required`` keeps its global, unconditional meaning for
    every consumer -- including ones that have never heard of modes, such as
    the Console's application inventory -- so no reader can see ``required``
    true without that being true in every mode.
    """

    name: str
    value_type: str = ""
    required: bool = False
    description: str = ""
    positional: bool = False
    possible_values: tuple[str, ...] = ()
    #: Which modes this parameter belongs to, keyed by mode group. Membership
    #: is a conjunction: every declared group must be on one of its named
    #: modes for the parameter to be active.
    mode_membership: tuple[tuple[str, tuple[str, ...]], ...] = ()
    #: The modes in which this parameter must have a value, keyed the same way.
    required_when: tuple[tuple[str, tuple[str, ...]], ...] = ()
    #: What an empty control says it would do. For a choice, the label of the
    #: empty option an optional parameter is given.
    placeholder: str = ""
    #: Where a surface draws it: with the other fields, or beside the action
    #: that runs the command. An execution mode is not part of what is being
    #: defined, and saying so is the declaration's business, not the reader's.
    placement: str = "form"
    #: Which of a load's OBJECTS this parameter configures: ``source``,
    #: ``transform``, ``destination``, or nothing.
    #:
    #: THE ONE THING THE OTHER FIELDS CANNOT SAY. ``mode_membership`` says
    #: which SHAPE a parameter belongs to and ``placement`` says WHERE it is
    #: drawn; neither says which part of a load it describes. Once a source may
    #: be a file or a connection-and-statement, a flat list of names cannot --
    #: ``connection`` is ambiguous on its face.
    #:
    #: IT WAS ``endpoint``, and the name was too narrow. A load composes three
    #: objects -- source, transform, destination -- and a transform is not an
    #: END of anything. Naming two of three was a field that could not hold
    #: the middle one.
    #:
    #: EMPTY IS A REAL ANSWER, not an omission to be guessed at. A parameter
    #: that is not one end of anything says nothing, and a surface that finds
    #: some of a command's parameters declaring an end and some not has an
    #: incomplete declaration in front of it -- which stays visible rather than
    #: being absorbed into a model that cannot hold it.
    load_object: str = ""


@dataclass(frozen=True)
class ApplicationParameterGroup:
    """Parameters the parser treats together, at the scope that declares them.

    The members are named, and each is an ordinary parameter of the same scope:
    an application's own parameters for a group under ``cli``, one command's for
    a group under that command. ``mutually_exclusive`` with ``required`` false
    is at most one member; with ``required`` true, exactly one -- what argparse's
    ``add_mutually_exclusive_group(required=...)`` enforces (backlog 940).
    """

    name: str
    description: str = ""
    mutually_exclusive: bool = False
    required: bool = False
    members: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApplicationCommand:
    """One command an application exposes, and what it takes."""

    name: str
    description: str = ""
    parameters: tuple[ApplicationCommandParameter, ...] = ()
    mode_groups: tuple[ApplicationModeGroup, ...] = ()
    parameter_groups: tuple[ApplicationParameterGroup, ...] = ()
    #: How it is invoked: ``run`` or ``hosted`` (``INVOCATION_*``, #1166).
    invocation: str = INVOCATION_RUN
    #: For a hosted command, the canonical subject identity kinds it can open and
    #: host (#1218): the hosting application's own declaration of what it hosts,
    #: from which an opening reaches it. Empty for a ``run`` command.
    hosted_subject_kinds: tuple[str, ...] = ()

    def arguments(self, inputs: Mapping[str, Any]) -> list[str]:
        """This command's published command-line form of its semantic inputs.

        A published parameter's input is keyed by its name with ``-`` read as
        ``_`` (``workflow_id`` for ``workflow-id``). In published order: a
        positional parameter gives its value; ``True`` gives ``--name``; a false
        or empty value gives nothing; any other value gives ``--name value``.

        A ``json`` parameter (``JSON_VALUE_TYPE``, #1143) carries one structured
        value: given whenever its input is not None, and rendered as ONE
        argument by canonical JSON serialization -- never ``str()`` or a
        Python repr. Only that it is valid JSON is checked here; what it means
        is the receiving application's.

        Raises:
            ConfigError: When an input names no parameter this command
                publishes, a required parameter has no value, or a json
                parameter's value cannot be serialized as JSON.
        """
        by_input = {one.name.replace("-", "_"): one for one in self.parameters}
        unpublished = sorted(set(inputs) - set(by_input))
        if unpublished:
            raise ConfigError(
                f"command '{self.name}' publishes no parameter for "
                f"{', '.join(unpublished)}."
            )
        arguments: list[str] = []
        for key, parameter in by_input.items():
            value = inputs.get(key)
            if parameter.value_type == JSON_VALUE_TYPE:
                if value is None:
                    if parameter.required:
                        raise ConfigError(
                            f"command '{self.name}' requires '{parameter.name}'."
                        )
                    continue
                try:
                    rendered = json.dumps(
                        value, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False, allow_nan=False,
                    )
                except (TypeError, ValueError) as exc:
                    raise ConfigError(
                        f"command '{self.name}' parameter '{parameter.name}' takes "
                        f"JSON, and its value cannot be serialized: {exc}"
                    ) from exc
                arguments += ([rendered] if parameter.positional
                              else [f"--{parameter.name}", rendered])
                continue
            given = value is True or (bool(value) and str(value).lower() != "false")
            if not given:
                if parameter.required:
                    raise ConfigError(
                        f"command '{self.name}' requires '{parameter.name}'."
                    )
                continue
            if parameter.positional:
                arguments.append(str(value))
            elif value is True or str(value).lower() == "true":
                arguments.append(f"--{parameter.name}")
            else:
                arguments += [f"--{parameter.name}", str(value)]
        return arguments


@dataclass(frozen=True)
class ApplicationOperation:
    """One operation an application publishes for workflows, with its contract.

    ``contract_definition`` is a JSON Schema (2020-12) of the settings the
    operation reads; ``contract_sample`` is the starter settings a new settings
    instance copies. Both are the application's own published answer, checked
    when the registration is read (rey_lib.config.operation_contract), and the
    coordinator validates every step's settings against the definition before
    dispatch. Neither holds workflow values.
    """

    name: str
    description: str = ""
    contract_definition: Mapping[str, Any] = field(default_factory=dict, hash=False)
    contract_sample: Mapping[str, Any] = field(default_factory=dict, hash=False)


#: How a canonical-object capability is accessed (#1196 D1, #45 step 17).
CAPABILITY_ACCESS = frozenset({"read", "write"})

#: A canonical, behavior-surface capability key: dotted lower-case segments,
#: e.g. ``file.text``. The same shape code.application_capability checks.
_CAPABILITY_KEY = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


@dataclass(frozen=True)
class ApplicationCapability:
    """One canonical-object capability an application publishes (#1196 D1).

    Behavior of the canonical object the application hosts, by its canonical
    key (``file.text``), the identity kinds it applies to, an input contract
    (JSON Schema 2020-12, no sample) and whether it reads or writes. A
    declaration only: what implements it is the owner's own catalogue, and
    nothing here names, imports or resolves it.
    """

    key: str
    description: str
    target_kinds: tuple[str, ...]
    input_contract: Mapping[str, Any] = field(default_factory=dict, hash=False)
    access: str = "read"


@dataclass(frozen=True)
class Application:
    """One configured application.

    Identity and the settings execution needs -- ``app_path``, ``entry_point``
    and whether the shared parameters are passed -- beside the commands it
    exposes. One object, so a consumer that has an application does not go back
    to the registry for the half it is missing.
    """

    name: str
    label: str = ""
    app_type: str = "python"
    app_path: str = ""
    entry_point: str = "main.py"
    shared_parameters: bool = False
    #: How much this application records, when it overrides the installation.
    #:
    #: Installation-owned: how loud an application runs is an operational
    #: decision about this installation, not something the distribution
    #: publishes about itself.
    #:
    #: EMPTY MEANS INHERIT, and is left empty rather than defaulted here. The
    #: precedence chain is resolved once, at settlement, and it can only
    #: distinguish "this application asked for a level" from "this application
    #: said nothing" while absence is still absence. Normalizing it to a level
    #: here would silently make every application an override and leave the
    #: installation term with nothing to answer.
    log_level: str = ""
    #: The icon this application is known by, as a name in the shared library.
    #:
    #: Published by the distribution, beside its CLI and its operations, because
    #: which mark is an application's own is a fact about the application. The
    #: surface drawing it holds the artwork and nothing else; a NAME crosses,
    #: never markup -- an installed distribution is not a source the Console's
    #: raw-SVG path accepts.
    #:
    #: OPTIONAL HERE, REQUIRED BY POLICY OF THE ESTATE'S OWN PACKAGES. The two
    #: are different contracts on purpose: an external application has no
    #: package to publish one and a declaration predating this field is still
    #: valid, so both stay legal and are drawn with the generic application
    #: mark. That the estate's own registrations must each publish one is
    #: enforced by a test, not by this default -- reading the empty string as
    #: laxity is the misreading this comment exists to prevent.
    icon: str = ""
    #: The panel kind this application is drawn by, published by the
    #: distribution beside its icon. Empty means the general application panel.
    panel: str = ""
    #: The tree node type this application's node is, published by the
    #: distribution beside its icon and panel (backlog 1098): which governed
    #: node actions its node offers. Empty means the ordinary application node.
    node_type: str = ""
    #: The application's own parameters, declared under ``cli.parameters``.
    #:
    #: Not every application has commands. Several declare parameters at this
    #: level and no subcommand at all, and ``build_app_command`` invokes those
    #: with flags and no command word. So both levels are carried: an
    #: application that takes arguments without naming a command is an ordinary
    #: shape, not a declaration missing its commands.
    parameters: tuple[ApplicationCommandParameter, ...] = field(default=())
    #: Groups over :attr:`parameters`, declared under ``cli.parameter_groups``.
    parameter_groups: tuple[ApplicationParameterGroup, ...] = field(default=())
    commands: tuple[ApplicationCommand, ...] = field(default=())
    #: The operations this application allows a workflow to invoke.
    #:
    #: The published half of the workflow contract: a declared process names one
    #: of these, and its step settings are validated against the operation's
    #: contract_definition (JSON Schema) before anything is dispatched. Not the
    #: CLI parameter vocabulary: an operation's settings are hierarchical, so
    #: the contract is a schema rather than a parameter list (backlog 939).
    #:
    #: Empty until an application publishes them. Which operations an
    #: application approves is its own answer, and an empty tuple means it has
    #: not given one -- never that it approves everything.
    workflow_operations: tuple[ApplicationOperation, ...] = field(default=())
    #: The implementations this application approves for its workflow
    #: operations, operation name -> callable (backlog 963).
    #:
    #: The other half of binding: a workflow step binds to one of
    #: :attr:`workflow_operations` and to the implementation published here.
    #: Taken from the registration's ``approved_implementations`` as the
    #: finished mapping the application itself built; nothing here imports an
    #: application module. Empty until an application publishes it.
    approved_implementations: Mapping[str, Callable[..., Any]] = field(
        default_factory=dict, hash=False,
    )
    #: The canonical-object capabilities this application publishes (#1196 D1).
    #:
    #: Beside, not inside, :attr:`workflow_operations`: behavior of the object
    #: the application hosts, not a workflow step. Empty until published, and
    #: empty never means everything.
    capabilities: tuple[ApplicationCapability, ...] = field(default=())

    #: The presentation an application's settings require (#1150): what the
    #: Console's registry presents it with. Not :attr:`panel`, the retired
    #: application -> panel mapping, which only the Loader openings still read
    #: until #1151.
    PANEL_TYPE: ClassVar[str] = "application"

    def command(self, name: str) -> ApplicationCommand | None:
        """The command of that name, or None where this application has none."""
        return next((one for one in self.commands if one.name == name), None)

    def projection(self) -> dict[str, Any]:
        """This application's settings, as they are presented (#1150).

        Its identity and the commands a run can start -- each with its exclusive
        choices and the parameters it takes -- read from what construction
        already settled; nothing is re-read or normalized here. A hosted
        command is not offered: it needs the launcher's channel (#1166). Which
        marks stand for an alternative, which parameters belong to which modes,
        and which of a load's objects a parameter configures are carried as
        declared, never interpreted.
        """
        return {
            "panel_type": self.PANEL_TYPE,
            "application_id": self.name,
            "label": self.label,
            "commands": [
                {
                    "name": command.name,
                    "description": command.description,
                    "mode_groups": [
                        {
                            "name": group.name,
                            "label": group.label,
                            "default": group.default,
                            "modes": [
                                {"name": mode.name, "label": mode.label, "icons": list(mode.icons)}
                                for mode in group.modes
                            ],
                        }
                        for group in command.mode_groups
                    ],
                    "parameters": [
                        {
                            "name": parameter.name,
                            "value_type": parameter.value_type,
                            "required": parameter.required,
                            "description": parameter.description,
                            "possible_values": list(parameter.possible_values),
                            "mode_membership": {
                                group: list(modes) for group, modes in parameter.mode_membership
                            },
                            "required_when": {
                                group: list(modes) for group, modes in parameter.required_when
                            },
                            "placeholder": parameter.placeholder,
                            "placement": parameter.placement,
                            "load_object": parameter.load_object,
                        }
                        for parameter in command.parameters
                    ],
                }
                for command in self.commands if command.invocation == INVOCATION_RUN
            ],
        }

    def command_line(
        self, ctx: Any, operation: str, inputs: Mapping[str, Any],
    ) -> list[str]:
        """The executable invocation of one published operation, from this configuration.

        The configured Python and this application's entry point; the
        installation's configuration when it takes the shared parameters; then
        the operation and its arguments, converted from ``inputs`` by the
        operation itself (:meth:`ApplicationCommand.arguments`).

        Raises:
            ConfigError: When this application publishes no such operation, the
                runtime names no Python executable, or a shared-parameter
                application has no configuration path.
        """
        command = self.command(operation)
        if command is None:
            raise ConfigError(
                f"application '{self.name}' publishes no operation '{operation}'."
            )
        runtime = getattr(ctx, "runtime", None)
        python = getattr(getattr(runtime, "python", None), "executable", None)
        if not python:
            raise ConfigError("ctx.runtime.python.executable is required.")
        line = [str(Path(str(python)).expanduser()),
                str(Path(self.app_path).expanduser() / self.entry_point)]
        if self.shared_parameters:
            config_path = getattr(ctx, "config_path", None)
            if not config_path:
                raise ConfigError("config_path is required.")
            line += ["--config-path", str(Path(str(config_path)).expanduser())]
        return line + [command.name, *command.arguments(inputs)]

    @property
    def command_names(self) -> tuple[str, ...]:
        """Every command name this application offers.

        The names of :attr:`commands`, and nothing else. Construction has
        already normalized the two declaration styles into one, so this is a
        reading of the commands rather than a second rule about where they come
        from.
        """
        return tuple(one.name for one in self.commands)


def hosted_command(ctx: Any, kind: str) -> tuple[Application, ApplicationCommand]:
    """The configured application and hosted command that host a subject ``kind`` (#1218).

    Read off ``ctx.applications``: the one hosted command whose
    ``hosted_subject_kinds`` names the kind, as its application published it.

    Raises:
        ConfigError: When no configured command hosts the kind, or more than one
            claims it.
    """
    found = [
        (application, command)
        for application in getattr(ctx, "applications", None) or ()
        for command in application.commands
        if kind and kind in command.hosted_subject_kinds
    ]
    if not found:
        raise ConfigError(f"No configured application hosts the subject kind '{kind}'.")
    if len(found) > 1:
        claims = ", ".join(f"{one.name} {command.name}" for one, command in found)
        raise ConfigError(f"The subject kind '{kind}' is hosted by several commands: {claims}.")
    return found[0]


def configured_application(ctx: Any, name: str) -> Application:
    """The application this runtime configured as ``name`` (#1150).

    Read off ``ctx.applications``, which the config load built once; nothing is
    re-read from configuration here.

    Raises:
        ConfigError: When this runtime configures no application by that name.
    """
    found = next(
        (one for one in getattr(ctx, "applications", None) or () if one.name == name), None,
    )
    if found is None:
        raise ConfigError(f"No application is configured as '{name}'.")
    return found


def build_applications(
    ctx: Any,
    registrations: dict[str, dict[str, Any]] | None = None,
) -> tuple[Application, ...]:
    """Return this installation's applications, resolved once.

    Built after the collections a parameter's choices may name, because a
    declaration reading one of them is resolved here rather than later.

    Args:
        ctx: The loaded context, carrying the ``apps`` declaration and the
            collections a choice may be resolved from.
        registrations: What the installed applications published. None
            discovers them from the environment.

    Returns:
        One :class:`Application` per enabled declaration, in declared order.

    Raises:
        ConfigError: If ``apps`` is not a canonical list, or a parameter names a
            choice source that does not exist.
    """
    declared = getattr(ctx, "apps", None)
    if declared is None:
        return ()
    if not isinstance(declared, (list, tuple)):
        raise ConfigError(
            "Config section 'apps' must be a canonical list; "
            f"found {type(declared).__name__}."
        )
    # Discovery answers before any Application is built, so a duplicate claim
    # stops the run rather than producing objects that would have to be undone.
    #
    # Supplied by the caller where the caller already has them -- bootstrap
    # discovers once for the process. Not a second source: the same
    # registrations either way, handed over rather than found again.
    if registrations is None:
        registrations = discover_registrations()
    return tuple(
        _application(entry, ctx, registrations)
        for entry in (_plain(item) for item in declared)
        if isinstance(entry, dict) and entry.get("enabled", True)
    )


def _application(
    entry: dict[str, Any],
    ctx: Any,
    registrations: dict[str, dict[str, Any]],
) -> Application:
    """One declaration, as the object.

    Two construction paths reach the same object. A Python application takes its
    identity and published capability from its registration and everything else
    from the declaration; an external one, which cannot publish a Python entry
    point, is declared whole.

    Raises:
        ConfigError: If a Python application has no registration, if a
            declaration claims ``python`` where the registry path is the only
            one available to it, or if a Python declaration supplies no
            ``app_path``.
    """
    name = str(entry.get("name") or "")
    if not name:
        raise ConfigError("An application in 'apps' is declared without a name.")

    registration = registrations.get(name)
    declared_type = str(entry.get("type") or entry.get("app_type") or "python")

    if registration is None:
        if declared_type == "python":
            # A Python application exists only through its package. Declaring
            # one the environment does not have would let configuration invent
            # an application, and the capability half would have nowhere to come
            # from.
            raise ConfigError(
                f"Application '{name}' is declared as python but no installed "
                f"distribution registers it under '{REGISTRATION_GROUP}'. "
                "Install it, or declare it as an external application type."
            )
        # External: PowerShell, shell, a command. Declared whole, because there
        # is no package to publish anything.
        return _from_declaration(entry, ctx, name, declared_type)

    if declared_type != "python":
        raise ConfigError(
            f"Application '{name}' registers as a Python application but is "
            f"declared as '{declared_type}'."
        )
    return _from_registration(entry, ctx, name, registration)


def _from_registration(
    entry: dict[str, Any],
    ctx: Any,
    name: str,
    registration: dict[str, Any],
) -> Application:
    """A Python application: published capability, declared configuration.

    The published half is taken from the registration and nothing else. What the
    declaration says about commands is not merged, not a fallback and cannot
    override -- an entry left behind in the external registry is legacy data,
    and reading it would keep the authority it lost.
    """
    registered_name = str(registration.get("name") or "")
    if registered_name != name:
        raise ConfigError(
            f"Application '{name}' is registered under the name "
            f"'{registered_name}'. A registration and a declaration cannot "
            "disagree about which application they describe."
        )

    app_path = str(entry.get("app_path") or "")
    if not app_path:
        # Never recovered from the package. Where importable code lives is not
        # the same question as where the installation runs the application from,
        # and one packaging mode answering it would make the other wrong.
        raise ConfigError(
            f"Application '{name}' declares no 'app_path'. It is installation "
            "configuration and is never reconstructed from package metadata."
        )

    cli = registration.get("cli")
    cli = cli if isinstance(cli, dict) else {}
    parameters = tuple(
        _parameter(one, ctx, name)
        for one in (_plain(item) for item in (cli.get("parameters") or []))
        if isinstance(one, dict)
    )
    return Application(
        name=name,
        # THREE SOURCES, IN THAT ORDER. An installation naming an application
        # has said what it calls it there and keeps saying it. Otherwise the
        # distribution's own published name is used -- what an application is
        # called is its fact, the way its icon is. The identifier stands in only
        # when neither said anything, and then it reads like an identifier.
        label=str(entry.get("label") or registration.get("label") or name),
        app_type="python",
        app_path=app_path,
        entry_point=str(registration.get("entry_point") or "main.py"),
        shared_parameters=bool(cli.get("shared_parameters")),
        # From the REGISTRATION: which mark is this application's own is
        # published by the distribution, not chosen by an installation. Two
        # installations running the same application draw the same icon.
        icon=str(registration.get("icon") or ""),
        # From the REGISTRATION, as the icon is: which panel draws it is the
        # distribution's fact about itself.
        panel=str(registration.get("panel") or ""),
        # From the REGISTRATION, as the panel is: which node type its tree node
        # is -- and so which governed actions that node offers -- is the
        # distribution's fact about itself (backlog 1098).
        node_type=str(registration.get("node_type") or ""),
        # From the installation entry, not the registration: how loud this
        # application runs here is this installation's decision.
        log_level=str(entry.get("log_level") or ""),
        parameters=parameters,
        parameter_groups=_parameter_groups(cli, parameters, name),
        commands=_commands(cli, ctx, name),
        workflow_operations=_operations(registration, name),
        # From the REGISTRATION, as the operations are: the finished mapping
        # the application built from its own implementations.
        approved_implementations=dict(registration.get("approved_implementations") or {}),
        # From the REGISTRATION, beside the operations: the canonical-object
        # capabilities it publishes (#1196 D1).
        capabilities=_capabilities(registration, name),
    )


def _operations(
    registration: dict[str, Any],
    application: str,
) -> tuple[ApplicationOperation, ...]:
    """The operations an application approves for workflow invocation.

    Absent means none approved, which is the safe reading: an application that
    has published nothing has not approved everything.

    Each operation's contract is checked here, once, when the registration is
    read: a definition that is not JSON Schema 2020-12, or a sample that does
    not satisfy it, stops the load rather than surfacing at a workflow's first
    step.

    Raises:
        ConfigError: An operation with no name, one still publishing the
            retired ``parameters`` list, or one whose contract is refused.
    """
    operations: list[ApplicationOperation] = []
    for one in (_plain(item) for item in (registration.get("workflow_operations") or [])):
        if not isinstance(one, dict):
            continue
        name = str(one.get("name") or "")
        if not name:
            raise ConfigError(
                f"Application '{application}' publishes a workflow operation with no name."
            )
        label = f"{application}.{name}"
        if "parameters" in one:
            # Refused rather than ignored: a parameter list left beside a
            # contract would read as a second statement of the same thing.
            raise ConfigError(
                f"Operation '{label}' publishes a parameters list. A workflow "
                "operation publishes contract_definition and contract_sample."
            )
        definition = one.get("contract_definition")
        sample = one.get("contract_sample")
        check_contract(definition, sample, label)
        operations.append(ApplicationOperation(
            name=name,
            description=str(one.get("description") or ""),
            contract_definition=definition,
            contract_sample=sample,
        ))
    return tuple(operations)


def _capabilities(
    registration: dict[str, Any],
    application: str,
) -> tuple[ApplicationCapability, ...]:
    """The canonical-object capabilities an application publishes (#1196 D1).

    Absent means none. Each declaration is checked here, once, when the
    registration is read, so that only a well-formed declaration can become
    governed or be invoked.

    Raises:
        ConfigError: A key that is not a dotted canonical name or is published
            twice, no target kinds, an empty description, an input contract
            that is not JSON Schema 2020-12, or access other than read | write.
    """
    capabilities: list[ApplicationCapability] = []
    for one in (_plain(item) for item in (registration.get("capabilities") or [])):
        if not isinstance(one, dict):
            continue
        key = str(one.get("key") or "")
        if not _CAPABILITY_KEY.match(key):
            raise ConfigError(
                f"Application '{application}' publishes capability {key!r}; a "
                "capability key is a dotted canonical name such as 'file.text'."
            )
        label = f"{application}.{key}"
        if any(capability.key == key for capability in capabilities):
            raise ConfigError(f"Capability '{label}' is published twice.")
        kinds = one.get("target_kinds")
        target_kinds = tuple(str(kind) for kind in (kinds if isinstance(kinds, list) else []))
        if not target_kinds or not all(target_kinds):
            raise ConfigError(f"Capability '{label}' publishes no target_kinds.")
        description = str(one.get("description") or "")
        if not description.strip():
            raise ConfigError(f"Capability '{label}' publishes no description.")
        input_contract = one.get("input_contract")
        check_definition(input_contract, label)
        access = str(one.get("access") or "")
        if access not in CAPABILITY_ACCESS:
            raise ConfigError(
                f"Capability '{label}' declares access {access!r}; access is read or write."
            )
        capabilities.append(ApplicationCapability(
            key=key,
            description=description,
            target_kinds=target_kinds,
            input_contract=input_contract,
            access=access,
        ))
    return tuple(capabilities)


def _from_declaration(
    entry: dict[str, Any],
    ctx: Any,
    name: str,
    declared_type: str,
) -> Application:
    """An external application, declared whole in the registry."""
    cli = entry.get("cli")
    cli = cli if isinstance(cli, dict) else {}
    return Application(
        name=name,
        label=str(entry.get("label") or name),
        app_type=declared_type,
        app_path=str(entry.get("app_path") or ""),
        entry_point=str(entry.get("entry_point") or "main.py"),
        shared_parameters=bool(cli.get("shared_parameters")),
        # From the declaration here, because an external application has no
        # package to publish anything. Absent is ordinary and draws the
        # generic mark.
        icon=str(entry.get("icon") or ""),
        log_level=str(entry.get("log_level") or ""),
        parameters=tuple(
            _parameter(one, ctx, name)
            for one in (_plain(item) for item in (cli.get("parameters") or []))
            if isinstance(one, dict)
        ),
        commands=_commands(cli, ctx, name),
    )


#: The parameter whose values are an application's commands, where it declares
#: them that way. Positional, and named for what it carries.
_COMMAND_PARAMETER = "command"


def _commands(
    cli: dict[str, Any],
    ctx: Any,
    application: str,
) -> tuple[ApplicationCommand, ...]:
    """Every command this application offers, however it declared them.

    Two styles are in use, and a consumer should not have to know which:

    - ``cli.commands`` lists them, each with its own parameters;
    - a **positional parameter named ``command``** carries them as its values,
      and the application's own ``cli.parameters`` are what those invocations
      take. Several applications declare only this.

    Both become :class:`ApplicationCommand` objects here, so
    ``Application.commands`` is every executable command and each one carries
    exactly the parameters that invocation takes. Nothing downstream reads the
    positional parameter, and nothing falls back to the application's own
    parameters to work out what a selected command accepts.

    An application declaring **neither** offers no commands. That is not a gap:
    it is invoked with no command word, and its own parameters are what that
    invocation takes.
    """
    listed = tuple(
        _command(one, ctx, application)
        for one in (_plain(item) for item in (cli.get("commands") or []))
        if isinstance(one, dict)
    )
    declared = tuple(
        _parameter(one, ctx, application)
        for one in (_plain(item) for item in (cli.get("parameters") or []))
        if isinstance(one, dict)
    )
    carrier = next(
        (
            one for one in declared
            if one.positional and one.name == _COMMAND_PARAMETER
        ),
        None,
    )
    if listed and carrier is not None:
        # Refused rather than resolved by precedence. Which declaration wins is
        # not something to decide on an application's behalf, and no
        # application declares both today -- so the first one that does says so
        # at load rather than quietly losing half its commands.
        raise ConfigError(
            f"Application '{application}' declares commands both under "
            "'cli.commands' and as the values of its positional 'command' "
            "parameter. One or the other."
        )
    if listed:
        return listed
    if carrier is None:
        return ()
    # The command word is chosen, not filled in: it is what the others belong
    # to, so it is not one of them.
    takes = tuple(one for one in declared if one is not carrier)
    # Each value may state how its command is invoked (#1166), as a listed
    # command does: the same ApplicationCommand whichever syntax declared it.
    invoked = _value_invocations(cli, application)
    return tuple(
        ApplicationCommand(name=value, parameters=takes,
                           invocation=invoked.get(value, (INVOCATION_RUN, ()))[0],
                           hosted_subject_kinds=invoked.get(value, (INVOCATION_RUN, ()))[1])
        for value in carrier.possible_values
    )


def _value_invocations(
    cli: dict[str, Any], application: str,
) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Each stated command value's invocation and hosted subject kinds (#1166, #1218)."""
    for one in (_plain(item) for item in (cli.get("parameters") or [])):
        if isinstance(one, dict) and _is_command_carrier(one):
            return {
                name: (invocation, kinds)
                for name, invocation, kinds in (
                    _possible_value(value, application, one)
                    for value in (one.get("possible_values") or [])
                )
            }
    return {}


def _hosted_subject_kinds(
    value: Any, invocation: str, application: str, where: str,
) -> tuple[str, ...]:
    """A command's declared hosted subject kinds, empty when absent (#1218).

    Raises:
        ConfigError: When they are not a list of non-empty names, or are declared
            on a command that is not hosted.
    """
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(
            isinstance(one, str) and one for one in value):
        raise ConfigError(
            f"Application '{application}' {where} declares hosted_subject_kinds that "
            "are not a list of subject kind names."
        )
    if invocation != INVOCATION_HOSTED:
        raise ConfigError(
            f"Application '{application}' {where} declares hosted_subject_kinds but is "
            f"invoked '{invocation}'; only a hosted command hosts subjects."
        )
    return tuple(value)


def _is_command_carrier(entry: dict[str, Any]) -> bool:
    """Whether a parameter declaration is the positional one carrying the commands."""
    return bool(entry.get("positional")) and entry.get("name") == _COMMAND_PARAMETER


def _invocation(value: Any, application: str, where: str) -> str:
    """A declared invocation, ``run`` when absent.

    Raises:
        ConfigError: When it is anything but ``run`` or ``hosted``.
    """
    if value is None:
        return INVOCATION_RUN
    if value not in _INVOCATIONS:
        raise ConfigError(
            f"Application '{application}' {where} declares invocation {value!r}; "
            f"one of {sorted(_INVOCATIONS)}."
        )
    return str(value)


def _possible_value(
    value: Any, application: str, entry: dict[str, Any],
) -> tuple[str, str, tuple[str, ...]]:
    """One stated possible value, as its name, invocation and hosted subject kinds.

    A value is stated as itself (its name, invoked ``run``) or as an object
    ``{name, invocation?, hosted_subject_kinds?}`` (#1166, #1218). Only the
    command carrier's values may state an invocation or hosted subject kinds --
    on any other parameter they would mean nothing -- and an object with any
    other shape is refused rather than interpreted.

    Raises:
        ConfigError: When an object value has no name, an unknown key, an
            invocation outside ``run`` / ``hosted``, an invocation or hosted
            subject kinds on a parameter that does not carry commands, or hosted
            subject kinds on a command that is not hosted.
    """
    parameter = entry.get("name")
    if not isinstance(value, dict):
        if isinstance(value, (list, tuple, set)):
            raise ConfigError(
                f"Application '{application}' parameter '{parameter}' states a "
                "possible value that is neither a value nor a {name, invocation} object."
            )
        return str(value), INVOCATION_RUN, ()
    unknown = sorted(set(value) - {"name", "invocation", "hosted_subject_kinds"})
    name = value.get("name")
    if unknown or not isinstance(name, str) or not name:
        raise ConfigError(
            f"Application '{application}' parameter '{parameter}' states a possible "
            "value object that is not {name, invocation?, hosted_subject_kinds?}"
            + (f" (unknown {unknown})" if unknown else " (it needs a name)") + "."
        )
    if ({"invocation", "hosted_subject_kinds"} & set(value)) and not _is_command_carrier(entry):
        raise ConfigError(
            f"Application '{application}' parameter '{parameter}' states an "
            "invocation or hosted subject kinds on a value that is not a command."
        )
    where = f"command value '{name}'"
    invocation = _invocation(value.get("invocation"), application, where)
    return name, invocation, _hosted_subject_kinds(
        value.get("hosted_subject_kinds"), invocation, application, where)


def _command(entry: dict[str, Any], ctx: Any, application: str) -> ApplicationCommand:
    """One command declaration, as the object.

    The mode groups are built first, because every parameter's membership is
    checked against them: a membership naming a group nobody declared is a typo
    that would otherwise hide a field forever.
    """
    name = str(entry.get("name") or "")
    invocation = _invocation(entry.get("invocation"), application, f"command '{name}'")
    groups = _mode_groups(entry, application, name)
    parameters = tuple(
        _parameter(one, ctx, application, name, groups)
        for one in (_plain(item) for item in (entry.get("parameters") or []))
        if isinstance(one, dict)
    )
    return ApplicationCommand(
        name=name,
        description=str(entry.get("description") or ""),
        parameters=parameters,
        mode_groups=groups,
        parameter_groups=_parameter_groups(entry, parameters, application, name),
        invocation=invocation,
        hosted_subject_kinds=_hosted_subject_kinds(
            entry.get("hosted_subject_kinds"), invocation, application, f"command '{name}'"),
    )


def _parameter_groups(
    entry: dict[str, Any],
    parameters: tuple[ApplicationCommandParameter, ...],
    application: str,
    command: str = "",
) -> tuple[ApplicationParameterGroup, ...]:
    """One scope's parameter groups, each checked against that scope's parameters.

    Raises:
        ConfigError: A group with no name, a duplicate group name, a group
            still nesting its members' declarations, no members, a member that
            is not a parameter of the same scope, or a parameter in two groups.
            Each would publish a group the parser does not have.
    """
    where = f"Application '{application}'" + (f" command '{command}'" if command else "")
    declared = {one.name for one in parameters}
    built: list[ApplicationParameterGroup] = []
    grouped: set[str] = set()
    for item in (_plain(one) for one in (entry.get("parameter_groups") or [])):
        if not isinstance(item, dict):
            continue
        group = str(item.get("name") or "")
        if not group:
            raise ConfigError(f"{where} declares a parameter group with no name.")
        if any(one.name == group for one in built):
            raise ConfigError(f"{where} declares parameter group '{group}' more than once.")
        if "parameters" in item:
            # Refused rather than read: members declared inside a group are
            # invisible to every reader of the scope's parameters.
            raise ConfigError(
                f"{where} parameter group '{group}' nests parameter declarations. "
                "Declare them as parameters of the same scope and name them under "
                "'members'."
            )
        members = tuple(str(one) for one in (item.get("members") or ()))
        if not members:
            raise ConfigError(f"{where} parameter group '{group}' names no members.")
        unknown = [one for one in members if one not in declared]
        if unknown:
            raise ConfigError(
                f"{where} parameter group '{group}' names {', '.join(unknown)}, "
                "which this scope does not declare as parameters."
            )
        twice = [one for one in members if one in grouped]
        if twice:
            raise ConfigError(
                f"{where} parameter group '{group}' names {', '.join(twice)}, "
                "already a member of another group."
            )
        grouped.update(members)
        built.append(ApplicationParameterGroup(
            name=group,
            description=str(item.get("description") or ""),
            mutually_exclusive=bool(item.get("mutually_exclusive")),
            required=bool(item.get("required")),
            members=members,
        ))
    return tuple(built)


def _mode_groups(
    entry: dict[str, Any],
    application: str,
    command: str,
) -> tuple[ApplicationModeGroup, ...]:
    """One command's exclusive choices, refused rather than repaired.

    Raises:
        ConfigError: A group with no name or no modes, a duplicate group name,
            a duplicate mode within one group, or a default naming a mode the
            group does not declare. Each would present a reader with a chooser
            that cannot work, and none is recoverable by guessing.
    """
    built: list[ApplicationModeGroup] = []
    for item in (_plain(one) for one in (entry.get("mode_groups") or [])):
        if not isinstance(item, dict):
            continue
        group = str(item.get("name") or "")
        where = f"Application '{application}' command '{command}'"
        if not group:
            raise ConfigError(f"{where} declares a mode group with no name.")
        if any(one.name == group for one in built):
            raise ConfigError(
                f"{where} declares mode group '{group}' more than once. A group "
                "name identifies one choice, or a membership naming it cannot "
                "say which."
            )
        modes = tuple(
            ApplicationMode(
                name=str(mode.get("name") or ""),
                label=str(mode.get("label") or ""),
                # Order is the meaning, so the sequence is carried as declared
                # and nothing here sorts, de-duplicates or validates a name. What
                # a mark is called is the drawing surface's vocabulary, and a
                # name it does not carry is its own fallback to make.
                icons=tuple(
                    str(one) for one in (mode.get("icons") or ()) if str(one)
                ),
            )
            for mode in (_plain(one) for one in (item.get("modes") or []))
            if isinstance(mode, dict) and mode.get("name")
        )
        if not modes:
            raise ConfigError(
                f"{where} mode group '{group}' declares no modes. A chooser "
                "with nothing to choose is not a choice."
            )
        named = [one.name for one in modes]
        if len(set(named)) != len(named):
            raise ConfigError(
                f"{where} mode group '{group}' declares a mode more than once: "
                f"{', '.join(sorted(named))}."
            )
        default = str(item.get("default") or "")
        if default and default not in named:
            raise ConfigError(
                f"{where} mode group '{group}' defaults to '{default}', which "
                f"it does not declare. Its modes are: {', '.join(named)}."
            )
        built.append(ApplicationModeGroup(
            name=group,
            label=str(item.get("label") or ""),
            default=default or named[0],
            modes=modes,
        ))
    return tuple(built)


def _parameter(
    entry: dict[str, Any],
    ctx: Any,
    application: str,
    command: str = "",
    groups: tuple[ApplicationModeGroup, ...] = (),
) -> ApplicationCommandParameter:
    """One parameter declaration, with its choices and memberships resolved."""
    name = str(entry.get("name") or "")
    return ApplicationCommandParameter(
        name=name,
        value_type=str(entry.get("value_type") or ""),
        required=bool(entry.get("required")),
        description=str(entry.get("description") or ""),
        positional=bool(entry.get("positional")),
        possible_values=_choices(entry, ctx, application),
        mode_membership=_membership(
            entry, "mode_membership", application, command, name, groups,
        ),
        required_when=_membership(
            entry, "required_when", application, command, name, groups,
        ),
        placeholder=str(entry.get("placeholder") or ""),
        placement=str(entry.get("placement") or "form"),
        # No default of its own: absent means the declaration says nothing
        # about which object, which is what every parameter declared before
        # this existed says.
        load_object=str(entry.get("load_object") or ""),
    )


def _membership(
    entry: dict[str, Any],
    key: str,
    application: str,
    command: str,
    parameter: str,
    groups: tuple[ApplicationModeGroup, ...],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """One ``{group: [mode, ...]}`` declaration, checked against the groups.

    Fails closed on a name nothing declares, because the consequence of a typo
    is a field that is never active and never explains why.

    Raises:
        ConfigError: Naming the group or mode that does not exist, and what the
            command actually declares.
    """
    declared = _plain(entry.get(key)) or {}
    if not isinstance(declared, dict):
        raise ConfigError(
            f"Application '{application}' command '{command}' parameter "
            f"'{parameter}' declares '{key}' as {type(declared).__name__}. It "
            "maps a mode group name to the modes within it."
        )
    known = {one.name: {mode.name for mode in one.modes} for one in groups}
    built: list[tuple[str, tuple[str, ...]]] = []
    for group, modes in declared.items():
        where = (
            f"Application '{application}' command '{command}' parameter "
            f"'{parameter}' {key}"
        )
        if str(group) not in known:
            raise ConfigError(
                f"{where} names mode group '{group}', which this command does "
                f"not declare. Declared: {', '.join(sorted(known)) or 'none'}."
            )
        named = tuple(str(one) for one in (modes or ()))
        unknown = [one for one in named if one not in known[str(group)]]
        if unknown:
            raise ConfigError(
                f"{where} names mode(s) {', '.join(unknown)} in group "
                f"'{group}', which declares: "
                f"{', '.join(sorted(known[str(group)]))}."
            )
        built.append((str(group), named))
    return tuple(built)


def _choices(entry: dict[str, Any], ctx: Any, application: str) -> tuple[str, ...]:
    """What this parameter's choices are, resolved now.

    A declaration either states them or names where they come from. Naming a
    source is what replaced an application checked by name in shared code: the
    configuration says which collection its choices are, and this resolves that
    for every application without knowing any of them.
    """
    named = str(entry.get("possible_values_from") or "").strip()
    if not named:
        # A stated value is itself or a {name, invocation?} object (#1166); the
        # choices are the names either way.
        return tuple(
            _possible_value(value, application, entry)[0]
            for value in (entry.get("possible_values") or [])
        )
    registry = _REGISTRY_CHOICES.get(named)
    if registry is not None:
        return tuple(sorted(registry(ctx)))
    collection = _CHOICE_SOURCES.get(named)
    if collection is None:
        raise ConfigError(
            f"Application '{application}' parameter '{entry.get('name')}' names "
            f"choice source '{named}', which is not one of: "
            f"{', '.join(sorted({*_CHOICE_SOURCES, *_REGISTRY_CHOICES}))}."
        )
    members = getattr(ctx, collection, None)
    if members is None:
        raise ConfigError(
            f"Application '{application}' parameter '{entry.get('name')}' resolves "
            f"its choices from '{named}', which is not on the context. "
            "Applications are built after the collections they read."
        )
    selected = _CHOICE_FILTERS.get(named)
    return _member_names(selected(members) if selected else members)


def _member_names(members: Any) -> tuple[str, ...]:
    """A collection's members, as the values a choice offers.

    The member's own name, sorted -- the projection the Console's enrichment
    already used, kept so the generic resolver answers what the one it replaces
    answered.
    """
    named = [
        str(member.get("name") or "")
        for member in (_plain(item) for item in (members or []))
        if isinstance(member, dict) and member.get("name")
    ]
    return tuple(sorted(named))


def _plain(value: Any) -> Any:
    """One configuration item as plain data, without resolving anything."""
    from rey_lib.config.inventory import to_plain_data

    return to_plain_data(value)
