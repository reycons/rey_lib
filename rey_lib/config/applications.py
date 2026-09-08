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

    def command(self, name: str) -> ApplicationCommand | None:
        """The command of that name, or None where this application has none."""
        return next((one for one in self.commands if one.name == name), None)

    @property
    def command_names(self) -> tuple[str, ...]:
        """Every command name this application advertises.

        Two declarations, both already in use. An application either lists
        ``cli.commands``, or takes a **positional parameter named ``command``**
        whose values are the commands it accepts -- and several declare the
        second and no commands at all.

        Stated once here because it was stated once before, in
        ``execution_service._registered_app_commands``, and two places deciding
        what an application offers is how they come to disagree.
        """
        listed = [one.name for one in self.commands if one.name]
        for parameter in self.parameters:
            if parameter.positional and parameter.name == "command":
                listed.extend(parameter.possible_values)
        return tuple(dict.fromkeys(listed))


def build_applications(ctx: Any) -> tuple[Application, ...]:
    """Return this installation's applications, resolved once.

    Built after the collections a parameter's choices may name, because a
    declaration reading one of them is resolved here rather than later.

    Args:
        ctx: The loaded context, carrying the ``apps`` declaration and the
            collections a choice may be resolved from.

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
    return tuple(
        _application(entry, ctx)
        for entry in (_plain(item) for item in declared)
        if isinstance(entry, dict) and entry.get("enabled", True)
    )


def _application(entry: dict[str, Any], ctx: Any) -> Application:
    """One declaration, as the object."""
    cli = entry.get("cli")
    cli = cli if isinstance(cli, dict) else {}
    name = str(entry.get("name") or "")
    if not name:
        raise ConfigError("An application in 'apps' is declared without a name.")
    return Application(
        name=name,
        label=str(entry.get("label") or name),
        app_type=str(entry.get("type") or entry.get("app_type") or "python"),
        app_path=str(entry.get("app_path") or ""),
        entry_point=str(entry.get("entry_point") or "main.py"),
        shared_parameters=bool(cli.get("shared_parameters")),
        parameters=tuple(
            _parameter(one, ctx, name)
            for one in (_plain(item) for item in (cli.get("parameters") or []))
            if isinstance(one, dict)
        ),
        commands=tuple(
            _command(one, ctx, name)
            for one in (_plain(item) for item in (cli.get("commands") or []))
            if isinstance(one, dict)
        ),
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
