"""The Transform's HTTP lifecycle, driven with no Console at all (backlog 829).

Rule ``loader_semantics_survive_without_the_console``: everything here is the
canonical objects alone -- a Transform established ``from_source`` on a runtime
carrying a Control, and the HttpTransformSource it creates. One read; atomic
kind selection; the operation chosen in memory; the request held for display
and never a credential, never an execution argument.

Synthetic only: neutral job rows answered by a recording Control.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.errors.error_utils import ConfigError, StateError
from rey_lib.load.execution import load_arguments
from rey_lib.load.http_transform_source import HttpTransformSource
from rey_lib.load.source import Source
from rey_lib.load.target import Target
from rey_lib.load.transform import Transform

_MAPPING = '{"columns": [{"name": "code", "source": "code"}]}'


def _job(key: str, **overrides: Any) -> dict[str, Any]:
    """A neutral job row, as f_http_operation_get shapes it."""
    job: dict[str, Any] = {
        "adapter_key": key, "connection_name": "site",
        "base_url": "https://api.example.test/v1/", "auth_header": "X-Api-Key",
        "auth_prefix": None, "auth_secret_ref": "env.example_api_key", "auth_required": True,
        "timeout_seconds": 5, "verify": True,
        "default_headers": {"User-Agent": "rey test", "Accept": "application/json"},
        "http_method": "POST", "path": "find", "request_format": "json",
        "response_format": "json", "is_enabled": True,
        "request_body_fields": ["idType", "idValue"],
        "request_body_kinds": ["literal", "reference"],
        "request_body_values": ["TICKER", "symbol"],
        "request_query_fields": ["limit"], "request_query_kinds": ["literal"],
        "request_query_values": ["10"],
        "response_field_names": ["found"], "response_field_paths": ["$.name"],
        "batch_size": 2, "correlation_mode": "position", "response_records_path": "$.data",
    }
    job.update(overrides)
    return job


class _Control:
    """Answers http_operation from fixed rows, recording every call."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[str] = []

    def http_operation(self, installation: str) -> list[dict[str, Any]]:
        self.calls.append(installation)
        return [dict(row) for row in self.rows]


def _control() -> _Control:
    return _Control([_job("alpha"), _job("beta", is_enabled=False), _job("gamma")])


def _runtime(control: Any) -> SimpleNamespace:
    """A runtime carrying its shared Control, as open_shared_control leaves one."""
    return SimpleNamespace(shared_control=control, installation=SimpleNamespace(name="local"))


def _established(control: Any, declaration: dict[str, Any] | None = None) -> Transform:
    """A Transform established as the Loader establishes one: from its Source."""
    return Transform.from_source(_runtime(control), Source.from_declaration({}), declaration)


_REQUEST_ALPHA = {
    "method": "POST",
    "fields": [
        {"part": "body", "name": "idType", "kind": "literal", "value": "TICKER"},
        {"part": "body", "name": "idValue", "kind": "reference", "value": "symbol"},
        {"part": "query", "name": "limit", "kind": "literal", "value": "10"},
    ],
    "headers": [
        {"part": "header", "name": "Accept", "kind": "configured", "value": "application/json"},
        {"part": "header", "name": "User-Agent", "kind": "configured", "value": "rey test"},
    ],
}


class TestTheSource:
    def test_one_read_and_the_enabled_choices_in_read_order(self) -> None:
        control = _control()

        source = HttpTransformSource.create(control, "local")

        assert control.calls == ["local"]
        assert source.transform_choices == ["alpha", "gamma"]
        assert source.choices() == [
            {"value": "alpha", "label": "alpha"}, {"value": "gamma", "label": "gamma"},
        ]
        assert source.selected_operation is None

    def test_answers_a_request_without_selecting(self) -> None:
        source = HttpTransformSource.create(_control(), "local")

        assert source.request("alpha") == _REQUEST_ALPHA
        assert source.selected_operation is None

    @pytest.mark.parametrize("key", ["beta", "missing", ""])
    def test_refuses_what_is_not_an_enabled_operation(self, key: str) -> None:
        source = HttpTransformSource.create(_control(), "local")

        with pytest.raises(ConfigError, match="not an enabled operation"):
            source.request(key)
        with pytest.raises(ConfigError, match="not an enabled operation"):
            source.select_operation(key)

    def test_no_credential_is_in_the_request(self) -> None:
        shown = repr(HttpTransformSource.create(_control(), "local").request("alpha"))

        assert "X-Api-Key" not in shown
        assert "env.example_api_key" not in shown


