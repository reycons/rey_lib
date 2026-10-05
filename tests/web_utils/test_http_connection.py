"""An HTTP endpoint is a Rey connection over corehttp (backlogs 670, 665).

Declared in connections.yaml with ``provider: http``, resolved by name through
the same owner as a database connection, closed by the same shutdown. It builds
one corehttp client on first use with exactly two policies -- default headers
and the credential -- and the authentication value is a secret reference read
at each request.

Synthetic only: the HTTPX client corehttp builds is answered by
``httpx.MockTransport``. Nothing here reaches the network.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable

import httpx
import pytest
from corehttp.exceptions import ServiceRequestError, ServiceResponseError
from corehttp.rest import HttpRequest, HttpResponse
from corehttp.runtime import PipelineClient
from corehttp.runtime.policies import HeadersPolicy, ServiceKeyCredentialPolicy

import rey_lib.web_utils as web_utils
from rey_lib.db.connection import Connection, ConnectionOwner, shared_connection
from rey_lib.errors.error_utils import ConfigError, HttpTransportError
from rey_lib.web_utils import HttpConnection
from rey_lib.web_utils import connection as module

_SECRET = "synthetic-api-key"


def _record(**overrides: Any) -> SimpleNamespace:
    """One resolved ``provider: http`` connections[] record."""
    fields: dict[str, Any] = {
        "name": "example",
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


def _get(connection: HttpConnection, path: str, **params: str) -> HttpResponse:
    """Send one GET for ``path`` through the connection."""
    return connection.send_request(
        HttpRequest("GET", connection.format_url(path), params=params or None),
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    answer: Callable[[httpx.Request], httpx.Response],
) -> dict[str, Any]:
    """Answer every request with ``answer``; record what was built, and with what."""
    seen: dict[str, Any] = {"requests": [], "transports": [], "clients": []}
    real_transport = module._new_transport
    real_client = httpx.Client

    def transport(**options: Any) -> Any:
        seen["transports"].append(options)
        return real_transport(**options)

    def client(**options: Any) -> httpx.Client:
        built = real_client(transport=httpx.MockTransport(answer), **options)
        seen["clients"].append((options, built))
        return built

    monkeypatch.setattr(module, "_new_transport", transport)
    monkeypatch.setattr(httpx, "Client", client)
    return seen


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Echo every request; the status comes from the ``status`` query parameter."""
    monkeypatch.setenv("EXAMPLE_API_KEY", _SECRET)
    seen: dict[str, Any]

    def answer(request: httpx.Request) -> httpx.Response:
        seen["requests"].append(request)
        status = int(request.url.params.get("status", "200"))
        return httpx.Response(
            status, json={"echo": request.url.path}, headers={"Retry-After": "1"},
        )

    seen = _wire(monkeypatch, answer)
    return seen


class TestResolvedLikeAnyConnection:
    def test_the_owner_resolves_an_http_record_to_one_shared_object(self) -> None:
        owner = ConnectionOwner()
        ctx = _ctx(_record(), config_path="/installations/test/owner.yaml")

        first = owner.resolve(ctx, "example")

        assert isinstance(first, HttpConnection)
        assert owner.resolve(ctx, "example") is first

    def test_shared_connection_reaches_it(self) -> None:
        ctx = _ctx(_record(), config_path="/installations/test/shared.yaml")

        assert isinstance(shared_connection(ctx, "example"), HttpConnection)

    def test_a_database_record_is_still_a_database_connection(self) -> None:
        owner = ConnectionOwner()
        ctx = _ctx(SimpleNamespace(name="control", provider="postgres"))

        assert isinstance(owner.resolve(ctx, "control"), Connection)


