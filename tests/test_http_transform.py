"""Records through an HTTP service and back: HTTPTransform (backlog 671).

Asserted: the adapter contract and its registry; the http kind is implemented
but hidden until 668; resolution builds an HTTPTransform from a shared HTTP
connection and a registered adapter and refuses anything else; the transform,
its schema, preview and a load carry the adapter's records; output_columns
sends nothing; what ran is recorded without a secret; errors pass through.

Synthetic only: a test-only adapter, registered for each test and removed
after, sends through an HttpConnection answered by httpx.MockTransport. The
load path's connection lookup is swapped in the test (backlog 671, E).
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping
from unittest.mock import MagicMock

import httpx
import pytest
from corehttp.transport.httpx import HttpXTransport

from rey_lib.data import http_transform
from rey_lib.data.http_transform import (
    HTTPTransform, HttpTransformAdapter, http_transform_adapter, http_transform_adapter_for, http_transform_adapters,
)
from rey_lib.db.connection import Connection
from rey_lib.errors.error_utils import ConfigError, HttpTransportError
from rey_lib.load import Source, Target, Transform, run_selected_load
from rey_lib.load import load_operation
from rey_lib.load.execution import preview_selected
from rey_lib.load.transform import TRANSFORM_FIELDS, TRANSFORM_KINDS
from rey_lib.web_utils import HttpConnection, HttpRequest

_SECRET = "synthetic-api-key"


class _Echo(HttpTransformAdapter):
    """Adds `echoed` to each record from one POST of all records, by position."""

    def output_columns(self, input_columns: list[str]) -> list[str]:
        return [*input_columns, "echoed"]

    def apply(
        self, records: list[dict[str, Any]], connection: HttpConnection,
        options: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        response = connection.send_request(HttpRequest(
            "POST", connection.format_url(str(options.get("path", "echo"))), json=records,
        ))
        answers = response.json()
        return [{**record, "echoed": answer["echoed"]} for record, answer in zip(records, answers)]


class _Elsewhere(_Echo):
    """Breaks the boundary: asks for another origin."""

    def apply(self, records: Any, connection: HttpConnection, options: Any) -> Any:
        connection.send_request(HttpRequest("GET", "https://elsewhere.test/x"))
        return records


def _answer(seen: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        sent = json.loads(request.content)
        return httpx.Response(200, json=[{"echoed": f"{row['a']}!"} for row in sent])
    return answer


def _connection(handler: Any, **overrides: Any) -> HttpConnection:
    record = {
        "name": "web", "provider": "http", "base_url": "https://api.example.test/v1/",
        "timeout_seconds": 5,
        "auth": SimpleNamespace(header="X-API-KEY", value="env.example_api_key"),
        **overrides,
    }
    ctx = SimpleNamespace(
        config_path="/installations/test/http_transform.yaml",
        env=[SimpleNamespace(name="example_api_key", env_var="EXAMPLE_API_KEY")],
    )
    return HttpConnection(
        SimpleNamespace(**{k: v for k, v in record.items() if v is not None}), ctx=ctx,
        transport=HttpXTransport(client=httpx.Client(transport=httpx.MockTransport(handler))),
    )


@pytest.fixture(autouse=True)
def _adapters(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("EXAMPLE_API_KEY", _SECRET)
    http_transform._discover_transform_adapters()
    before = dict(http_transform._ADAPTERS)
    http_transform_adapter("echo")(_Echo)
    http_transform_adapter("elsewhere")(_Elsewhere)
    yield
    http_transform._ADAPTERS.clear()
    http_transform._ADAPTERS.update(before)


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """The load path's connection lookup answers with a mocked HTTP connection."""
    seen: list[httpx.Request] = []
    connection = _connection(_answer(seen))
    monkeypatch.setattr(load_operation, "shared_connection", lambda _ctx, _name: connection)
    return seen


@pytest.fixture
def csv_file(tmp_path: Path) -> Path:
    held = tmp_path / "in.csv"
    held.write_text("a,b\n1,x\n2,y\n3,z\n", encoding="utf-8")
    return held


def _http(**values: Any) -> Transform:
    return Transform({"connection": "web", "adapter": "echo", **values}, selected="http")


class TestTheAdapterRegistry:
    def test_a_registered_adapter_is_found_by_name(self) -> None:
        assert isinstance(http_transform_adapter_for("echo"), _Echo)
        assert "echo" in http_transform_adapters()

    def test_an_unregistered_name_answers_none(self) -> None:
        assert http_transform_adapter_for("nobody") is None


class TestTheKindIsHidden:
    def test_it_is_not_offered(self) -> None:
        assert "http" not in Transform.kinds()
        assert "http" not in {kind.id for kind in TRANSFORM_KINDS}
        assert not {"connection", "adapter", "options"} & set(TRANSFORM_FIELDS)

    def test_it_is_in_force_only_when_selected(self) -> None:
        assert Transform({"connection": "web", "adapter": "echo"}).selected_kind() == "identity"
        assert _http().selected_kind() == "http"

    def test_connection_and_adapter_are_required(self) -> None:
        assert Transform(selected="http").validate() == ["connection", "adapter"]

    def test_an_incomplete_one_is_refused_at_resolve(self) -> None:
        with pytest.raises(ConfigError, match="missing: connection, adapter"):
            Transform(selected="http").resolve(SimpleNamespace())


