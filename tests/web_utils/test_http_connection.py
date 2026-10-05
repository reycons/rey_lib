"""An HTTP endpoint is a Rey connection over corehttp (backlogs 670, 665, 666).

Declared in connections.yaml with ``provider: http``, resolved by name through
the same owner as a database connection, closed by the same shutdown. It builds
one corehttp client on first use with exactly two policies -- default headers
and the credential -- keeps no cookies, and never sends its credential outside
the HTTPS origin its base URL declares.

Synthetic only; nothing here reaches the network. Two kinds of test:
app-style tests answer requests through the public ``transport=`` boundary;
tests of the transport this module BUILDS replace its private HTTPX client
construction with one answered by ``httpx.MockTransport``.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Callable

import httpx
import pytest
from corehttp.exceptions import ServiceRequestError, ServiceResponseError
from corehttp.rest import HttpRequest as CoreHttpRequest
from corehttp.rest import HttpResponse as CoreHttpResponse
from corehttp.runtime import PipelineClient
from corehttp.runtime.policies import HeadersPolicy, ServiceKeyCredentialPolicy
from corehttp.transport.httpx import HttpXTransport

import rey_lib.web_utils as web_utils
from rey_lib.db.connection import Connection, ConnectionOwner, shared_connection
from rey_lib.errors.error_utils import ConfigError, HttpTransportError
from rey_lib.web_utils import HttpConnection, HttpRequest, HttpResponse
from rey_lib.web_utils import connection as module

_SECRET = "synthetic-api-key"
_LOGGER = "rey_lib.web_utils.connection"

Handler = Callable[[httpx.Request], httpx.Response]


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


def _echo(seen: list[httpx.Request]) -> Handler:
    """Answer with the path; status from ?status=, redirects for /hop paths."""
    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/setcookie"):
            return httpx.Response(200, headers={"Set-Cookie": "sid=abc; Path=/"})
        if path.endswith("/hop"):
            return httpx.Response(302, headers={"Location": "https://api.example.test/v3/landed"})
        if path.endswith("/big"):
            return httpx.Response(200, content=b"x" * 100_000)
        status = int(request.url.params.get("status", "200"))
        return httpx.Response(
            status, json={"echo": path}, headers={"Retry-After": "1"},
        )
    return answer


def _supplied(handler: Handler) -> HttpXTransport:
    """An app-style transport: public corehttp and HTTPX APIs only."""
    return HttpXTransport(client=httpx.Client(transport=httpx.MockTransport(handler)))


def _get(connection: HttpConnection, path: str, **kwargs: Any) -> HttpResponse:
    """Send one GET for ``path`` through the connection."""
    params = kwargs.pop("params", None)
    return connection.send_request(
        HttpRequest("GET", connection.format_url(path), params=params), **kwargs,
    )


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXAMPLE_API_KEY", _SECRET)


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The transport this module BUILDS, its HTTPX client answered by a mock."""
    seen: dict[str, Any] = {"requests": [], "clients": []}
    real = module._new_wire_client

    def built(**options: Any) -> httpx.Client:
        client = real(transport=httpx.MockTransport(_echo(seen["requests"])), **options)
        seen["clients"].append((options, client))
        return client

    monkeypatch.setattr(module, "_new_wire_client", built)
    return seen


@pytest.fixture
def app() -> tuple[list[httpx.Request], Callable[..., HttpConnection]]:
    """Connections answered through the public transport= boundary."""
    seen: list[httpx.Request] = []

    def connect(**overrides: Any) -> HttpConnection:
        return HttpConnection(
            _record(**overrides), ctx=_ctx(), transport=_supplied(_echo(seen)),
        )
    return seen, connect


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


