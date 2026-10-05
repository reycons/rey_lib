"""An HTTP endpoint is a Rey connection (backlog 670).

Declared in connections.yaml with ``provider: http``, resolved by name through
the same owner as a database connection, closed by the same shutdown. Its
client is built on the first request and never leaves the module; the
authentication value is a secret reference read as the client is built.

Synthetic only: every request is answered by ``httpx.MockTransport``. Nothing
here reaches the network.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import rey_lib.web_utils as web_utils
from rey_lib.db.connection import Connection, ConnectionOwner, shared_connection
from rey_lib.errors.error_utils import ConfigError, HttpTransportError
from rey_lib.web_utils import HttpConnection, HttpResponse
from rey_lib.web_utils import connection as module

_SECRET = "synthetic-api-key"


def _record(**overrides: Any) -> SimpleNamespace:
    """One resolved ``provider: http`` connections[] record."""
    fields: dict[str, Any] = {
        "name": "openfigi",
        "provider": "http",
        "base_url": "https://api.example.test/v3/",
        "auth": SimpleNamespace(header="X-API-KEY", value="env.example_api_key"),
        "headers": SimpleNamespace(Accept="application/json"),
        "timeout_seconds": 30,
    }
    fields.update(overrides)
    return SimpleNamespace(**{k: v for k, v in fields.items() if v is not None})


def _ctx(*records: Any, config_path: str = "/installations/test/http.yaml") -> SimpleNamespace:
    """A context carrying connections and the env declaration the auth names."""
    return SimpleNamespace(
        config_path=config_path,
        connections=list(records),
        env=[SimpleNamespace(name="example_api_key", env_var="EXAMPLE_API_KEY")],
    )


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Answer every request synthetically, and record what the client was built with."""
    seen: dict[str, Any] = {"requests": [], "built": []}
    monkeypatch.setenv("EXAMPLE_API_KEY", _SECRET)

    def answer(request: httpx.Request) -> httpx.Response:
        seen["requests"].append(request)
        status = int(request.url.params.get("status", "200"))
        return httpx.Response(status, json={"echo": request.url.path})

    real = module._new_client

    def built(**options: Any) -> httpx.Client:
        seen["built"].append(options)
        return real(transport=httpx.MockTransport(answer), **options)

    monkeypatch.setattr(module, "_new_client", built)
    return seen


class TestResolvedLikeAnyConnection:
    def test_the_owner_resolves_an_http_record_to_one_shared_object(self) -> None:
        owner = ConnectionOwner()
        ctx = _ctx(_record(), config_path="/installations/test/owner.yaml")

        first = owner.resolve(ctx, "openfigi")

        assert isinstance(first, HttpConnection)
        assert owner.resolve(ctx, "openfigi") is first

    def test_shared_connection_reaches_it(self) -> None:
        ctx = _ctx(_record(), config_path="/installations/test/shared.yaml")

        assert isinstance(shared_connection(ctx, "openfigi"), HttpConnection)

    def test_a_database_record_is_still_a_database_connection(self) -> None:
        owner = ConnectionOwner()
        ctx = _ctx(SimpleNamespace(name="control", provider="postgres"))

        assert isinstance(owner.resolve(ctx, "control"), Connection)