class TestResolution:
    def test_it_builds_an_http_transform(self, wire: list[httpx.Request]) -> None:
        built = _http(options={"path": "echo"}).resolve(SimpleNamespace())

        assert isinstance(built, HTTPTransform)
        assert isinstance(built.adapter, _Echo)
        assert built.connection.name == "web"
        assert built.options == {"path": "echo"}
        assert wire == []

    def test_options_may_be_json_text(self, wire: list[httpx.Request]) -> None:
        assert _http(options='{"path": "p"}').resolve(SimpleNamespace()).options == {"path": "p"}

    def test_a_database_connection_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        database = Connection(SimpleNamespace(name="db", provider="postgres"))
        monkeypatch.setattr(load_operation, "shared_connection", lambda _ctx, _name: database)

        with pytest.raises(ConfigError, match="'web' is a postgres connection, not http"):
            _http().resolve(SimpleNamespace())

    def test_an_unregistered_adapter_is_refused(self, wire: list[httpx.Request]) -> None:
        with pytest.raises(ConfigError, match="no HTTP adapter is registered as 'nobody'.*echo"):
            _http(adapter="nobody").resolve(SimpleNamespace())


class TestTheTransform:
    def test_records_go_through_the_connection_and_come_back(self) -> None:
        seen: list[httpx.Request] = []
        built = HTTPTransform(_connection(_answer(seen)), _Echo(), {"path": "echo"})

        produced = built.transform([{"a": "1"}, {"a": "2"}])

        assert produced == [{"a": "1", "echoed": "1!"}, {"a": "2", "echoed": "2!"}]
        assert str(seen[0].url) == "https://api.example.test/v1/echo"
        assert seen[0].headers["X-API-KEY"] == _SECRET

    def test_the_schema_comes_from_the_produced_records(self) -> None:
        built = HTTPTransform(_connection(_answer([])), _Echo())

        schema = built.logical_schema([{"a": "1", "echoed": "1!"}])

        assert [name for name, _ in schema] == ["a", "echoed"]

    def test_output_columns_sends_nothing(self) -> None:
        seen: list[httpx.Request] = []
        built = HTTPTransform(_connection(_answer(seen)), _Echo())

        assert built.columns_for_names(["a", "b"]) == ["a", "b", "echoed"]
        assert seen == []


class TestThroughTheLoadPath:
    def test_preview_header_and_rows(self, wire: list[httpx.Request], csv_file: Path) -> None:
        found = preview_selected(
            SimpleNamespace(), Source({"file": str(csv_file)}), _http(), limit=10,
        )

        assert list(found.columns) == ["a", "b", "echoed"]
        assert [row["echoed"] for row in found.rows] == ["1!", "2!", "3!"]
        assert len(wire) == 1

    def test_a_load_hands_the_http_transform_to_the_boundary(
        self, wire: list[httpx.Request], csv_file: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The transfer boundary recorded instead of run, as tests/load/
        # test_execution.py does: a file source into a file target is not an
        # implemented shape, and a table target needs a database.
        reached: list[tuple[Any, ...]] = []
        monkeypatch.setattr(
            load_operation, "_load_one_file", lambda *args, **_kw: reached.append(args) or 3,
        )

        run_selected_load(
            SimpleNamespace(), None, Source({"file": str(csv_file)}), _http(),
            Target({"table": "s.t", "connection": "w"}),
        )

        (_source, transform, _target), = reached
        assert isinstance(transform, HTTPTransform)
        assert isinstance(transform.adapter, _Echo)
        assert wire == []

    def test_the_boundary_writes_the_adapters_records(
        self, wire: list[httpx.Request], csv_file: Path, tmp_path: Path,
    ) -> None:
        from rey_lib.files.data_file import data_file_for

        out = tmp_path / "out.csv"
        built = _http().resolve(SimpleNamespace())

        load_operation._load_one_file(
            data_file_for(csv_file), built, data_file_for(out),
            ctx=SimpleNamespace(app_name="test", log_depth=0), run_log=MagicMock(), movements=None,
            load_name="http-transform-test",
        )

        assert out.read_text(encoding="utf-8").splitlines() == [
            "a,b,echoed", "1,x,1!", "2,y,2!", "3,z,3!",
        ]
        assert len(wire) == 1

    def test_what_ran_is_recorded_without_a_secret(self) -> None:
        declared = _http(options={"path": "echo"}).executed_declaration()

        assert declared == {"connection": "web", "adapter": "echo", "options": {"path": "echo"}}
        assert _SECRET not in json.dumps(declared)


class TestErrorsPassThrough:
    def test_a_transport_failure(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(HttpTransportError):
            HTTPTransform(_connection(refuse), _Echo()).transform([{"a": "1"}])

    def test_an_origin_refusal(self) -> None:
        seen: list[httpx.Request] = []

        with pytest.raises(ConfigError, match="base_url origin"):
            HTTPTransform(_connection(_answer(seen)), _Elsewhere()).transform([{"a": "1"}])
        assert seen == []


class TestTheBoundary:
    def test_its_log_line_carries_no_values(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger="rey_lib.data.http_transform")

        HTTPTransform(_connection(_answer([])), _Echo()).transform([{"a": "value-secret"}])

        lines = [r.getMessage() for r in caplog.records if r.name == "rey_lib.data.http_transform"]
        assert lines == ["http transform: 1 record(s) through adapter _Echo on connection web"]

    def test_adapters_are_discovered_on_the_first_lookup_only(self) -> None:
        probe = (
            "import sys, rey_lib.data\n"
            "name = 'rey_lib.data.http_transform_adapters.openfigi'\n"
            "print(name in sys.modules)\n"
            "from rey_lib.data.http_transform import http_transform_adapter_for\n"
            "print(type(http_transform_adapter_for('openfigi')).__name__, name in sys.modules)\n"
        )
        found = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)

        assert found.stdout.split() == ["False", "OpenFigiAdapter", "True"]

    def test_rey_lib_data_does_not_import_the_http_layer(self) -> None:
        probe = "import sys, rey_lib.data; print('rey_lib.web_utils' in sys.modules)"
        found = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)

        assert found.stdout.strip() == "False"
