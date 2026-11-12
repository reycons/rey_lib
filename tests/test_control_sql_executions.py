"""Control.sql_executions: one database object's past Workbench executions (#1344).

The read is the governed routine's, through the control map's ``get_sql_executions``:
Control names the object -- installation, connection, schema, name -- and returns
the rows the routine answers. Nothing here selects, filters or widens them.
"""

from __future__ import annotations

from typing import Any

from rey_lib.control.control import Control


def _control(rows: list[dict[str, Any]], seen: list[tuple[str, dict[str, Any], bool]]) -> Control:
    """A Control whose routine reads are recorded and answered with ``rows``."""
    control = Control.__new__(Control)

    def call_rows(binding: str, values: dict[str, Any], required: bool = True) -> list[Any]:
        seen.append((binding, dict(values), required))
        return rows

    control._call_rows = call_rows  # type: ignore[method-assign]
    return control


def test_the_object_is_named_to_the_governed_read_and_its_rows_returned() -> None:
    seen: list[tuple[str, dict[str, Any], bool]] = []
    rows = [{"run_log_id": 3308396, "status": "failed", "recorded_ts": "t", "duration_ms": 5}]

    answered = _control(rows, seen).sql_executions(5, "rey_apps", "code", "development_backlog_vw")

    assert seen == [("get_sql_executions", {"installation_id": 5, "connection_name": "rey_apps",
                                            "schema": "code", "name": "development_backlog_vw"},
                     True)]
    assert answered == rows


def test_the_subjects_experience_is_read_through_its_governed_routine() -> None:
    """#1344: one read for reconciliation and recall, scoped by installation,
    application and subject -- the routine owns the scope."""
    seen: list[tuple[str, dict[str, Any], bool]] = []
    subject = {"kind": "views", "name": "development_backlog_vw", "schema": "code",
               "catalog": "", "connection": "rey_apps"}
    rows = [{"ai_experience_id": 7, "validation_state": "validated", "is_authoritative": True,
             "staleness": "current", "verified_claims": []}]

    answered = _control(rows, seen).ai_experiences(1, 6, subject)

    assert seen == [("get_ai_experience", {"installation_id": 1, "application_id": 6,
                                           "subject_identity": subject}, True)]
    assert answered == rows


def test_no_executions_is_an_empty_list() -> None:
    seen: list[tuple[str, dict[str, Any], bool]] = []

    assert _control([], seen).sql_executions(5, "rey_apps", "code", "x", required=False) == []
    assert seen[0][2] is False