class TestChoosingTheKind:
    def test_selecting_http_reads_once_and_offers_the_operations(self) -> None:
        control = _control()
        transform = _established(control)

        transform.select("http")
        transform.select("http")

        assert control.calls == ["local"]
        assert transform.selected_kind() == "http"
        assert transform.choices() == {"http-adapter": [
            {"value": "alpha", "label": "alpha"}, {"value": "gamma", "label": "gamma"},
        ]}
        assert transform.field_operations() == {"http-adapter": "select_http_operation"}
        assert transform.http_request() is None

    def test_without_a_control_database_http_is_refused_and_nothing_changes(self) -> None:
        transform = _established(None, {"selected": "declaration"})

        with pytest.raises(ConfigError, match="control database"):
            transform.select("http")

        assert transform.selected_kind() == "declaration"
        assert transform.choices() == {} and transform.field_operations() == {}

    def test_a_refused_named_operation_leaves_the_kind_unchanged(self) -> None:
        transform = _established(_control(), {
            "selected": "declaration", "values": {"http-adapter": "beta"},
        })

        with pytest.raises(ConfigError, match="not an enabled operation"):
            transform.select("http")

        assert transform.selected_kind() == "declaration"
        assert transform.choices() == {}

    def test_leaving_http_lets_the_source_and_request_go(self) -> None:
        transform = _established(_control(), {
            "selected": "http", "values": {"http-adapter": "alpha"},
        })

        # As ManifestSource.populate does: no Console, no special path.
        transform.select("declaration")

        assert transform.http_request() is None
        assert transform.choices() == {} and transform.field_operations() == {}
        assert transform.value("http-adapter") == "alpha"

    def test_returning_to_http_reads_again_and_repopulates(self) -> None:
        control = _control()
        transform = _established(control, {"selected": "http", "values": {"http-adapter": "alpha"}})
        transform.select("declaration")

        transform.select("http")

        assert control.calls == ["local", "local"]
        assert transform.http_request() == _REQUEST_ALPHA


class TestEstablishment:
    def test_declared_in_http_it_is_established_and_reconciled(self) -> None:
        control = _control()

        transform = _established(control, {
            "selected": "http", "values": {"http-adapter": "gamma", "http-transform": _MAPPING},
        })

        assert control.calls == ["local"]
        assert transform.http_request()["method"] == "POST"
        assert transform.value("http-transform") == _MAPPING

    def test_declared_in_http_without_control_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="control database"):
            _established(None, {"selected": "http"})

    def test_a_declarative_rebuild_establishes_nothing(self) -> None:
        # What execution rebuilds from: no Control, no read, no refusal.
        transform = Transform.from_declaration({
            "selected": "http", "values": {"http-adapter": "alpha"},
        })

        assert transform.selected_kind() == "http"
        assert transform.choices() == {} and transform.http_request() is None


class TestChoosingTheOperation:
    def test_chosen_in_memory_with_its_request(self) -> None:
        control = _control()
        transform = _established(control, {"selected": "http"})

        transform.select_http_operation("http-adapter", "alpha")

        assert control.calls == ["local"]
        assert transform.value("http-adapter") == "alpha"
        assert transform.http_request() == _REQUEST_ALPHA

    def test_a_refused_operation_leaves_the_choice_as_it_was(self) -> None:
        transform = _established(_control(), {"selected": "http", "values": {"http-adapter": "alpha"}})

        with pytest.raises(ConfigError, match="not an enabled operation"):
            transform.select_http_operation("http-adapter", "beta")

        assert transform.value("http-adapter") == "alpha"
        assert transform.http_request() == _REQUEST_ALPHA

    def test_an_update_of_the_field_is_choosing_an_operation(self) -> None:
        transform = _established(_control(), {"selected": "http"})

        transform.update("http-adapter", "gamma")

        assert transform.http_request()["method"] == "POST"
        with pytest.raises(ConfigError, match="not an enabled operation"):
            transform.update("http-adapter", "beta")

    def test_needs_an_http_transform(self) -> None:
        with pytest.raises(StateError, match="not in kind http"):
            _established(_control(), {"selected": "declaration"}).select_http_operation(
                "http-adapter", "alpha",
            )

    def test_only_through_the_selection_field(self) -> None:
        with pytest.raises(ConfigError, match="is chosen through"):
            _established(_control(), {"selected": "http"}).select_http_operation(
                "http-transform", "alpha",
            )


class TestTheHeldStateIsNeverConfiguration:
    def test_it_is_neither_declared_nor_an_execution_argument(self) -> None:
        transform = _established(_control(), {"selected": "http", "values": {"http-adapter": "alpha"}})

        assert transform.declaration() == {
            "selected": "http", "values": {"http-adapter": "alpha"},
        }
        assert load_arguments(
            Source.from_declaration({}), transform, Target.from_declaration({}),
        ) == {"http-adapter": "alpha"}