class TestTheRecordContract:
    @pytest.mark.parametrize("missing", ["base_url", "timeout_seconds"])
    def test_a_record_missing_a_required_setting_is_refused(self, missing: str) -> None:
        with pytest.raises(ConfigError, match=missing):
            HttpConnection(_record(**{missing: None}))

    def test_auth_without_a_value_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="auth"):
            HttpConnection(_record(auth=SimpleNamespace(header="X-API-KEY")))

    def test_auth_on_a_plain_http_base_url_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="only over https"):
            HttpConnection(_record(base_url="http://api.example.test/v3/"))

    def test_the_owner_refuses_it_before_any_request(self) -> None:
        ctx = _ctx(
            _record(base_url="http://api.example.test/"),
            config_path="/installations/test/plain.yaml",
        )

        with pytest.raises(ConfigError, match="only over https"):
            ConnectionOwner().resolve(ctx, "example")

    def test_plain_http_without_auth_is_allowed(self) -> None:
        HttpConnection(_record(base_url="http://api.example.test/", auth=None))

    def test_an_empty_prefix_is_refused(self) -> None:
        auth = SimpleNamespace(header="Authorization", value="env.example_api_key", prefix=" ")

        with pytest.raises(ConfigError, match="prefix"):
            HttpConnection(_record(auth=auth))

    def test_a_missing_ca_bundle_is_refused(self, tmp_path: Any) -> None:
        with pytest.raises(ConfigError, match="CA bundle"):
            HttpConnection(_record(verify=str(tmp_path / "absent.pem")))

    def test_a_present_ca_bundle_reaches_the_client(
        self, tmp_path: Any, wire: dict[str, Any],
    ) -> None:
        bundle = tmp_path / "ca.pem"
        bundle.write_text("synthetic")

        connection = HttpConnection(_record(verify=str(bundle)), ctx=_ctx())
        connection.format_url("x")

        assert wire["clients"][0][0]["verify"] == str(bundle)

    @pytest.mark.parametrize("verify", [1, "  "])
    def test_a_malformed_verify_is_refused(self, verify: Any) -> None:
        with pytest.raises(ConfigError, match="verify"):
            HttpConnection(_record(verify=verify))