class TestConfiguration:
    def test_nothing_opens_until_the_first_request(self, transport: dict[str, Any]) -> None:
        connection = HttpConnection(_record(), ctx=_ctx())

        assert not connection.is_open
        assert transport["built"] == []
        connection.request("GET", "mapping")
        assert connection.is_open
        assert len(transport["built"]) == 1

    def test_the_client_is_built_once_and_reused(self, transport: dict[str, Any]) -> None:
        connection = HttpConnection(_record(), ctx=_ctx())

        connection.request("GET", "a")
        connection.request("GET", "b")

        assert len(transport["built"]) == 1

    def test_the_settings_reach_the_client(self, transport: dict[str, Any]) -> None:
        connection = HttpConnection(_record(verify=False), ctx=_ctx())

        connection.request("POST", "mapping", json=[{"idType": "ID_CUSIP"}])

        options = transport["built"][0]
        assert options["base_url"] == "https://api.example.test/v3/"
        assert options["timeout"] == 30.0
        assert options["verify"] is False
        sent = transport["requests"][0]
        assert str(sent.url) == "https://api.example.test/v3/mapping"
        assert sent.headers["Accept"] == "application/json"

    def test_tls_verification_is_on_by_default(self, transport: dict[str, Any]) -> None:
        HttpConnection(_record(), ctx=_ctx()).request("GET", "x")

        assert transport["built"][0]["verify"] is True

    @pytest.mark.parametrize("missing", ["base_url", "timeout_seconds"])
    def test_a_record_missing_a_required_setting_is_refused(self, missing: str) -> None:
        with pytest.raises(ConfigError, match=missing):
            HttpConnection(_record(**{missing: None}))

    def test_auth_without_a_value_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="auth"):
            HttpConnection(_record(auth=SimpleNamespace(header="X-API-KEY")))


class TestTheSecret:
    def test_the_auth_header_carries_the_resolved_value(self, transport: dict[str, Any]) -> None:
        HttpConnection(_record(), ctx=_ctx()).request("GET", "x")

        assert transport["requests"][0].headers["X-API-KEY"] == _SECRET

    def test_the_value_is_in_no_field_of_the_object_or_its_config(
        self, transport: dict[str, Any],
    ) -> None:
        record = _record()
        connection = HttpConnection(record, ctx=_ctx())
        connection.request("GET", "x")

        assert _SECRET not in repr(vars(connection).get("_config"))
        held = {k: v for k, v in vars(connection).items() if k != "_client"}
        assert _SECRET not in repr(held)
        assert record.auth.value == "env.example_api_key"


class TestThePublicSurface:
    def test_only_the_rey_objects_are_exported(self) -> None:
        assert sorted(web_utils.__all__) == ["HttpConnection", "HttpResponse"]

    def test_no_public_member_is_an_httpx_object(self, transport: dict[str, Any]) -> None:
        connection = HttpConnection(_record(), ctx=_ctx())
        response = connection.request("GET", "x")

        assert isinstance(response, HttpResponse)
        assert not hasattr(connection, "client")
        for name in dir(connection):
            if not name.startswith("_") and not callable(getattr(connection, name)):
                assert not type(getattr(connection, name)).__module__.startswith("httpx")


class TestResponsesAndFailures:
    @pytest.mark.parametrize("status", [404, 500])
    def test_a_received_error_status_is_returned_not_raised(
        self, status: int, transport: dict[str, Any],
    ) -> None:
        response = HttpConnection(_record(), ctx=_ctx()).request(
            "GET", "x", params={"status": str(status)},
        )

        assert response.status == status
        assert response.json() == {"echo": "/v3/x"}

    def test_a_transport_failure_raises_http_transport_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("EXAMPLE_API_KEY", _SECRET)

        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        real = module._new_client
        monkeypatch.setattr(
            module, "_new_client",
            lambda **options: real(transport=httpx.MockTransport(refuse), **options),
        )

        with pytest.raises(HttpTransportError, match="was not answered"):
            HttpConnection(_record(), ctx=_ctx()).request("GET", "x")


class TestClose:
    def test_close_is_idempotent(self, transport: dict[str, Any]) -> None:
        connection = HttpConnection(_record(), ctx=_ctx())
        connection.request("GET", "x")

        connection.close()
        connection.close()

        assert not connection.is_open

    def test_the_owner_closes_it(self, transport: dict[str, Any]) -> None:
        owner = ConnectionOwner()
        connection = owner.resolve(
            _ctx(_record(), config_path="/installations/test/close.yaml"), "openfigi",
        )
        connection.request("GET", "x")

        owner.close()

        assert not connection.is_open
