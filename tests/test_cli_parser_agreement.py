"""Every registration's cli declaration agrees with its application's parser (backlog 940).

The registration is the published command surface (rule
a_command_surface_is_declared_in_registration); the parser is what the
application actually accepts. Each installed application's entry point is
loaded and its argparse parser captured at ``parse_args`` -- nothing past
argument parsing runs -- and compared with what rey_lib reads from the
registration:

- parameter groups agree exactly, at both scopes;
- the rest of the surface (commands, parameters, shared parameters) differs
  only where the drift is already recorded, so new drift fails.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import inspect
import sys
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest

from rey_lib.config.applications import _commands, _parameter, _parameter_groups
from rey_lib.config.cli import add_config_args
from rey_lib.config.registration import discover_registrations

#: The surface drift measured 2026-10-13 when this test was written, as
#: (application, scope, name, side). Scope is ``shared``, ``commands``, the
#: application itself (``""``) or a command; side says which of the two holds
#: the name. Recorded on the backlog (cli_declarations_drift_from_parsers);
#: a row leaves this table when its declaration is fixed.
_KNOWN_DRIFT: set[tuple[str, str, str, str]] = {
    ('data_engine', '', 'generated-yaml', 'parser'),
    ('data_engine', '', 'max-sample-rows', 'parser'),
    ('data_engine', '', 'review-dir', 'parser'),
    ('data_engine', 'shared', 'connection-alias', 'parser'),
    ('data_engine', 'shared', 'log-level', 'parser'),
    ('data_engine', 'shared', 'run-id', 'parser'),
    ('ftp_sync', 'shared', 'connection-alias', 'parser'),
    ('ftp_sync', 'shared', 'log-level', 'parser'),
    ('ftp_sync', 'shared', 'run-id', 'parser'),
    ('rey_analyzer', 'approve', 'run-id', 'registration'),
    ('rey_analyzer', 'reject', 'run-id', 'registration'),
    ('rey_analyzer', 'shared', 'connection-alias', 'parser'),
    ('rey_analyzer', 'shared', 'log-level', 'parser'),
    ('rey_analyzer', 'shared', 'run-id', 'parser'),
    ('rey_analyzer', 'status', 'run-id', 'registration'),
    ('rey_console', '', 'console-config-path', 'parser'),
    ('rey_console', '', 'host', 'parser'),
    ('rey_console', '', 'installation', 'parser'),
    ('rey_console', '', 'port', 'parser'),
    ('rey_console', 'shared', 'config-dir', 'parser'),
    ('rey_console', 'shared', 'config-path', 'parser'),
    ('rey_console', 'shared', 'connection-alias', 'parser'),
    ('rey_console', 'shared', 'console-config-path', 'registration'),
    ('rey_console', 'shared', 'dashboard', 'registration'),
    ('rey_console', 'shared', 'host', 'registration'),
    ('rey_console', 'shared', 'installation', 'registration'),
    ('rey_console', 'shared', 'port', 'registration'),
    ('rey_console', 'shared', 'run-id', 'parser'),
    ('rey_db_admin', 'export-ddl', 'object-filter', 'parser'),
    ('rey_db_admin', 'shared', 'connection-alias', 'parser'),
    ('rey_db_admin', 'shared', 'log-level', 'parser'),
    ('rey_db_admin', 'shared', 'run-id', 'parser'),
    ('data_engine', 'shared', 'connection-alias', 'parser'),
    ('data_engine', 'shared', 'log-level', 'parser'),
    ('data_engine', 'shared', 'run-id', 'parser'),
    ('rey_messaging', '', 'no-dry-run', 'registration'),
    ('rey_messaging', 'shared', 'connection-alias', 'parser'),
    ('rey_messaging', 'shared', 'log-level', 'parser'),
    ('rey_messaging', 'shared', 'run-id', 'parser'),
    ('rey_repository_map', 'commands', 'check-bindings', 'parser'),
    # workflow_app and ai_app share the estate-wide drift of the three shared
    # flags rey_lib's parser adds (#1027); the declarations are fixed across
    # every application together under #945.
    ('ai_app', 'shared', 'connection-alias', 'parser'),
    ('ai_app', 'shared', 'log-level', 'parser'),
    ('ai_app', 'shared', 'run-id', 'parser'),
    ('workflow_app', 'shared', 'connection-alias', 'parser'),
    ('workflow_app', 'shared', 'log-level', 'parser'),
    ('workflow_app', 'shared', 'run-id', 'parser'),
}

#: The positional parameter whose values are an application's commands.
_COMMAND = "command"

#: Applications whose one declared surface is implemented by several delegated
#: parsers, each an entry point of its own beneath the application root. Its
#: main.py hands an invocation to one of them, so the parser that main.py
#: reaches under pytest is only part of the surface; every one is captured and
#: their union is compared (#1110: data_engine's loader and file parsers).
_DELEGATED_PARSERS: dict[str, tuple[str, ...]] = {
    "data_engine": ("data_engine/loader_main.py", "data_engine/file_main.py"),
}

#: Declared exclusive groups a parser enforces once it has parsed -- its own check,
#: not an argparse group (#1236) -- by application and command: the entry point
#: whose parser it is, what every invocation of the command carries, and what a
#: member needs beside it. Each is verified by running that parser: every member
#: alone is accepted, any two are refused, and none is refused when required.
_ENFORCED_AFTER_PARSING: dict[tuple[str, str], dict[str, Any]] = {
    # loader_main._check_serve_arguments: exactly one Load identity, a statement
    # with the connection it runs on, always on the selected subject.
    ("data_engine", "serve_load"): {
        "entry": "data_engine/loader_main.py",
        "given": ["--subject", "file"],
        "companions": {"statement": ["--source-connection", "warehouse"]},
    },
}

#: argparse's own parse_args, which ``_parser`` replaces with a capture.
_PARSE_ARGS = argparse.ArgumentParser.parse_args


class _Captured(Exception):
    """Raised in place of parsing, carrying the parser that was about to parse."""

    def __init__(self, parser: argparse.ArgumentParser) -> None:
        super().__init__(parser.prog)
        self.parser = parser


def _capture(self: argparse.ArgumentParser, *_args: Any, **_kwargs: Any) -> None:
    raise _Captured(self)


class _NoCollections:
    """A context whose every collection is empty: choices are not compared here.

    ``shared_control`` is None: a runtime with no control database, which the
    reader treats as offering no registry-held choices.
    """

    shared_control = None

    def __getattr__(self, _name: str) -> list:
        return []


def _option(action: argparse.Action) -> str:
    """An action's name as a registration declares it: the long flag, undashed."""
    longs = [one for one in action.option_strings if one.startswith("--")]
    return (longs or action.option_strings or [action.dest])[0].lstrip("-")