class TestTheBuiltTransport:
    def test_nothing_is_built_before_first_use(self, wire: dict[str, Any]) -> None:
        connection = HttpConnection(_record(), ctx=_ctx())

        assert not connection.is_open
        assert wire["clients"] == []

    def test_one_client_is_built_and_reused(self, wire: dict[str, Any]) -> None:
        connection = HttpConnection(_record(), ctx=_ctx())

        for path in ("a", "b", "c"):
            _get(connection, path)

        assert len(wire["clients"]) == 1
        assert len(wire["requests"]) == 3

    def test_its_settings(self, wire: dict[str, Any]) -> None:
        connection = HttpConnection(_record(verify=False), ctx=_ctx())

        _get(connection, "mapping")

        options, _client = wire["clients"][0]
        assert options["verify"] is False
        assert options["trust_env"] is False
        assert options["follow_redirects"] is False
        sent = wire["requests"][0]
        assert str(sent.url) == "https://api.example.test/v3/mapping"
        assert sent.headers["Accept"] == "application/json"
        assert sent.extensions["timeout"]["connect"] == 30.0
        assert sent.extensions["timeout"]["read"] == 30.0

    def test_tls_verification_is_on_by_default(self, wire: dict[str, Any]) -> None:
        HttpConnection(_record(), ctx=_ctx()).format_url("x")

        assert wire["clients"][0][0]["verify"] is True

    def test_an_environment_proxy_is_not_inherited(
        self, wire: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")

        response = _get(HttpConnection(_record(), ctx=_ctx()), "x")

        assert response.status_code == 200
        assert len(wire["requests"]) == 1

    def test_it_keeps_no_cookies(self, wire: dict[str, Any]) -> None:
        connection = HttpConnection(_record(), ctx=_ctx())

        _get(connection, "setcookie")
        _get(connection, "next")

        assert "cookie" not in wire["requests"][1].headers
        assert len(wire["clients"][0][1].cookies.jar) == 0

    def test_an_explicit_cookie_header_is_still_sent(self, wire: dict[str, Any]) -> None:
        connection = HttpConnection(_record(), ctx=_ctx())

        connection.send_request(HttpRequest(
            "GET", connection.format_url("x"), headers={"Cookie": "chosen=1"},
        ))

        assert wire["requests"][0].headers["cookie"] == "chosen=1"


class TestOwnership:
    def test_close_before_first_request_is_a_no_op(self, wire: dict[str, Any]) -> None:
        connection = HttpConnection(_record(), ctx=_ctx())

        connection.close()

        assert not connection.is_open
        assert wire["clients"] == []

    def test_the_built_transport_is_owned_and_closed(self, wire: dict[str, Any]) -> None:
        connection = HttpConnection(_record(), ctx=_ctx())
        _get(connection, "x")

        connection.close()
        connection.close()

        assert not connection.is_open
        assert wire["clients"][0][1].is_closed

    def test_the_owner_closes_the_built_transport(self, wire: dict[str, Any]) -> None:
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

        assert len(wire["clients"]) == 2

    def test_a_supplied_transport_is_borrowed_never_closed(self) -> None:
        seen: list[httpx.Request] = []
        wire_client = httpx.Client(transport=httpx.MockTransport(_echo(seen)))
        transport = HttpXTransport(client=wire_client)
        connection = HttpConnection(_record(), ctx=_ctx(), transport=transport)
        _get(connection, "x")

        connection.close()
        connection.close()

        assert not connection.is_open
        assert not wire_client.is_closed
        _get(connection, "again")
        assert len(seen) == 2
        assert transport.client is wire_client

    def test_a_supplied_transport_means_nothing_is_built(
        self, wire: dict[str, Any], app: Any,
    ) -> None:
        seen, connect = app

        _get(connect(), "x")

        assert wire["clients"] == []
        assert len(seen) == 1


class TestTheSendSurface:
    def test_a_received_status_is_returned_not_raised(self, app: Any) -> None:
        _seen, connect = app

        for status in (200, 404, 500):
            response = _get(connect(), "x", params={"status": str(status)})
            assert isinstance(response, CoreHttpResponse)
            assert response.status_code == status
            assert response.json() == {"echo": "/v3/x"}
            assert response.headers["retry-after"] == response.headers["Retry-After"] == "1"

    def test_a_large_body_streams(self, app: Any) -> None:
        _seen, connect = app

        response = _get(connect(), "big", stream=True)
        received = sum(len(chunk) for chunk in response.iter_bytes())
        response.close()

        assert received == 100_000

    def test_per_call_timeouts_reach_the_wire(self, app: Any) -> None:
        seen, connect = app

        _get(connect(), "x", connection_timeout=2, read_timeout=5)

        assert seen[0].extensions["timeout"]["connect"] == 2
        assert seen[0].extensions["timeout"]["read"] == 5

    def test_a_redirect_is_returned_by_default(self, wire: dict[str, Any]) -> None:
        response = _get(HttpConnection(_record(auth=None), ctx=_ctx()), "hop")

        assert response.status_code == 302
        assert len(wire["requests"]) == 1

    def test_an_unauthenticated_call_may_follow_redirects(self, app: Any) -> None:
        seen, connect = app

        response = _get(connect(auth=None), "hop", follow_redirects=True)

        assert response.status_code == 200
        assert [str(one.url) for one in seen] == [
            "https://api.example.test/v3/hop", "https://api.example.test/v3/landed",
        ]


class TestPolicies:
    @staticmethod
    def _policies(connection: HttpConnection) -> list[type]:
        client = connection._open()
        return [
            type(getattr(node, "_policy", node))
            for node in client.pipeline._impl_policies
            if type(node).__name__ != "_TransportRunner"
        ]

    def test_exactly_headers_and_credential(self, app: Any) -> None:
        _seen, connect = app

        assert self._policies(connect()) == [HeadersPolicy, ServiceKeyCredentialPolicy]

    def test_only_headers_when_no_auth_is_declared(self, app: Any) -> None:
        _seen, connect = app

        assert self._policies(connect(auth=None)) == [HeadersPolicy]

    def test_a_retryable_status_is_sent_once(self, app: Any) -> None:
        seen, connect = app

        response = _get(connect(), "x", params={"status": "503"})

        assert response.status_code == 503
        assert len(seen) == 1

    def test_no_corehttp_user_agent_is_sent(self, app: Any) -> None:
        seen, connect = app

        _get(connect(), "x")

        assert "python-core" not in seen[0].headers.get("User-Agent", "")


class TestTheSecret:
    def test_the_auth_header_carries_the_resolved_value(self, app: Any) -> None:
        seen, connect = app

        _get(connect(), "x")

        assert seen[0].headers["X-API-KEY"] == _SECRET

    def test_a_prefix_is_sent_before_the_value(self, app: Any) -> None:
        seen, connect = app
        auth = SimpleNamespace(header="Authorization", value="env.example_api_key", prefix="Bearer")

        _get(connect(auth=auth), "x")

        assert seen[0].headers["Authorization"] == f"Bearer {_SECRET}"

    def test_the_value_is_resolved_at_each_request(
        self, app: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen, connect = app
        connection = connect()
        _get(connection, "x")

        monkeypatch.setenv("EXAMPLE_API_KEY", "rotated-key")
        _get(connection, "y")

        assert seen[1].headers["X-API-KEY"] == "rotated-key"

    def test_the_value_is_held_nowhere(self, wire: dict[str, Any]) -> None:
        record = _record()
        connection = HttpConnection(record, ctx=_ctx())
        _get(connection, "x")

        held = {k: v for k, v in vars(connection).items() if k != "_client"}
        assert _SECRET not in repr(held)
        assert _SECRET not in repr(connection)
        assert record.auth.value == "env.example_api_key"
        options, client = wire["clients"][0]
        assert _SECRET not in repr(options)
        assert _SECRET not in repr(dict(client.headers))
        credential = module._EnvKeyCredential(_ctx(), "env.example_api_key")
        assert _SECRET not in repr(credential)
        assert _SECRET not in repr(vars(credential))


class TestTheCredentialStaysInItsOrigin:
    @pytest.mark.parametrize("url", [
        "https://other.example.test/v3/x",
        "https://api.example.test:8443/v3/x",
        "http://api.example.test/v3/x",
        "http://api.example.test:443/v3/x",
    ])
    def test_another_origin_is_refused_before_send(self, url: str, app: Any) -> None:
        seen, connect = app

        with pytest.raises(ConfigError, match="base_url origin"):
            connect().send_request(HttpRequest("GET", url))

        assert seen == []

    @pytest.mark.parametrize("url", [
        "https://api.example.test/other/path",
        "https://API.Example.test:443/v3/x",
    ])
    def test_the_same_origin_is_allowed(self, url: str, app: Any) -> None:
        seen, connect = app

        connect().send_request(HttpRequest("GET", url))

        assert len(seen) == 1

    def test_an_authenticated_call_may_not_follow_redirects(self, app: Any) -> None:
        seen, connect = app

        with pytest.raises(ConfigError, match="does not follow redirects"):
            _get(connect(), "hop", follow_redirects=True)

        assert seen == []

    def test_its_redirect_is_returned_not_followed(self, wire: dict[str, Any]) -> None:
        response = _get(HttpConnection(_record(), ctx=_ctx()), "hop")

        assert response.status_code == 302
        assert len(wire["requests"]) == 1

    def test_an_unauthenticated_connection_may_go_anywhere(self, app: Any) -> None:
        seen, connect = app

        connect(auth=None).send_request(HttpRequest("GET", "https://other.example.test/x"))

        assert len(seen) == 1


class TestFailures:
    @pytest.mark.parametrize("failure, cause", [
        (httpx.ConnectError, ServiceRequestError),
        (httpx.ReadTimeout, ServiceResponseError),
    ])
    def test_a_transport_failure_raises_http_transport_error(
        self, failure: type[httpx.HTTPError], cause: type[Exception],
    ) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise failure("refused", request=request)

        connection = HttpConnection(_record(), ctx=_ctx(), transport=_supplied(refuse))

        with pytest.raises(HttpTransportError, match="was not answered") as raised:
            _get(connection, "x", params={"token": "q-secret"})
        assert isinstance(raised.value.__cause__, cause)
        assert "q-secret" not in str(raised.value)


class TestLogging:
    def test_intent_is_logged_as_metadata_only(
        self, app: Any, caplog: pytest.LogCaptureFixture,
    ) -> None:
        _seen, connect = app
        caplog.set_level(logging.DEBUG, logger=_LOGGER)
        connection = connect()

        connection.send_request(HttpRequest(
            "POST", "https://user:pw@api.example.test/v3/mapping?token=q-secret#frag",
            headers={"X-Extra": "h-secret"}, json={"body": "b-secret"},
        ))

        lines = [r.getMessage() for r in caplog.records if r.name == _LOGGER]
        assert lines == ["http connection example: POST https://api.example.test/v3/mapping"]

    def test_nothing_sensitive_reaches_any_record(
        self, app: Any, caplog: pytest.LogCaptureFixture,
    ) -> None:
        _seen, connect = app
        caplog.set_level(logging.DEBUG)

        connect().send_request(HttpRequest(
            "POST", "https://api.example.test/v3/mapping?token=q-secret",
            headers={"X-Extra": "h-secret"}, json={"body": "b-secret"},
        ))

        for text in (r.getMessage() for r in caplog.records if r.name == _LOGGER):
            for secret in (_SECRET, "q-secret", "h-secret", "b-secret", "echo"):
                assert secret not in text

    def test_a_refusal_is_logged_before_it_is_raised(
        self, app: Any, caplog: pytest.LogCaptureFixture,
    ) -> None:
        _seen, connect = app
        caplog.set_level(logging.DEBUG, logger=_LOGGER)

        with pytest.raises(ConfigError):
            connect().send_request(HttpRequest("GET", "https://other.example.test/x?k=q-secret"))

        lines = [r.getMessage() for r in caplog.records if r.name == _LOGGER]
        assert lines == [
            "http connection example: refused GET https://other.example.test/x "
            "(the credential is sent only to the base_url origin)",
        ]

    def test_an_unanswered_request_is_logged_before_it_is_raised(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        caplog.set_level(logging.DEBUG, logger=_LOGGER)
        connection = HttpConnection(_record(), ctx=_ctx(), transport=_supplied(refuse))

        with pytest.raises(HttpTransportError):
            _get(connection, "x", params={"token": "q-secret"})

        lines = [r.getMessage() for r in caplog.records if r.name == _LOGGER]
        assert lines[-1] == (
            "http connection example: GET https://api.example.test/v3/x "
            "not answered (ServiceRequestError)"
        )


class TestThePublicSurface:
    def test_the_exports(self) -> None:
        assert web_utils.__all__ == ["HttpConnection", "HttpRequest", "HttpResponse"]
        assert HttpRequest is CoreHttpRequest
        assert HttpResponse is CoreHttpResponse

    def test_no_public_member_is_an_httpx_or_client_object(self, app: Any) -> None:
        _seen, connect = app
        connection = connect()
        _get(connection, "x")

        assert not hasattr(connection, "client")
        assert not hasattr(connection, "request")
        for name in dir(connection):
            if name.startswith("_"):
                continue
            value = getattr(connection, name)
            if callable(value):
                continue
            assert not type(value).__module__.startswith(("httpx", "corehttp"))
            assert not isinstance(value, PipelineClient)
