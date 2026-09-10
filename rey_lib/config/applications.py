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
}


@dataclass(frozen=True)
class ApplicationCommandParameter:
    """One declared parameter of one command.

    ``possible_values`` is resolved: either stated outright by the declaration
    or read from the collection the declaration named, once, while this object
    was built. Which of the two it was is deliberately not recorded -- a
    consumer sees values, and has nothing to re-resolve with.
    """

    name: str
    value_type: str = ""
    required: bool = False
    description: str = ""
    positional: bool = False
    possible_values: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApplicationCommand:
    """One command an application exposes, and what it takes."""

    name: str
    description: str = ""
    parameters: tuple[ApplicationCommandParameter, ...] = ()


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
    """One command declaration, as the object."""
    return ApplicationCommand(
        name=str(entry.get("name") or ""),
        description=str(entry.get("description") or ""),
        parameters=tuple(
            _parameter(one, ctx, application)
            for one in (_plain(item) for item in (entry.get("parameters") or []))
            if isinstance(one, dict)
        ),
    )


def _parameter(
    entry: dict[str, Any],
    ctx: Any,
    application: str,
) -> ApplicationCommandParameter:
    """One parameter declaration, with its choices resolved."""
    return ApplicationCommandParameter(
        name=str(entry.get("name") or ""),
        value_type=str(entry.get("value_type") or ""),
        required=bool(entry.get("required")),
        description=str(entry.get("description") or ""),
        positional=bool(entry.get("positional")),
        possible_values=_choices(entry, ctx, application),
    )


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
