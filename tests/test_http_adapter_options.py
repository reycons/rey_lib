"""Adapter-declared options, read and edited through the Transform (backlog 687).

Asserted with a test-only adapter declaring one option of every kind: the
Transform answers them with their values and offers a column option the
mapping's exported output names; an edit is held as its declared kind and is
what executes; and a value of the wrong kind is refused.
"""

from __future__ import annotations

import json
from typing import Any, Iterator, Mapping

import pytest

from rey_lib.data import http_transform
from rey_lib.data.http_transform import HttpTransformAdapter, http_transform_adapter
from rey_lib.errors.error_utils import ConfigError
from rey_lib.load import Transform


class _Declared(HttpTransformAdapter):
    """Declares one option of every kind; sends nothing."""

    def output_columns(self, input_columns: list[str]) -> list[str]:
        return list(input_columns)

    def options(self) -> list[dict[str, Any]]:
        return [
            {"name": "id_column", "required": True, "kind": "column"},
            {"name": "id_type", "required": True, "kind": "choice", "choices": ["CODE_A", "CODE_B"]},
            {"name": "batch_size", "required": False, "kind": "integer", "default": 5},
            {"name": "job", "required": False, "kind": "properties", "choices": ["region", "venue"]},
        ]

    def apply(self, records: list[dict[str, Any]], connection: Any,
              options: Mapping[str, Any]) -> list[dict[str, Any]]:
        return records


@pytest.fixture(autouse=True)
def _registered() -> Iterator[None]:
    """The test adapter, registered for each test and removed after it."""
    before = dict(http_transform._ADAPTERS)
    http_transform_adapter("declared")(_Declared)
    yield
    http_transform._ADAPTERS.clear()
    http_transform._ADAPTERS.update(before)


def _held() -> Transform:
    held = Transform({"http-connection": "web", "http-adapter": "declared"}, selected="http")
    held.observe_source_columns(["asset_id", "code"])
    return held


def test_the_transform_answers_them_with_values_and_column_choices() -> None:
    held = _held()
    held.edit_column(0, "export", False)

    by_name = {one["name"]: one for one in held.http_options()}

    assert list(by_name) == ["id_column", "id_type", "batch_size", "job"]
    assert by_name["id_column"]["choices"] == ["code"]
    assert all(one["value"] is None for one in by_name.values())


def test_an_edit_is_held_as_its_kind_and_executes() -> None:
    held = _held()

    held.edit_option("id_column", "code")
    held.edit_option("id_type", "CODE_A")
    held.edit_option("batch_size", "10")
    held.edit_option("job", '{"region": "US"}')

    assert json.loads(held.value("http-options")) == {
        "id_column": "code", "id_type": "CODE_A", "batch_size": 10, "job": {"region": "US"},
    }
    assert held.executed_declaration()["options"]["batch_size"] == 10
    assert {one["name"]: one["value"] for one in held.http_options()}["id_type"] == "CODE_A"


def test_clearing_an_option_removes_it() -> None:
    held = _held()
    held.edit_option("batch_size", "10")

    held.edit_option("batch_size", "")

    assert held.value("http-options") == ""


@pytest.mark.parametrize(("name", "value"), [
    ("batch_size", "ten"), ("job", "not json"), ("id_type", "NOT_A_TYPE"), ("unknown", "x"),
])
def test_a_wrong_value_is_refused(name: str, value: str) -> None:
    with pytest.raises(ConfigError):
        _held().edit_option(name, value)


def test_no_options_unless_http_is_selected() -> None:
    assert Transform(selected="declaration").http_options() == []