class TestLifecycle:
    def test_nothing_is_built_before_first_use(self, wire: dict[str, Any]) -> None:
        connection = HttpConnection(_record(), ctx=_ctx())

        assert not connection.is_open
        assert wire["transports"] == [] and wire["clients"] == []

    def test_one_client_is_built_and_reused(self, wire: dict[str, Any]) -> None:
        connection = HttpConnection(_record(), ctx=_ctx())

        for path in ("a", "b", "c"):
            _get(connection, path)

        assert len(wire["transports"]) == 1
        assert len(wire["clients"]) == 1
        assert len(wire["requests"]) == 3

    def test_close_before_first_request_is_a_no_op(self, wire: dict[str, Any]) -> None:
        connection = HttpConnection(_record(), ctx=_ctx())

        connection.close()

        assert not connection.is_open
        assert wire["transports"] == []

    def test_close_is_idempotent_and_closes_the_wire_client(
        self, wire: dict[str, Any],
    ) -> None:
        connection = HttpConnection(_record(), ctx=_ctx())
        _get(connection, "x")

        connection.close()
        connection.close()

        assert not connection.is_open
        assert wire["clients"][0][1].is_closed

    def test_the_owner_closes_it(self, wire: dict[str, Any]) -> None:
        owner = ConnectionOwner()
        connection = owner.resolve(
            _ctx(_record(), config_path="/installations/test/close.yaml"), "example",
        )
        _get(connection, "x")

        owner.close()

        assert not connection.is_open
        assert wire["clients"][0][1].is_closed

    def test_a_send_after_close_builds_a_fresh_client(self, wire: dict[str, Any]) -> None:
        connection = HttpConnection(_record(), ctx=_ctx())
        _get(connection, "x")
        connection.close()

        _get(connection, "y")

        assert len(wire["transports"]) == 2


class TestConfiguration:
    def test_a_relative_path_is_joined_onto_the_base_url(self, wire: dict[str, Any]) -> None:
        connection = HttpConnection(_record(), ctx=_ctx())

        assert connection.format_url("mapping") == "https://api.example.test/v3/mapping"
        assert connection.format_url("https://other.test/x") == "https://other.test/x"

    def test_format_url_sends_nothing_and_builds_no_wire_client(
        self, wire: dict[str, Any],
    ) -> None:
        HttpConnection(_record(), ctx=_ctx()).format_url("mapping")

        assert wire["clients"] == [] and wire["requests"] == []

    def test_the_settings_reach_the_transport(self, wire: dict[str, Any]) -> None:
        connection = HttpConnection(_record(verify=False), ctx=_ctx())

        _get(connection, "mapping")

        options = wire["transports"][0]
        assert options["connection_timeout"] == 30.0
        assert options["read_timeout"] == 30.0
        assert options["connection_verify"] is False
        assert options["use_env_settings"] is False
        built, _client = wire["clients"][0]
        assert built["verify"] is False
        assert built["trust_env"] is False
        sent = wire["requests"][0]
        assert str(sent.url) == "https://api.example.test/v3/mapping"
        assert sent.headers["Accept"] == "application/json"
        assert sent.extensions["timeout"]["connect"] == 30.0
        assert sent.extensions["timeout"]["read"] == 30.0

    def test_tls_verification_is_on_by_default(self, wire: dict[str, Any]) -> None:
        _get(HttpConnection(_record(), ctx=_ctx()), "x")

        assert wire["transports"][0]["connection_verify"] is True

    def test_an_environment_proxy_is_not_inherited(
        self, wire: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")

        response = _get(HttpConnection(_record(), ctx=_ctx()), "x")

        assert response.status_code == 200
        assert len(wire["requests"]) == 1

    @pytest.mark.parametrize("missing", ["base_url", "timeout_seconds"])
    def test_a_record_missing_a_required_setting_is_refused(self, missing: str) -> None:
        with pytest.raises(ConfigError, match=missing):
            HttpConnection(_record(**{missing: None}))

    def test_auth_without_a_value_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="auth"):
            HttpConnection(_record(auth=SimpleNamespace(header="X-API-KEY")))


