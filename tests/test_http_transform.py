"""Records through a governed HTTP operation and back: HTTPTransform (backlogs 671, 813).

Asserted: the http kind is offered under the load command's parameter names;
resolution reads the job through Control.http_operation and hydrates it, with
no connection resolved by name; the transform, its schema, preview and a load
carry the operation's records; output_columns sends nothing; what ran is
recorded without a secret; errors pass through; the mapping is applied before
sending, once.

Synthetic only: one neutral job row, answered by httpx.MockTransport through
the HttpConnection the operation builds from the job.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import MagicMock

import httpx
import pytest
from corehttp.transport.httpx import HttpXTransport

from rey_lib import web_utils
from rey_lib.data.http_transform import HTTPTransform
from rey_lib.errors.error_utils import ConfigError, HttpTransportError
from rey_lib.load import Source, Target, Transform, run_selected_load
from rey_lib.load import load_operation
from rey_lib.load.execution import preview_selected
from rey_lib.load.transform import TRANSFORM_FIELDS, TRANSFORM_PARAMETERS
from rey_lib.web_utils.connection import HttpConnection
from rey_lib.web_utils.operation import HttpOperation

_SECRET = "synthetic-api-key"


def _job(**overrides: Any) -> dict[str, Any]:
    """One neutral job row, as f_http_operation_get shapes it: POST each batch to echo."""
    job: dict[str, Any] = {
        "installation_key": "test", "adapter_key": "echo", "connection_name": "web",
        "base_url": "https://api.example.test/v1/", "auth_header": "X-API-KEY",
        "auth_prefix": None, "auth_secret_ref": "env.example_api_key", "auth_required": True,
        "timeout_seconds": 5, "verify": True, "default_headers": None,
        "http_method": "POST", "path": "echo", "request_format": "json",
        "response_format": "json", "is_enabled": True,
        "request_body_fields": ["a"], "request_body_kinds": ["reference"],
        "request_body_values": ["a"],
        "response_field_names": ["echoed"], "response_field_paths": ["$.echoed"],
        "batch_size": 10, "correlation_mode": "position",
    }
    job.update(overrides)
    return job


def _answer(seen: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    """Answers each body element with {"echoed": "<a>!"}, in order."""
    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        sent = json.loads(request.content)
        return httpx.Response(200, json=[{"echoed": f"{row['a']}!"} for row in sent])
    return answer


def _ctx(job: dict[str, Any] | None = None) -> SimpleNamespace:
    """A runtime context: the installation, its control answering the job, the secret's env."""
    jobs = [job if job is not None else _job()]
    control = SimpleNamespace(
        http_operation=lambda installation, adapter_key=None, required=True: [
            one for one in jobs if adapter_key in (None, one["adapter_key"])
        ],
    )
    return SimpleNamespace(
        installation=SimpleNamespace(name="test"), shared_control=control,
        config_path="/installations/test/http_transform.yaml",
        env=[SimpleNamespace(name="example_api_key", env_var="EXAMPLE_API_KEY")],
    )


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXAMPLE_API_KEY", _SECRET)


def _transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    """The job's connection is built as HttpOperation builds it, over a mocked transport."""
    monkeypatch.setattr(web_utils, "HttpConnection", lambda config, ctx: HttpConnection(
        config, ctx, transport=HttpXTransport(client=httpx.Client(transport=httpx.MockTransport(handler))),
    ))


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Every request the job's connection sends."""
    seen: list[httpx.Request] = []
    _transport(monkeypatch, _answer(seen))
    return seen


@pytest.fixture
def csv_file(tmp_path: Path) -> Path:
    held = tmp_path / "in.csv"
    held.write_text("a,b\n1,x\n2,y\n3,z\n", encoding="utf-8")
    return held


def _http(adapter: str = "echo") -> Transform:
    """An http Transform, through the load command's parameter names (backlogs 668, 813)."""
    return Transform({"http-adapter": adapter}, selected="http")


