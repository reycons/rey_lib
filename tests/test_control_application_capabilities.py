"""Control.get_application_capabilities: the governed read boundary, nothing more (#1196 D1).

The canonical-object capabilities an application declares are read through the
procedure map's get_application_capabilities binding
(control.f_application_capability_get). Control passes the application's own name
and carries the declarations back unread: it dispatches nothing, resolves no
executable and judges no applicability -- the owner's invocation does that.
"""

from __future__ import annotations

from typing import Any

from rey_lib.control.control import Control

#: Stand-in declarations: Control carries them and never reads them.
_ROWS = [
    {"capability_key": "file.facts", "description": "the routine's description",
     "target_kinds": ["configured_file"], "input_contract": {"type": "object"},
     "access": "read"},
    {"capability_key": "file.text", "description": "the routine's description",
     "target_kinds": ["configured_file"], "input_contract": {"type": "object"},
     "access": "read"},
]


def _answering(rows: list[dict[str, Any]]) -> tuple[Control, list[tuple[str, dict[str, Any], bool]]]:
    calls: list[tuple[str, dict[str, Any], bool]] = []
    control = Control.__new__(Control)

    def _call_rows(action: str, variables: dict[str, Any],
                   required: bool = False) -> list[dict[str, Any]]:
        calls.append((action, dict(variables), required))
        return [dict(row) for row in rows]

    control._call_rows = _call_rows  # type: ignore[method-assign]
    return control, calls


def test_control_reads_an_applications_declarations_by_its_name_alone() -> None:
    control, calls = _answering(_ROWS)

    declarations = control.get_application_capabilities("data_engine")

    # Carried back exactly as the routine answered, in its order.
    assert declarations == _ROWS
    # Only the application's name is sent -- the declarations are estate-wide, so
    # no installation -- and the read is required by default: a missing binding is
    # a configuration fault, and an owner reading its capabilities fails closed.
    assert calls == [("get_application_capabilities", {"application": "data_engine"}, True)]


def test_an_application_declaring_nothing_reads_as_no_declarations() -> None:
    control, _ = _answering([])

    assert control.get_application_capabilities("workflow_app") == []
