"""Applications, built once, from a declaration that is never written to.

The path these replace re-entered the registry on every read, re-normalized
whatever shape it found, and resolved one application's dynamic choices by
checking its name:

    if entry.get("name") != "rey_loader":
        return

These prove the object model that removes it: resolution happens at
construction, the declaration is consumed rather than carried, and a source
nobody implemented is refused instead of answered with nothing.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.config.applications import Application, build_applications
from rey_lib.errors.error_utils import ConfigError


def ctx(apps: Any, **collections: Any) -> SimpleNamespace:
    """A context carrying a declaration and whatever it may resolve against."""
    return SimpleNamespace(apps=apps, **collections)


def declaration(**cli: Any) -> dict[str, Any]:
    """One enabled application declaring the given cli block."""
    return {
        "name": "loader", "app_path": "/apps/loader", "entry_point": "main.py",
        "cli": cli,
    }


class TestConstruction:
    """The declaration in, the object out."""

    def test_an_application_carries_what_execution_needs(self) -> None:
        built = build_applications(ctx([declaration(shared_parameters=True)]))

        assert built[0].name == "loader"
        assert built[0].app_path == "/apps/loader"
        assert built[0].entry_point == "main.py"
        assert built[0].shared_parameters is True

    def test_a_disabled_application_is_not_built(self) -> None:
        entry = {**declaration(), "enabled": False}

        assert build_applications(ctx([entry])) == ()

    def test_a_non_canonical_collection_is_refused(self) -> None:
        # As _ctx_pipelines refuses one. A shape coerced per read is what the
        # old path did, and it is why every reader had to agree about shapes.
        with pytest.raises(ConfigError):
            build_applications(ctx({"loader": declaration()}))

    def test_an_absent_collection_is_not_an_error(self) -> None:
        assert build_applications(SimpleNamespace()) == ()

    def test_an_application_without_a_name_is_refused(self) -> None:
        with pytest.raises(ConfigError):
            build_applications(ctx([{"app_path": "/x"}]))


class TestBothParameterLevels:
    """An application declares parameters, commands, or both."""

    def test_application_level_parameters_are_carried(self) -> None:
        # Several applications declare these and no commands at all, and are
        # invoked with flags and no command word.
        built = build_applications(ctx([declaration(parameters=[{"name": "source"}])]))

        assert [p.name for p in built[0].parameters] == ["source"]

    def test_command_parameters_are_carried(self) -> None:
        built = build_applications(ctx([declaration(commands=[
            {"name": "load", "parameters": [{"name": "file", "required": True}]},
        ])]))

        assert built[0].command("load").parameters[0].required is True

    def test_a_command_that_was_not_declared_is_not_found(self) -> None:
        assert build_applications(ctx([declaration()]))[0].command("nothing") is None


class TestCommandNames:
    """Two declarations for what an application offers, stated once."""

    def test_listed_commands_are_offered(self) -> None:
        built = build_applications(ctx([declaration(commands=[
            {"name": "load"}, {"name": "transform"},
        ])]))

        assert built[0].command_names == ("load", "transform")

    def test_a_positional_command_parameter_is_offered_too(self) -> None:
        # The second declaration, and the only one several applications use.
        built = build_applications(ctx([declaration(parameters=[
            {"name": "command", "positional": True,
             "possible_values": ["load", "sql"]},
        ])]))

        assert built[0].command_names == ("load", "sql")

    def test_both_are_offered_without_duplication(self) -> None:
        built = build_applications(ctx([declaration(
            commands=[{"name": "load"}],
            parameters=[{"name": "command", "positional": True,
                         "possible_values": ["load", "sql"]}],
        )]))

        assert built[0].command_names == ("load", "sql")

    def test_a_non_positional_command_parameter_offers_nothing(self) -> None:
        built = build_applications(ctx([declaration(parameters=[
            {"name": "command", "possible_values": ["load"]},
        ])]))

        assert built[0].command_names == ()


class TestDeclaredChoices:
    """Where a parameter's choices come from, said in configuration."""

    def _built(self, source: str, **collections: Any) -> Application:
        return build_applications(ctx([declaration(parameters=[
            {"name": "workflow", "value_type": "choice",
             "possible_values_from": source},
        ])], **collections))[0]

    def test_choices_are_resolved_from_the_named_collection(self) -> None:
        built = self._built("workflows", workflows=[
            {"name": "beta"}, {"name": "alpha"},
        ])

        # The member's own name, sorted -- the projection the enrichment this
        # replaces already used.
        assert built.parameters[0].possible_values == ("alpha", "beta")

    def test_stated_choices_are_carried_as_stated(self) -> None:
        built = build_applications(ctx([declaration(parameters=[
            {"name": "mode", "possible_values": ["one", "two"]},
        ])]))[0]

        assert built.parameters[0].possible_values == ("one", "two")

    def test_a_source_outside_the_vocabulary_is_refused(self) -> None:
        # Not an empty dropdown discovered later: a configuration fault, at load.
        with pytest.raises(ConfigError) as raised:
            self._built("whatever", whatever=[{"name": "x"}])

        assert "whatever" in str(raised.value)

    def test_a_source_not_yet_on_the_context_is_refused(self) -> None:
        # Applications are built after the collections they read. Being asked
        # before that is the build order being wrong, not an empty answer.
        with pytest.raises(ConfigError) as raised:
            self._built("workflows")

        assert "built after" in str(raised.value)

    def test_no_application_is_named_anywhere(self) -> None:
        # The defect being removed was a name in a branch, so this reads the
        # source rather than trusting behaviour.
        from pathlib import Path
        source = Path(
            "rey_lib/config/applications.py"
        ).read_text(encoding="utf-8")

        assert "rey_loader" not in source


class TestResolvedOnce:
    """A property, not a live view."""

    def test_the_declaration_does_not_survive_onto_the_object(self) -> None:
        # possible_values_from is consumed. A consumer sees values and has
        # nothing to resolve a second time, or differently.
        built = build_applications(ctx([declaration(parameters=[
            {"name": "workflow", "possible_values_from": "workflows"},
        ])], workflows=[{"name": "alpha"}]))[0]

        assert not hasattr(built.parameters[0], "possible_values_from")

    def test_changing_the_source_afterwards_changes_nothing(self) -> None:
        # The proof that resolution happened once. Two agreeing reads would
        # not have shown this.
        workflows = [{"name": "alpha"}]
        context = ctx([declaration(parameters=[
            {"name": "workflow", "possible_values_from": "workflows"},
        ])], workflows=workflows)
        built = build_applications(context)[0]

        workflows.append({"name": "beta"})

        assert built.parameters[0].possible_values == ("alpha",)

    def test_construction_does_not_write_to_the_declaration(self) -> None:
        # ctx.apps is the input. The old enrichment wrote resolved values into
        # the entry it was given; nothing here writes to anything.
        entry = declaration(parameters=[
            {"name": "workflow", "possible_values_from": "workflows"},
        ])
        build_applications(ctx([entry], workflows=[{"name": "alpha"}]))

        assert "possible_values" not in entry["cli"]["parameters"][0]
