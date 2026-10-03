"""The workflow runner's standard controls, defined once (backlog row 608).

An application consumes the parser options, the registration entries and the
mapping from here; the engine keeps every decision about what a selection
means.
"""

from __future__ import annotations

import argparse

import pytest

from rey_lib.workflow.cli import (
    WORKFLOW_SELECTION_PARAMETERS,
    add_workflow_selection_args,
    workflow_selection,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    add_workflow_selection_args(parser)
    return parser


class TestParsing:
    """An application parser accepts the three controls the Console sends."""

    def test_the_three_options_parse(self) -> None:
        args = _parser().parse_args(
            ["--step", "s1", "--from-step", "a", "--to-step", "b"])

        assert (args.step, args.from_step, args.to_step) == ("s1", "a", "b")

    def test_absent_options_are_none(self) -> None:
        args = _parser().parse_args([])

        assert (args.step, args.from_step, args.to_step) == (None, None, None)


class TestTheMapping:
    """Parsed arguments become exactly the engine's keywords."""

    def test_maps_to_the_engine_keywords(self) -> None:
        args = _parser().parse_args(["--step", "inventory_source_files"])

        assert workflow_selection(args) == {
            "step": "inventory_source_files", "from_step": None, "to_step": None,
        }

    def test_a_range_maps_both_ends(self) -> None:
        args = _parser().parse_args(["--from-step", "a", "--to-step", "b"])

        assert workflow_selection(args) == {"step": None, "from_step": "a", "to_step": "b"}

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_value_is_no_selection(self, blank: str) -> None:
        assert workflow_selection(_parser().parse_args(["--step", blank]))["step"] is None

    def test_validation_is_left_to_the_engine(self) -> None:
        """step with a range is passed on as given; the engine refuses it."""
        args = _parser().parse_args(["--step", "a", "--from-step", "b"])

        assert workflow_selection(args) == {"step": "a", "from_step": "b", "to_step": None}


class TestTheRegistration:
    """What an application publishes is what its parser accepts."""

    def test_the_entries_name_the_parser_options(self) -> None:
        parsed = {
            option.lstrip("-")
            for action in _parser()._actions
            for option in action.option_strings
            if option.startswith("--") and option != "--help"
        }

        assert {entry["name"] for entry in WORKFLOW_SELECTION_PARAMETERS} == parsed

    def test_every_entry_is_an_optional_string(self) -> None:
        for entry in WORKFLOW_SELECTION_PARAMETERS:
            assert entry["required"] is False
            assert entry["value_type"] == "string"
            assert entry["description"]
