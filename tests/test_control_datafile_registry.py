"""Control.get_datafile_registry: one binding, the installation not sent (#1159).

The governed defaults of a DataFile type are read through the procedure map's
get_datafile_registry binding (control.f_datafile_registry_get). The routine
resolves the path; Control passes the type and the path and nothing else.
"""

from __future__ import annotations

from typing import Any

from rey_lib.control.control import Control

#: Stand-in text: Control carries the routine's answer and never reads it.
_ROW = {
    "datafile_type": "CSV",
    "default_sql": "the routine's resolved default statement",
    "connection_name": "file_reader",
}


def _answering(rows: list[dict[str, Any]]) -> tuple[Control, list[tuple[str, dict[str, Any], bool]]]:
    calls: list[tuple[str, dict[str, Any], bool]] = []
    control = Control.__new__(Control)

    def _call_rows(action: str, variables: dict[str, Any],
                   required: bool = False) -> list[dict[str, Any]]:
        calls.append((action, dict(variables), required))
        return [dict(row) for row in rows]

    control._call_rows = _call_rows  # type: ignore[method-assign]
    return control, calls


def test_control_reads_the_type_defaults_by_type_and_path() -> None:
    control, calls = _answering([_ROW])

    row = control.get_datafile_registry("CSV", "/data/a.csv")

    assert row == _ROW
    # The installation is not sent -- the binding reads it off Control -- and
    # the read is required by default: a missing binding is a configuration fault.
    assert calls == [("get_datafile_registry",
                      {"datafile_type": "CSV", "file_path": "/data/a.csv"}, True)]


def test_control_answers_none_when_the_type_has_no_default() -> None:
    control, _ = _answering([])

    assert control.get_datafile_registry("XLSX", "/data/a.xlsx") is None