def _operation(**overrides: Any) -> HttpOperation:
    return HttpOperation.hydrate(_job(**overrides))


class TestTheKindIsOffered:
    """Offered since 668, under the load command's parameter names."""

    def test_it_is_offered_with_its_parameters(self) -> None:
        assert "http" in Transform.kinds()
        assert {"http-adapter", "http-transform"} <= set(TRANSFORM_FIELDS)
        assert {"http-adapter", "http-transform"} <= set(TRANSFORM_PARAMETERS)
        assert not {"http-connection", "http-options"} & set(TRANSFORM_FIELDS)

    def test_its_value_selects_it(self) -> None:
        assert Transform({"http-adapter": "echo"}).selected_kind() == "http"

    def test_the_operation_is_required(self) -> None:
        assert Transform(selected="http").validate() == ["http-adapter"]

    def test_an_incomplete_one_is_refused_at_resolve(self) -> None:
        with pytest.raises(ConfigError, match="missing: http-adapter"):
            Transform(selected="http").resolve(_ctx())


class TestOutbound:
    """The execution-effect fact a preview gate reads (663 invariant 4)."""

    def test_an_http_transform_is_outbound_through_its_operation(self) -> None:
        assert _http().outbound(50) == {"operation": "echo", "max_preview_records": 50}

    def test_it_is_stated_without_resolving_anything(self, wire: list[httpx.Request]) -> None:
        _http(adapter="not-configured").outbound(50)

        assert wire == []

    @pytest.mark.parametrize("transform", [
        Transform(),
        Transform({"transform": '{"columns": [{"source": "a", "name": "a"}]}'}),
        Transform({"transform-file": "/t.yaml"}),
    ])
    def test_a_local_kind_is_not_outbound(self, transform: Transform) -> None:
        assert transform.outbound(50) is None


class TestResolution:
    def test_it_builds_an_http_transform_from_the_governed_job(
        self, wire: list[httpx.Request],
    ) -> None:
        built = _http().resolve(_ctx())

        assert isinstance(built, HTTPTransform)
        assert built.operation.adapter_key == "echo"
        assert built.operation.base_url == "https://api.example.test/v1/"
        assert wire == []

    def test_no_connection_is_resolved_by_name(
        self, wire: list[httpx.Request], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            load_operation, "shared_connection",
            lambda *_a: (_ for _ in ()).throw(AssertionError("a connection was looked up")),
        )

        assert isinstance(_http().resolve(_ctx()), HTTPTransform)

    def test_an_operation_the_installation_does_not_have_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="installation 'test' has no HTTP operation 'nobody'"):
            _http(adapter="nobody").resolve(_ctx())

    def test_a_runtime_without_control_is_refused(self) -> None:
        ctx = SimpleNamespace(installation=SimpleNamespace(name="test"), shared_control=None,
                              control=SimpleNamespace(procedure_map=None))

        with pytest.raises(ConfigError, match="control database"):
            _http().resolve(ctx)

    def test_a_disabled_operation_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="it is disabled"):
            _http().resolve(_ctx(_job(is_enabled=False)))


class TestTheTransform:
    def test_records_go_through_the_job_connection_and_come_back(
        self, wire: list[httpx.Request],
    ) -> None:
        built = HTTPTransform(_operation(), _ctx())

        produced = built.transform([{"a": "1"}, {"a": "2"}])

        assert produced == [
            {"a": "1", "echoed": "1!", "operation_status": "ok", "operation_message": None},
            {"a": "2", "echoed": "2!", "operation_status": "ok", "operation_message": None},
        ]
        assert str(wire[0].url) == "https://api.example.test/v1/echo"
        assert wire[0].headers["X-API-KEY"] == _SECRET

    def test_the_schema_comes_from_the_produced_records(self) -> None:
        built = HTTPTransform(_operation(), _ctx())

        schema = built.logical_schema([{"a": "1", "echoed": "1!"}])

        assert [name for name, _ in schema] == ["a", "echoed"]

    def test_output_columns_sends_nothing(self, wire: list[httpx.Request]) -> None:
        built = HTTPTransform(_operation(), _ctx())

        assert built.columns_for_names(["a", "b"]) == [
            "a", "b", "echoed", "operation_status", "operation_message",
        ]
        assert wire == []


