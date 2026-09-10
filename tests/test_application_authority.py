"""Where an application's halves come from, and what happens when one is absent.

A Python application exists through its installed distribution and belongs to an
installation through that installation's declaration. Neither half alone makes
one. These assert that boundary from both directions, because the failure it
prevents -- a registry entry quietly continuing to answer for capability -- is
invisible when it happens.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.config.applications import build_applications
from rey_lib.config.registration import discover_registrations
from rey_lib.errors.error_utils import ConfigError


def ctx(apps: Any, **collections: Any) -> SimpleNamespace:
    return SimpleNamespace(apps=apps, **collections)


def declared(**fields: Any) -> dict[str, Any]:
    """One installation declaration: membership and installation-owned config."""
    return {"name": "loader", "app_path": "/apps/loader", **fields}


def registered(**published: Any) -> dict[str, dict[str, Any]]:
    """What the installed distribution publishes."""
    return {"loader": {"name": "loader", **published}}


class TestBothHalvesAreRequired:
    """Neither discovery nor declaration alone creates an application."""

    def test_a_declared_python_app_with_no_registration_is_refused(self) -> None:
        """Configuration cannot invent an application the environment lacks."""
        with pytest.raises(ConfigError, match="no installed distribution"):
            build_applications(ctx([declared()]), registrations={})

    def test_a_registered_app_the_installation_omits_never_appears(self) -> None:
        """A shared venv serves several installations; discovery is not membership."""
        built = build_applications(ctx([]), registrations=registered())

        assert built == ()

    def test_a_library_never_becomes_an_application(self) -> None:
        """rey_lib has app-shaped configuration and publishes no registration."""
        with pytest.raises(ConfigError, match="no installed distribution"):
            build_applications(
                ctx([{"name": "rey_lib", "app_path": "/apps/rey_lib"}]),
                registrations={},
            )


class TestIdentity:
    """A registration and a declaration cannot describe different applications."""

    def test_disagreeing_identity_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="registered under the name"):
            build_applications(
                ctx([declared()]),
                registrations={"loader": {"name": "something_else"}},
            )

    def test_two_registrations_claiming_one_name_are_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Order of enumeration must never decide which application you get."""
        def one(name: str, value: str) -> Any:
            return SimpleNamespace(
                name=name, value=value, load=lambda: (lambda: {"name": "loader"})
            )

        monkeypatch.setattr(
            "rey_lib.config.registration.entry_points",
            lambda group: [one("a", "a:get"), one("b", "b:get")],
        )
        with pytest.raises(ConfigError, match="Two registrations claim"):
            discover_registrations()

    def test_the_refusal_precedes_any_application(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing is built and then undone."""
        built: list[Any] = []
        monkeypatch.setattr(
            "rey_lib.config.applications.discover_registrations",
            lambda: (_ for _ in ()).throw(ConfigError("Two registrations claim 'loader'")),
        )
        monkeypatch.setattr(
            "rey_lib.config.applications._application",
            lambda *args: built.append(args),
        )
        with pytest.raises(ConfigError):
            build_applications(ctx([declared()]))

        assert built == []


class TestCapabilityHasOneSource:
    """Registration publishes capability. The declaration cannot."""

    def test_commands_come_from_the_registration(self) -> None:
        built = build_applications(
            ctx([declared()]),
            registrations=registered(cli={"commands": [{"name": "index"}]}),
        )

        assert [one.name for one in built[0].commands] == ["index"]

    def test_a_command_removed_from_the_registration_disappears(self) -> None:
        """Stale registry content cannot preserve a command the app dropped."""
        built = build_applications(
            # The declaration still carries the old published surface. It is
            # legacy data: not merged, not a fallback, not an override.
            ctx([declared(cli={"commands": [{"name": "retired"}]})]),
            registrations=registered(cli={"commands": [{"name": "index"}]}),
        )

        assert [one.name for one in built[0].commands] == ["index"]

    def test_a_declaration_alone_publishes_nothing(self) -> None:
        built = build_applications(
            ctx([declared(cli={"commands": [{"name": "retired"}]})]),
            registrations=registered(),
        )

        assert built[0].commands == ()

    def test_workflow_operations_are_empty_until_published(self) -> None:
        """An application that has approved nothing has not approved everything."""
        built = build_applications(ctx([declared()]), registrations=registered())

        assert built[0].workflow_operations == ()

    def test_published_operations_carry_their_parameters(self) -> None:
        built = build_applications(
            ctx([declared()]),
            registrations=registered(workflow_operations=[
                {"name": "index", "parameters": [
                    {"name": "connection", "required": True},
                ]},
            ]),
        )

        operation = built[0].workflow_operations[0]
        assert operation.name == "index"
        assert operation.parameters[0].required is True


class TestInstallationOwnedFields:
    """What the declaration owns, and what it must supply."""

    def test_app_path_is_required_and_never_reconstructed(self) -> None:
        with pytest.raises(ConfigError, match="declares no 'app_path'"):
            build_applications(
                ctx([{"name": "loader"}]), registrations=registered()
            )

    def test_app_path_comes_from_the_declaration(self) -> None:
        built = build_applications(
            ctx([declared(app_path="/installation/owned")]),
            registrations=registered(),
        )

        assert built[0].app_path == "/installation/owned"

    def test_enabled_is_the_installations_answer(self) -> None:
        built = build_applications(
            ctx([declared(enabled=False)]), registrations=registered()
        )

        assert built == ()


class TestExternalApplications:
    """Non-Python extensions keep the registry path, and cannot claim python."""

    def test_an_external_application_needs_no_registration(self) -> None:
        built = build_applications(
            ctx([{
                "name": "db_backup", "type": "shell", "app_path": "/scripts",
                "cli": {"commands": [{"name": "run"}]},
            }]),
            registrations={},
        )

        assert built[0].name == "db_backup"
        assert built[0].app_type == "shell"
        assert [one.name for one in built[0].commands] == ["run"]

    def test_a_registered_app_cannot_be_declared_external(self) -> None:
        """Claiming another type would route a Python app around discovery."""
        with pytest.raises(ConfigError, match="declared as 'shell'"):
            build_applications(
                ctx([declared(type="shell")]), registrations=registered()
            )
