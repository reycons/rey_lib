"""Control's AI settings bindings hand their values over unchanged (#1191).

A None is carried as None -- for a task it is inheritance -- and nothing is
renamed, defaulted or re-derived on this side of the binding.
"""

from __future__ import annotations

from unittest.mock import patch

from rey_lib.control.control import Control


def _control() -> Control:
    """A Control with no database: only its call seam is exercised."""
    return Control.__new__(Control)


def test_the_stated_settings_are_read_through_their_binding() -> None:
    rows = [{"ai_group_key": "console_default", "task_key": "search_index",
             "task_profile_key": None}]
    with patch.object(Control, "_call_rows", return_value=rows) as called:
        answered = _control().ai_settings("local")

    called.assert_called_once_with("get_ai_settings", {"installation": "local"},
                                   required=True)
    assert answered == rows and answered[0] is not rows[0]


def test_group_defaults_are_written_exactly_as_given() -> None:
    with patch.object(Control, "_call") as called:
        _control().update_ai_group_defaults(
            "local", "console_default", profile_key="anthropic", instruction_key=None,
            temperature=0.2, representation=None, connection_name="llm_reader")

    called.assert_called_once_with("update_ai_group_defaults", {
        "installation": "local", "ai_group_key": "console_default",
        "profile_key": "anthropic", "instruction_key": None, "temperature": 0.2,
        "representation": None, "connection_name": "llm_reader",
    }, required=True)


def test_a_task_is_written_with_its_inherited_values_as_none() -> None:
    with patch.object(Control, "_call") as called:
        _control().update_ai_group_task(
            "local", "console_default", "search_index", profile_key=None,
            instruction_key=None, temperature=0.7, representation=None,
            connection_name=None)

    called.assert_called_once_with("update_ai_group_task", {
        "installation": "local", "ai_group_key": "console_default",
        "task_key": "search_index", "profile_key": None, "instruction_key": None,
        "temperature": 0.7, "representation": None, "connection_name": None,
    }, required=True)