class TestThroughTheLoadPath:
    def test_preview_header_and_rows(self, wire: list[httpx.Request], csv_file: Path) -> None:
        found = preview_selected(_ctx(), Source({"file": str(csv_file)}), _http(), limit=10)

        assert list(found.columns) == ["a", "b", "echoed", "operation_status", "operation_message"]
        assert [row["echoed"] for row in found.rows] == ["1!", "2!", "3!"]
        assert len(wire) == 1

    def test_the_held_transform_previews_what_it_shows(
        self, wire: list[httpx.Request], csv_file: Path,
    ) -> None:
        # ONE PATH (backlog 681): the shown mapping, and an edit to it, reach
        # the operation on the next preview.
        held = _http()
        held.observe_source_columns(["a", "b"])
        held.edit_column(1, "name", "b_out")

        first = preview_selected(_ctx(), Source({"file": str(csv_file)}), held, limit=10)

        assert list(first.columns) == ["a", "b_out", "echoed", "operation_status", "operation_message"]
        assert first.rows[0] == {
            "a": "1", "b_out": "x", "echoed": "1!", "operation_status": "ok", "operation_message": None,
        }

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
            _ctx(), None, Source({"file": str(csv_file)}), _http(),
            Target({"table": "s.t", "connection": "w"}),
        )

        (_source, transform, _target), = reached
        assert isinstance(transform, HTTPTransform)
        assert transform.operation.adapter_key == "echo"
        assert wire == []

    def test_the_boundary_writes_the_operations_records(
        self, wire: list[httpx.Request], csv_file: Path, tmp_path: Path,
    ) -> None:
        from rey_lib.files.data_file import data_file_for

        out = tmp_path / "out.csv"
        built = _http().resolve(_ctx())

        load_operation._load_one_file(
            data_file_for(csv_file), built, data_file_for(out),
            ctx=SimpleNamespace(app_name="test"), run_log=MagicMock(), movements=None,
            load_name="http-transform-test",
        )

        assert out.read_text(encoding="utf-8").splitlines() == [
            "a,b,echoed,operation_status,operation_message",
            "1,x,1!,ok,", "2,y,2!,ok,", "3,z,3!,ok,",
        ]
        assert len(wire) == 1

    def test_what_ran_is_recorded_without_a_secret(self) -> None:
        declared = _http().executed_declaration()

        assert declared == {"adapter": "echo", "declaration": None, "request": None}
        assert _SECRET not in json.dumps(declared)


class TestErrorsPassThrough:
    def test_a_transport_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)
        _transport(monkeypatch, refuse)

        with pytest.raises(HttpTransportError):
            HTTPTransform(_operation(), _ctx()).transform([{"a": "1"}])

    def test_an_origin_refusal(self, wire: list[httpx.Request]) -> None:
        # An authenticated job sends only to its base URL's origin.
        operation = _operation(path="https://elsewhere.test/x")

        with pytest.raises(ConfigError, match="base_url origin"):
            HTTPTransform(operation, _ctx()).transform([{"a": "1"}])
        assert wire == []


class TestTheBoundary:
    def test_its_log_line_carries_no_values(
        self, wire: list[httpx.Request], caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="rey_lib.data.http_transform")

        HTTPTransform(_operation(), _ctx()).transform([{"a": "value-secret"}])

        lines = [r.getMessage() for r in caplog.records if r.name == "rey_lib.data.http_transform"]
        assert lines == ["http transform: 1 record(s) through operation echo"]

    def test_rey_lib_data_does_not_import_the_http_layer(self) -> None:
        probe = "import sys, rey_lib.data; print('rey_lib.web_utils' in sys.modules)"
        found = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)

        assert found.stdout.strip() == "False"


