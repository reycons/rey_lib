"""A profile field's prepared name reaches the field-insert binding.

``Control.insert_data_profile_field`` sends every name in
``DATA_PROFILE_FIELD_COLUMNS`` to the ``insert_data_profile_field`` binding;
the prepared name -- the header create_prepared_files writes -- is one of them.
No database is opened: the routine call is recorded, not made.
"""

from __future__ import annotations

from typing import Any

from rey_lib.control.control import Control


def _recording() -> tuple[Control, list[tuple[str, dict[str, Any]]]]:
    """A Control whose routine calls are recorded instead of made."""
    calls: list[tuple[str, dict[str, Any]]] = []
    control = Control.__new__(Control)
    control._call = lambda action, variables, required=True: calls.append(  # type: ignore[method-assign]
        (action, dict(variables)),
    )
    return control, calls


def test_the_prepared_name_is_sent_with_the_field() -> None:
    control, calls = _recording()

    control.insert_data_profile_field(
        7, "Run Date", "clear", ordinal=1, prepared_name="run_date",
    )

    action, variables = calls[0]
    assert action == "insert_data_profile_field"
    assert variables["field_name"] == "Run Date"
    assert variables["prepared_name"] == "run_date"


def test_an_absent_prepared_name_is_sent_as_none() -> None:
    control, calls = _recording()

    control.insert_data_profile_field(7, "Run Date", "clear", ordinal=1)

    assert calls[0][1]["prepared_name"] is None
