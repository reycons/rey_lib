"""TransformChoice: one owner per Transform-dropdown choice, and the settings contract.

What is asserted is the contract a surface relies on -- every type the dispatch
knows is a choice, each setting states its own control and exactly the choices a
``select`` offers, and an edit becomes the declaration the choice decides --
and that the starters a surface is offered are the choices' own.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.data import TRANSFORM_CHOICES, authorable_starters, choice_for
from rey_lib.data.transform_choice import CONTROLS
from rey_lib.errors.error_utils import ConfigError

_GOVERNED = SimpleNamespace(governed_context=lambda: {
    "file_manifest_id": 3, "file_mutation_id": 12, "installation_id": 1,
    "data_profile_id": 4, "file_name": "a.csv", "mutation_choices": [{}],
})


class TestEveryChoiceIsOne:

    def test_every_type_the_dispatch_executes_is_a_choice(self) -> None:
        assert set(TRANSFORM_CHOICES) == {
            "constant", "context", "date", "datetime", "time", "numeric", "boolean",
            "regex_extract", "regex_date", "prefix_map", "strip_parens_suffix",
            "file_hash", "not_blank", "hash", "passthrough", "encrypt",
        }

    def test_the_starters_are_the_authorable_choices_declarations(self) -> None:
        assert authorable_starters() == {
            name: one.declaration for name, one in TRANSFORM_CHOICES.items() if one.authorable
        }
        assert "encrypt" not in authorable_starters()
        # The dropdown's "none": the stored spelling of no rule, offered by the choice.
        assert authorable_starters()["passthrough"] == {"type": "passthrough"}

    def test_prefix_map_and_regex_date_are_not_authorable_but_still_choices(self) -> None:
        assert "prefix_map" not in authorable_starters()
        assert "regex_date" not in authorable_starters()
        assert choice_for("prefix_map") is not None
        assert choice_for("regex_date") is not None

    def test_the_one_setting_states_a_known_control_and_authors_a_starter_key(self) -> None:
        for one in TRANSFORM_CHOICES.values():
            if one.setting is not None:
                assert one.setting.control in CONTROLS, one.name
                assert one.setting.key in one.declaration, one.name

    def test_each_authorable_choice_authors_one_setting_at_most(self) -> None:
        assert {
            name: (one.setting.name if one.setting else None)
            for name, one in TRANSFORM_CHOICES.items() if one.authorable
        } == {
            "constant": "value", "context": "value", "date": "format",
            "datetime": "format", "time": "format", "numeric": "strip_chars",
            "boolean": None, "regex_extract": "pattern", "strip_parens_suffix": None,
            "file_hash": None, "hash": "columns", "passthrough": None,
        }

    def test_the_input_is_never_a_setting(self) -> None:
        assert all(
            "source" not in one.declaration
            for one in TRANSFORM_CHOICES.values() if one.authorable
        )

    def test_an_unknown_type_is_no_choice(self) -> None:
        assert choice_for("nothing") is None


class TestTheSettingsContract:

    def test_context_is_a_select_of_the_sources_single_values_by_bare_name(self) -> None:
        settings = choice_for("context").settings_for(
            {"type": "context", "value": "source.file_mutation_id"}, _GOVERNED,
        )

        assert settings == [{
            "name": "value", "label": "Value", "control": "select",
            "value": "file_mutation_id",
            "choices": [
                "file_manifest_id", "file_mutation_id", "installation_id",
                "data_profile_id", "file_name",
            ],
        }]

    def test_with_no_source_a_select_offers_nothing(self) -> None:
        assert choice_for("context").settings_for(None, None)[0]["choices"] == []

    def test_a_text_setting_states_text_and_offers_no_choices(self) -> None:
        settings = choice_for("date").settings_for(None, _GOVERNED)

        assert [(one["name"], one["control"], one["choices"]) for one in settings] == [
            ("format", "text", []),
        ]

    def test_a_choice_without_settings_draws_nothing(self) -> None:
        assert choice_for("file_hash").settings_for({"type": "file_hash"}, _GOVERNED) == []

    def test_a_structured_value_is_shown_as_json(self) -> None:
        settings = choice_for("hash").settings_for(
            {"type": "hash", "hash_type": "sha256", "columns": ["a", "b"]}, None,
        )

        assert settings[0]["value"] == '["a", "b"]'

    def test_a_non_authorable_choice_draws_nothing(self) -> None:
        assert choice_for("prefix_map").settings_for(
            {"type": "prefix_map", "source": "s", "prefixes": {"B": "Buy"}}, None,
        ) == []


class TestAnEditBecomesTheDeclaration:

    def test_a_chosen_source_name_is_stored_as_its_reference(self) -> None:
        built = choice_for("context").edit(
            {"type": "context", "value": "ctx.row_num"}, "value", "file_mutation_id", _GOVERNED,
        )

        assert built == {"type": "context", "value": "source.file_mutation_id"}

    @pytest.mark.parametrize(("setting", "value"), [
        ("value", "run_id"), ("nothing", "x"),
    ])
    def test_what_the_choice_does_not_offer_is_refused(self, setting: str, value: str) -> None:
        with pytest.raises(ConfigError):
            choice_for("context").edit(None, setting, value, _GOVERNED)

    @pytest.mark.parametrize(("name", "setting", "text", "stored"), [
        ("regex_extract", "pattern", "^(\\w+)", "^(\\w+)"),
        ("hash", "columns", '["a", "b"]', ["a", "b"]),
        ("date", "format", "dd/MM/yyyy", "dd/MM/yyyy"),
    ])
    def test_text_is_typed_as_the_starter_holds_that_key(
        self, name: str, setting: str, text: str, stored: Any,
    ) -> None:
        built = choice_for(name).edit(None, setting, text, None)

        assert built[choice_for(name).setting.key] == stored

    def test_a_setting_the_choice_no_longer_has_is_refused(self) -> None:
        with pytest.raises(ConfigError):
            choice_for("date").edit(None, "output_format", "yyyy", None)
        with pytest.raises(ConfigError):
            choice_for("regex_extract").edit(None, "source", "Symbol", None)

    def test_unreadable_structured_text_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="JSON"):
            choice_for("hash").edit(None, "columns", "[nope", None)

    def test_the_given_rule_is_not_changed(self) -> None:
        given = {"type": "date", "format": "a", "output_format": "b"}

        choice_for("date").edit(given, "format", "c", None)

        assert given == {"type": "date", "format": "a", "output_format": "b"}