def _scope(parser: argparse.ArgumentParser) -> dict[str, Any]:
    """One parser level: options, exclusive groups, subcommands, command choices."""
    options: set[str] = set()
    commands: dict[str, dict[str, Any]] = {}
    choices: set[str] = set()
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        if isinstance(action, argparse._SubParsersAction):
            commands.update({name: _scope(sub) for name, sub in action.choices.items()})
            continue
        options.add(_option(action))
        if not action.option_strings and action.dest == _COMMAND and action.choices:
            choices = {str(one) for one in action.choices}
    groups = {
        (group.required, frozenset(_option(one) for one in group._group_actions))
        for group in parser._mutually_exclusive_groups
    }
    return {"options": options, "groups": groups, "commands": commands, "choices": choices}


def _parser(name: str, registration: dict[str, Any], monkeypatch: Any) -> dict[str, Any]:
    """The application's own parser surface, captured before anything is parsed.

    One parser for an application whose entry point builds it; for one whose
    surface is delegated (``_DELEGATED_PARSERS``), every delegate's, combined.
    """
    root = Path(importlib.import_module(f"{name}.registration").__file__).parents[1]
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", _capture)
    delegates = _DELEGATED_PARSERS.get(name)
    if delegates is None:
        entry = root / str(registration.get("entry_point") or "main.py")
        return _captured_scope(name, entry, name)
    return _combined(name, {
        one: _captured_scope(name, root / one, f"{name}_{Path(one).stem}") for one in delegates
    })


