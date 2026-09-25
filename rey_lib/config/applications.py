"""Applications, as the runtime objects the estate's other collections already are.

``ctx.workflows`` and ``ctx.pipelines`` are canonical collections: configuration
declares them, the load resolves them once, and every consumer projects what it
needs from the result. Applications were the exception -- each reader re-entered
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

from dataclasses import dataclass, field
from typing import Any

from rey_lib.config.registration import REGISTRATION_GROUP, discover_registrations
from rey_lib.errors.error_utils import ConfigError

__all__ = [
    "Application",
    "ApplicationCommand",
    "ApplicationCommandParameter",
    "ApplicationMode",
    "ApplicationModeGroup",
    "build_applications",
]

#: Where a parameter's choices may be resolved from, and how each collection
#: becomes option values.
#:
#: Closed on purpose. This is not an attribute lookup on ctx: a source nobody
#: implemented is a configuration fault worth seeing at load, not an empty
#: dropdown discovered later. Each entry earns its place when a declaration
#: actually needs it, and each says how a member becomes a value -- the answer
#: the Console's enrichment already gave for workflows, kept rather than
#: reinvented.
_CHOICE_SOURCES: dict[str, str] = {
    "workflows": "workflows",
    "pipelines": "pipelines",
    "tools": "tools",
    "connections": "connections",
    "data_sources": "data_sources",
}


@dataclass(frozen=True)
class ApplicationMode:
    """One alternative within a mode group."""

    name: str
    label: str = ""


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
    #: Which END of a movement this parameter belongs to: ``source``,
    #: ``destination``, or nothing.
    #:
    #: THE ONE THING THE OTHER FIELDS CANNOT SAY. ``mode_membership`` says
    #: which SHAPE a parameter belongs to and ``placement`` says WHERE it is
    #: drawn; neither says which side of a load it describes. Once a source may
    #: be a file or a connection-and-statement, a flat list of names cannot --
    #: ``connection`` is ambiguous on its face.
    #:
    #: EMPTY IS A REAL ANSWER, not an omission to be guessed at. A parameter
    #: that is not one end of anything says nothing, and a surface that finds
    #: some of a command's parameters declaring an end and some not has an
    #: incomplete declaration in front of it -- which stays visible rather than
    #: being absorbed into a model that cannot hold it.
    endpoint: str = ""


@dataclass(frozen=True)
class ApplicationCommand:
    """One command an application exposes, and what it takes."""

    name: str
    description: str = ""
    parameters: tuple[ApplicationCommandParameter, ...] = ()
    mode_groups: tuple[ApplicationModeGroup, ...] = ()


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
    #: The application's own parameters, declared under ``cli.parameters``.
    #:
    #: Not every application has commands. Several declare parameters at this
    #: level and no subcommand at all, and ``build_app_command`` invokes those
    #: with flags and no command word. So both levels are carried: an
    #: application that takes arguments without naming a command is an ordinary
    #: shape, not a declaration missing its commands.
    parameters: tuple[ApplicationCommandParameter, ...] = field(default=())
    commands: tuple[ApplicationCommand, ...] = field(default=())
    #: The operations this application allows a workflow to invoke.
    #:
    #: The published half of the workflow contract: a declared process names one
    #: of these, and its step configuration is validated against the parameters
    #: named here before anything is dispatched. Reuses the command parameter
    #: vocabulary rather than a second one, because a published parameter is a
    #: published parameter whichever execution model reads it.
    #:
    #: Empty until an application publishes them. Which operations an
    #: application approves is its own answer, and an empty tuple means it has
    #: not given one -- never that it approves everything.
    workflow_operations: tuple[ApplicationCommand, ...] = field(default=())

    def command(self, name: str) -> ApplicationCommand | None:
        """The command of that name, or None where this application has none."""
        return next((one for one in self.commands if one.name == name), None)

    @property
    def command_names(self) -> tuple[str, ...]:
        """Every command name this application offers.

        The names of :attr:`commands`, and nothing else. Construction has
        already normalized the two declaration styles into one, so this is a
        reading of the commands rather than a second rule about where they come
        from.
        """
        return tuple(one.name for one in self.commands)


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
            "Config section 'apps' must be a canonical list, as 'workflows' and "
            f"'pipelines' are; found {type(declared).__name__}."
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
    return Application(
        name=name,
        label=str(entry.get("label") or name),
        app_type="python",
        app_path=app_path,
        entry_point=str(registration.get("entry_point") or "main.py"),
        shared_parameters=bool(cli.get("shared_parameters")),
        # From the REGISTRATION: which mark is this application's own is
        # published by the distribution, not chosen by an installation. Two
        # installations running the same application draw the same icon.
        icon=str(registration.get("icon") or ""),
        # From the installation entry, not the registration: how loud this
        # application runs here is this installation's decision.
        log_level=str(entry.get("log_level") or ""),
        parameters=tuple(
            _parameter(one, ctx, name)
            for one in (_plain(item) for item in (cli.get("parameters") or []))
            if isinstance(one, dict)
        ),
        commands=_commands(cli, ctx, name),
        workflow_operations=_operations(registration, ctx, name),
    )


def _operations(
    registration: dict[str, Any],
    ctx: Any,
    application: str,
) -> tuple[ApplicationCommand, ...]:
    """The operations an application approves for workflow invocation.

    Absent means none approved, which is the safe reading: an application that
    has published nothing has not approved everything.
    """
    return tuple(
        _command(one, ctx, application)
        for one in (_plain(item) for item in (registration.get("workflow_operations") or []))
        if isinstance(one, dict)
    )


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
    return tuple(
        ApplicationCommand(name=value, parameters=takes)
        for value in carrier.possible_values
    )


def _command(entry: dict[str, Any], ctx: Any, application: str) -> ApplicationCommand:
    """One command declaration, as the object.

    The mode groups are built first, because every parameter's membership is
    checked against them: a membership naming a group nobody declared is a typo
    that would otherwise hide a field forever.
    """
    name = str(entry.get("name") or "")
    groups = _mode_groups(entry, application, name)
    return ApplicationCommand(
        name=name,
        description=str(entry.get("description") or ""),
        parameters=tuple(
            _parameter(one, ctx, application, name, groups)
            for one in (_plain(item) for item in (entry.get("parameters") or []))
            if isinstance(one, dict)
        ),
        mode_groups=groups,
    )


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
        # about ends, which is what every parameter declared before this
        # existed says.
        endpoint=str(entry.get("endpoint") or ""),
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
        return tuple(str(value) for value in (entry.get("possible_values") or []))
    collection = _CHOICE_SOURCES.get(named)
    if collection is None:
        raise ConfigError(
            f"Application '{application}' parameter '{entry.get('name')}' names "
            f"choice source '{named}', which is not one of: "
            f"{', '.join(sorted(_CHOICE_SOURCES))}."
        )
    members = getattr(ctx, collection, None)
    if members is None:
        raise ConfigError(
            f"Application '{application}' parameter '{entry.get('name')}' resolves "
            f"its choices from '{named}', which is not on the context. "
            "Applications are built after the collections they read."
        )
    return _member_names(members)


def _member_names(members: Any) -> tuple[str, ...]:
    """A collection's members, as the values a choice offers.

    The member's own name, sorted -- the projection the Console's enrichment
    already used for workflows, kept so the generic resolver answers what the
    one it replaces answered.
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