_MAPPING = {"columns": [
    {"source": "cusip", "name": "a"},
    {"source": "note", "name": "note", "export": False},
    {"source": "asset_id", "name": "asset_id"},
]}


class TestTheMappingBeforeSending:
    """http-transform: the existing ColumnTransform, applied before the operation (backlog 679)."""

    @staticmethod
    def _built(mapping: Any = _MAPPING) -> HTTPTransform:
        values = {"http-adapter": "echo"}
        if mapping is not None:
            values["http-transform"] = json.dumps(mapping)
        return Transform(values, selected="http").resolve(_ctx())

    def test_records_are_mapped_before_the_operation_sees_them(
        self, wire: list[httpx.Request],
    ) -> None:
        produced = self._built().transform([{"cusip": "X1", "note": "n", "asset_id": "7"}])

        assert json.loads(wire[0].content) == [{"a": "X1"}]
        assert produced == [{"a": "X1", "asset_id": "7", "echoed": "X1!",
                             "operation_status": "ok", "operation_message": None}]

    def test_a_dropped_column_never_reaches_the_result(self, wire: list[httpx.Request]) -> None:
        produced = self._built().transform([{"cusip": "X1", "note": "n", "asset_id": "7"}])

        assert "note" not in produced[0]

    def test_mapping_happens_exactly_once(
        self, wire: list[httpx.Request], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from rey_lib.data.column_transform import ColumnTransform

        calls: list[int] = []
        real = ColumnTransform.transform
        monkeypatch.setattr(
            ColumnTransform, "transform",
            lambda self, records: calls.append(len(records)) or real(self, records),
        )

        self._built().transform([{"cusip": "X1", "note": "n", "asset_id": "7"}])

        assert calls == [1]

    def test_preview_columns_are_the_mapped_columns_and_the_operations(
        self, wire: list[httpx.Request],
    ) -> None:
        assert self._built().columns_for_names(["cusip", "note", "asset_id"]) == [
            "a", "asset_id", "echoed", "operation_status", "operation_message",
        ]
        assert wire == []

    def test_no_declaration_is_a_genuine_pass_through(self, wire: list[httpx.Request]) -> None:
        built = self._built(mapping=None)

        produced = built.transform([{"a": "1", "note": "n"}])

        assert built.mapping is None
        assert produced == [{"a": "1", "note": "n", "echoed": "1!",
                             "operation_status": "ok", "operation_message": None}]

    def test_the_executed_declaration_records_the_authored_mapping(self) -> None:
        declared = Transform(
            {"http-adapter": "echo", "http-transform": json.dumps(_MAPPING)}, selected="http",
        ).executed_declaration()

        assert declared["declaration"]["columns"][0] == {"source": "cusip", "name": "a"}

    def test_the_shown_mapping_is_the_one_executed(self) -> None:
        # WHAT IS SHOWN IS WHAT EXECUTES (backlog 681): nothing is written to
        # the field, and the mapping the grid shows is applied before sending.
        held = Transform({"http-adapter": "echo"}, selected="http")
        held.observe_source_columns(["cusip", "asset_id"])

        assert held.value("http-transform") is None
        assert held.executed_declaration()["declaration"] == {"columns": held.columns()}

    def test_nothing_shown_and_nothing_authored_maps_nothing(self) -> None:
        held = Transform({"http-adapter": "echo"}, selected="http")

        assert held.executed_declaration()["declaration"] is None

    def test_it_is_authored_like_a_declaration(self) -> None:
        held = Transform({"http-adapter": "echo"}, selected="http")
        held.observe_source_columns(["cusip", "asset_id"])

        held.edit_column(0, "name", "lookup_id")

        assert json.loads(held.value("http-transform"))["columns"][0]["name"] == "lookup_id"
        assert held.executed_declaration()["declaration"]["columns"][0]["name"] == "lookup_id"