def _combined(name: str, scopes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Several delegated parsers as one surface; a command two of them take conflicts."""
    owners: dict[str, list[str]] = {}
    for delegate, scope in scopes.items():
        for command in set(scope["commands"]) | scope["choices"]:
            owners.setdefault(command, []).append(delegate)
    conflicts = {command: found for command, found in owners.items() if len(found) > 1}
    if conflicts:
        pytest.fail(f"{name}: commands claimed by more than one delegated parser: {conflicts}")
    return {
        "options": set().union(*(one["options"] for one in scopes.values())),
        "groups": set().union(*(one["groups"] for one in scopes.values())),
        "commands": {k: v for one in scopes.values() for k, v in one["commands"].items()},
        "choices": set().union(*(one["choices"] for one in scopes.values())),
    }


def _captured_scope(name: str, entry: Path, module_name: str) -> dict[str, Any]:
    """One entry point's parser, captured at ``parse_args``."""
    spec = importlib.util.spec_from_file_location(f"_cli_agreement_{module_name}", entry)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ModuleNotFoundError as exc:
        pytest.skip(f"{name}'s entry point cannot be imported here: {exc}")
    build = getattr(module, "_parse_args", None) or getattr(module, "main", None)
    if build is None:
        pytest.skip(f"{name}'s entry point builds no argparse parser")
    try:
        build(*([[]] if inspect.signature(build).parameters else []))
    except _Captured as captured:
        return _scope(captured.parser)
    pytest.fail(f"{name}'s entry point returned without parsing its arguments")


def _declared(name: str, registration: dict[str, Any]) -> tuple[Any, Any, Any]:
    """What rey_lib reads from the registration: parameters, groups and commands."""
    cli = registration.get("cli") or {}
    ctx = _NoCollections()
    parameters = tuple(_parameter(one, ctx, name) for one in cli.get("parameters") or [])
    return parameters, _parameter_groups(cli, parameters, name), _commands(cli, ctx, name)


def _exclusive(groups: Any) -> set[tuple[bool, frozenset[str]]]:
    return {(one.required, frozenset(one.members)) for one in groups if one.mutually_exclusive}


_NAMES = sorted(discover_registrations())


@pytest.fixture(scope="module")
def registrations() -> dict[str, dict[str, Any]]:
    return discover_registrations()


def _refused(entry: Path, argv: list[str], monkeypatch: Any) -> bool:
    """Whether the entry point's own parser refuses ``argv`` -- run for real."""
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", _PARSE_ARGS)
    monkeypatch.setattr(sys, "argv", ["main.py", "--config-path", "/c", *argv])
    spec = importlib.util.spec_from_file_location(f"_cli_enforced_{entry.stem}", entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        module._parse_args()
    except SystemExit:
        return True
    return False


def _enforced_after_parsing(
    name: str, command: str, group: tuple[bool, frozenset[str]], monkeypatch: Any,
) -> bool:
    """Whether the command's parser enforces this declared group by its own check."""
    declared = _ENFORCED_AFTER_PARSING.get((name, command))
    if declared is None:
        return False
    root = Path(importlib.import_module(f"{name}.registration").__file__).parents[1]
    entry = root / declared["entry"]
    required, members = group

    def invocation(*chosen: str) -> list[str]:
        argv = [command]
        for member in chosen:
            argv += [f"--{member}", "1", *declared["companions"].get(member, [])]
        return [*argv, *declared["given"]]

    alone = all(not _refused(entry, invocation(one), monkeypatch) for one in sorted(members))
    together = all(_refused(entry, invocation(*two), monkeypatch)
                   for two in combinations(sorted(members), 2))
    neither = _refused(entry, invocation(), monkeypatch) if required else True
    return alone and together and neither


@pytest.mark.parametrize("name", _NAMES)
def test_parameter_groups_agree_with_the_parser(name, registrations, monkeypatch) -> None:
    parser = _parser(name, registrations[name], monkeypatch)
    _parameters, groups, commands = _declared(name, registrations[name])

    actual = {"": parser["groups"]}
    actual.update({command: scope["groups"] for command, scope in parser["commands"].items()})
    declared = {"": _exclusive(groups)}
    declared.update({one.name: _exclusive(one.parameter_groups) for one in commands})
    # A group the parser enforces once it has parsed agrees when running the parser
    # shows it holding; it is then not looked for among the argparse groups.
    for command, held in declared.items():
        found = actual.get(command, set())
        declared[command] = {one for one in held if one in found
                             or not _enforced_after_parsing(name, command, one, monkeypatch)}
    assert ({scope: one for scope, one in declared.items() if one}
            == {scope: one for scope, one in actual.items() if one})


@pytest.mark.parametrize("name", _NAMES)
def test_the_declared_surface_drifts_only_where_recorded(name, registrations, monkeypatch) -> None:
    drift = _drift(name, registrations[name], monkeypatch)
    assert drift == {one for one in _KNOWN_DRIFT if one[0] == name}


def _drift(
    name: str, registration: dict[str, Any], monkeypatch: Any,
) -> set[tuple[str, str, str, str]]:
    """Every name one side holds and the other does not, at each scope."""
    parser = _parser(name, registration, monkeypatch)
    cli = registration.get("cli") or {}
    parameters, _groups, commands = _declared(name, registration)
    shared = _scope(_shared_parser())["options"]
    drift: set[tuple[str, str, str, str]] = set()

    def compare(scope: str, parsed: set[str], declared: set[str]) -> None:
        drift.update((name, scope, one, "parser") for one in parsed - declared)
        drift.update((name, scope, one, "registration") for one in declared - parsed)

    listed = cli.get("shared_parameters")
    if isinstance(listed, list):
        compare("shared", shared, {one["name"] for one in listed})
    compare("commands", set(parser["commands"]) | parser["choices"],
            {one.name for one in commands})

    own = {one.name for one in parameters}
    for command in cli.get("commands") or []:
        names = {one["name"] for one in command.get("parameters") or []}
        if command["name"] in parser["commands"]:
            compare(command["name"], parser["commands"][command["name"]]["options"] - shared,
                    names)
        else:
            # A flat parser: the command's parameters are the application's options.
            own |= names
    if parser["choices"]:
        own.add(_COMMAND)
    compare("", parser["options"] - (shared if listed else set()), own)
    return drift


def _shared_parser() -> argparse.ArgumentParser:
    """A parser holding only rey_lib's shared configuration arguments."""
    parser = argparse.ArgumentParser()
    add_config_args(parser)
    return parser
