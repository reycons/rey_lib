"""Control's AI settings bindings hand their values over unchanged (#1191, #1296).

A None is carried as None -- for a task it is inheritance -- and nothing is
renamed, defaulted or re-derived on this side of the binding. The scope is the
installation: there is no group key to carry.
"""

from __future__ import annotations

from unittest.mock import patch

from rey_lib.control.control import Control


def _control() -> Control:
    """A Control with no database: only its call seam is exercised."""
    return Control.__new__(Control)


def test_the_configuration_is_read_through_its_binding() -> None:
    rows = [{"installation": "local", "task_key": "search_index",
             "task_profile_key": None, "engines": []}]
    with patch.object(Control, "_call_rows", return_value=rows) as called:
        answered = _control().ai_configuration("local")

    called.assert_called_once_with("get_ai_configuration", {"installation": "local"},
                                   required=True)
    assert answered == rows and answered[0] is not rows[0]


def test_the_engines_available_to_an_installation_ride_on_its_rows() -> None:
    engines = [{"profile_key": "anthropic", "is_enabled": True}]
    rows = [{"installation": "local", "task_key": "search_index", "engines": engines},
            {"installation": "local", "task_key": "generate_sql", "engines": engines}]
    with patch.object(Control, "_call_rows", return_value=rows) as called:
        answered = _control().ai_engine_profiles("local")

    called.assert_called_once_with("get_ai_configuration", {"installation": "local"},
                                   required=True)
    assert answered == engines and answered[0] is not engines[0]


def test_an_installation_with_no_rows_offers_no_engines() -> None:
    with patch.object(Control, "_call_rows", return_value=[]):
        assert _control().ai_engine_profiles("local") == []


def test_installation_defaults_are_written_exactly_as_given() -> None:
    with patch.object(Control, "_call") as called:
        _control().update_ai_installation_defaults(
            "local", profile_key="anthropic", instruction_key=None,
            temperature=0.2, representation=None, connection_name="llm_reader")

    called.assert_called_once_with("update_ai_installation_defaults", {
        "installation": "local",
        "profile_key": "anthropic", "instruction_key": None, "temperature": 0.2,
        "representation": None, "connection_name": "llm_reader",
    }, required=True)


def test_a_task_is_written_with_its_inherited_values_as_none() -> None:
    with patch.object(Control, "_call") as called:
        _control().update_ai_installation_task(
            "local", "search_index", profile_key=None,
            instruction_key=None, temperature=0.7, representation=None,
            connection_name=None)

    called.assert_called_once_with("update_ai_installation_task", {
        "installation": "local",
        "task_key": "search_index", "profile_key": None, "instruction_key": None,
        "temperature": 0.7, "representation": None, "connection_name": None,
    }, required=True)
