"""Control.subject_panel: the governed read boundary, nothing more (backlog 1215).

A resolved subject's presentation is read through the procedure map's
get_subject_panel binding (tree.f_subject_panel_get). Control passes the subject
key and carries the row back unread; no row is None, never a default.
"""

from __future__ import annotations

from typing import Any

from rey_lib.control.control import Control

#: The file.yaml row as tree.f_subject_panel_get answers it.
_ROW = {"subject_panel_id": 24, "subject_key": "file.yaml",
        "panel": "shared_file_viewer", "panel_settings": {"language": "yaml"}}


def _answering(rows: list[dict[str, Any]]) -> tuple[Control, list[tuple[str, dict[str, Any], bool]]]:
    calls: list[tuple[str, dict[str, Any], bool]] = []
    control = Control.__new__(Control)

    def _call_rows(action: str, variables: dict[str, Any],
                   required: bool = False) -> list[dict[str, Any]]:
        calls.append((action, dict(variables), required))
        return [dict(row) for row in rows]

    control._call_rows = _call_rows  # type: ignore[method-assign]
    return control, calls


def test_control_reads_a_subjects_presentation_by_its_key_alone() -> None:
    control, calls = _answering([_ROW])

    assert control.subject_panel("file.yaml") == _ROW
    # Only the key is sent, and the read is required: an unreachable control
    # database must not read as "nothing is declared".
    assert calls == [("get_subject_panel", {"subject_key": "file.yaml"}, True)]


def test_an_undeclared_subject_reads_as_none() -> None:
    control, _ = _answering([])

    assert control.subject_panel("file.csv") is None
