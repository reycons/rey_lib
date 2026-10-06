"""Adapter-declared options, read and edited through the Transform (backlog 687).

Asserted: OpenFIGI declares its five options; the Transform answers them with
their values and offers a column option the mapping's exported output names;
an edit is held as its declared kind and is what executes; and a value of the
wrong kind is refused.
"""

from __future__ import annotations

import json

import pytest

from rey_lib.data.http_transform import http_transform_adapter_for
from rey_lib.errors.error_utils import ConfigError
from rey_lib.load import Transform


def _openfigi() -> Transform:
    held = Transform(
        {"http-connection": "openfigi", "http-adapter": "openfigi"}, selected="http",
    )
    held.observe_source_columns(["asset_id", "cusip"])
    return held


def test_openfigi_declares_its_options() -> None:
    declared = http_transform_adapter_for("openfigi").options()

    assert [(one["name"], one["kind"], one["required"]) for one in declared] == [
        ("id_column", "column", True), ("id_type", "choice", True),
        ("batch_size", "integer", False), ("max_retries", "integer", False),
        ("job", "properties", False),
    ]
    assert "ID_CUSIP" in declared[1]["choices"]


def test_the_transform_answers_them_with_values_and_column_choices() -> None:
    held = _openfigi()
    held.edit_column(0, "export", False)

    by_name = {one["name"]: one for one in held.http_options()}

    assert by_name["id_column"]["choices"] == ["cusip"]
    assert all(one["value"] is None for one in by_name.values())


def test_an_edit_is_held_as_its_kind_and_executes() -> None:
    held = _openfigi()

    held.edit_option("id_column", "cusip")
    held.edit_option("id_type", "ID_CUSIP")
    held.edit_option("batch_size", "10")
    held.edit_option("job", '{"exchCode": "US"}')

    assert json.loads(held.value("http-options")) == {
        "id_column": "cusip", "id_type": "ID_CUSIP", "batch_size": 10, "job": {"exchCode": "US"},
    }
    assert held.executed_declaration()["options"]["batch_size"] == 10
    assert {one["name"]: one["value"] for one in held.http_options()}["id_type"] == "ID_CUSIP"


def test_clearing_an_option_removes_it() -> None:
    held = _openfigi()
    held.edit_option("batch_size", "10")

    held.edit_option("batch_size", "")

    assert held.value("http-options") == ""


@pytest.mark.parametrize(("name", "value"), [
    ("batch_size", "ten"), ("job", "not json"), ("id_type", "NOT_A_TYPE"), ("unknown", "x"),
])
def test_a_wrong_value_is_refused(name: str, value: str) -> None:
    with pytest.raises(ConfigError):
        _openfigi().edit_option(name, value)


def test_no_options_unless_http_is_selected() -> None:
    assert Transform(selected="declaration").http_options() == []