class TestPolicies:
    @staticmethod
    def _policies(connection: HttpConnection) -> list[type]:
        client = connection._open()
        return [
            type(getattr(node, "_policy", node))
            for node in client.pipeline._impl_policies
            if type(node).__name__ != "_TransportRunner"
        ]

    def test_exactly_headers_and_credential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EXAMPLE_API_KEY", _SECRET)
        connection = HttpConnection(_record(), ctx=_ctx())

        assert self._policies(connection) == [HeadersPolicy, ServiceKeyCredentialPolicy]

    def test_only_headers_when_no_auth_is_declared(self) -> None:
        connection = HttpConnection(_record(auth=None), ctx=_ctx())

        assert self._policies(connection) == [HeadersPolicy]

    def test_a_retryable_status_is_sent_once(self, wire: dict[str, Any]) -> None:
        response = _get(HttpConnection(_record(), ctx=_ctx()), "x", status="503")

        assert response.status_code == 503
        assert len(wire["requests"]) == 1

    def test_no_corehttp_user_agent_is_sent(self, wire: dict[str, Any]) -> None:
        _get(HttpConnection(_record(), ctx=_ctx()), "x")

        assert "python-core" not in wire["requests"][0].headers.get("User-Agent", "")


class TestTheSecret:
    def test_the_auth_header_carries_the_resolved_value(self, wire: dict[str, Any]) -> None:
        _get(HttpConnection(_record(), ctx=_ctx()), "x")

        assert wire["requests"][0].headers["X-API-KEY"] == _SECRET

    def test_the_value_is_resolved_at_each_request(
        self, wire: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        connection = HttpConnection(_record(), ctx=_ctx())
        _get(connection, "x")

        monkeypatch.setenv("EXAMPLE_API_KEY", "rotated-key")
        _get(connection, "y")

        assert wire["requests"][1].headers["X-API-KEY"] == "rotated-key"

    def test_the_value_is_held_nowhere(self, wire: dict[str, Any]) -> None:
        record = _record()
        connection = HttpConnection(record, ctx=_ctx())
        _get(connection, "x")

        held = {k: v for k, v in vars(connection).items() if k != "_client"}
        assert _SECRET not in repr(held)
        assert _SECRET not in repr(connection)
        assert record.auth.value == "env.example_api_key"
        built, client = wire["clients"][0]
        assert _SECRET not in repr(built)
        assert _SECRET not in repr(dict(client.headers))
        credential = module._EnvKeyCredential(_ctx(), "env.example_api_key")
        assert _SECRET not in repr(credential)
        assert _SECRET not in repr(vars(credential))


class TestResponsesAndFailures:
    @pytest.mark.parametrize("status", [200, 404, 500])
    def test_a_received_status_is_returned_not_raised(
        self, status: int, wire: dict[str, Any],
    ) -> None:
        response = _get(HttpConnection(_record(), ctx=_ctx()), "x", status=str(status))

        assert isinstance(response, HttpResponse)
        assert response.status_code == status
        assert response.json() == {"echo": "/v3/x"}
        assert response.headers["retry-after"] == response.headers["Retry-After"] == "1"

    @pytest.mark.parametrize("failure, cause", [
        (httpx.ConnectError, ServiceRequestError),
        (httpx.ReadTimeout, ServiceResponseError),
    ])
    def test_a_transport_failure_raises_http_transport_error(
        self,
        failure: type[httpx.HTTPError],
        cause: type[Exception],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("EXAMPLE_API_KEY", _SECRET)

        def refuse(request: httpx.Request) -> httpx.Response:
            raise failure("refused", request=request)

        _wire(monkeypatch, refuse)

        with pytest.raises(HttpTransportError, match="was not answered") as raised:
            _get(HttpConnection(_record(), ctx=_ctx()), "x")
        assert isinstance(raised.value.__cause__, cause)


class TestThePublicSurface:
    def test_only_the_connection_is_exported(self) -> None:
        assert web_utils.__all__ == ["HttpConnection"]
        assert not hasattr(module, "HttpResponse") or module.HttpResponse is HttpResponse

    def test_no_public_member_is_an_httpx_or_client_object(
        self, wire: dict[str, Any],
    ) -> None:
        connection = HttpConnection(_record(), ctx=_ctx())
        _get(connection, "x")

        assert not hasattr(connection, "client")
        assert not hasattr(connection, "request")
        for name in dir(connection):
            if name.startswith("_"):
                continue
            value = getattr(connection, name)
            if callable(value):
                continue
            assert not type(value).__module__.startswith("httpx")
            assert not isinstance(value, PipelineClient)
