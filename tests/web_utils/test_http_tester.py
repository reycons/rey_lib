"""The HTTP tester sends one request through the public HTTP layer (backlog 672).

Manual tooling, not a client surface. Synthetic only: connections are answered
through the public ``transport=`` boundary by ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from corehttp.transport.httpx import HttpXTransport

import rey_lib.web_utils as web_utils
from rey_lib.errors.error_utils import ConfigError, HttpTransportError
from rey_lib.web_utils import HttpConnection
from rey_lib.web_utils import tester

_SECRET = "synthetic-api-key"


def _record(**overrides: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "name": "example",
        "provider": "http",
        "base_url": "https://api.example.test/v3/",
        "auth": SimpleNamespace(header="X-API-KEY", value="env.example_api_key"),
        "timeout_seconds": 30,
    }
    fields.update(overrides)
    return SimpleNamespace(**{k: v for k, v in fields.items() if v is not None})


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        config_path="/installations/test/tester.yaml",
        env=[SimpleNamespace(name="example_api_key", env_var="EXAMPLE_API_KEY")],
    )


def _answer(seen: list[httpx.Request]) -> Any:
    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/hop"):
            return httpx.Response(302, headers={"Location": "https://api.example.test/v3/landed"})
        status = int(request.url.params.get("status", "200"))
        return httpx.Response(status, json={"path": request.url.path, "n": [1, 2]})
    return answer


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXAMPLE_API_KEY", _SECRET)


@pytest.fixture
def connect() -> Any:
    seen: list[httpx.Request] = []

    def make(**overrides: Any) -> HttpConnection:
        transport = HttpXTransport(client=httpx.Client(transport=httpx.MockTransport(_answer(seen))))
        return HttpConnection(_record(**overrides), ctx=_ctx(), transport=transport)

    make.seen = seen  # type: ignore[attr-defined]
    return make


class TestItIsATesterNotAClientSurface:
    def test_it_is_not_exported_from_web_utils(self) -> None:
        assert "exchange" not in web_utils.__all__
        assert not hasattr(web_utils, "exchange")


class TestExchange:
    def test_a_get_with_query_and_headers(self, connect: Any) -> None:
        result = tester.exchange(
            connect(), "get", "mapping", query={"a": "1"}, headers={"X-Trace": "t"},
        )

        sent = connect.seen[0]
        assert sent.method == "GET"
        assert str(sent.url) == "https://api.example.test/v3/mapping?a=1"
        assert sent.headers["X-Trace"] == "t"
        assert result.status == 200
        assert json.loads(result.body) == {"path": "/v3/mapping", "n": [1, 2]}
        assert "json" in result.content_type
        assert result.elapsed_ms >= 0

    def test_a_json_body(self, connect: Any) -> None:
        tester.exchange(connect(), "POST", "mapping", json_body=[{"idType": "X"}])

        assert json.loads(connect.seen[0].content) == [{"idType": "X"}]

    def test_a_raw_body(self, connect: Any) -> None:
        tester.exchange(connect(), "PUT", "raw", content="plain text")

        assert connect.seen[0].content == b"plain text"

    @pytest.mark.parametrize("status", [404, 500])
    def test_an_error_status_is_reported_not_raised(self, connect: Any, status: int) -> None:
        result = tester.exchange(connect(), "GET", "x", query={"status": str(status)})

        assert result.status == status

    def test_a_timeout_reaches_the_wire(self, connect: Any) -> None:
        tester.exchange(connect(), "GET", "x", timeout_seconds=4)

        assert connect.seen[0].extensions["timeout"]["read"] == 4

    def test_redirects_are_followed_only_on_request(self, connect: Any) -> None:
        unfollowed = tester.exchange(connect(auth=None), "GET", "hop")
        followed = tester.exchange(connect(auth=None), "GET", "hop", follow_redirects=True)

        assert unfollowed.status == 302
        assert unfollowed.headers["location"] == "https://api.example.test/v3/landed"
        assert followed.status == 200

    def test_the_connections_refusal_propagates(self, connect: Any) -> None:
        with pytest.raises(ConfigError, match="does not follow redirects"):
            tester.exchange(connect(), "GET", "hop", follow_redirects=True)

    def test_a_transport_failure_propagates(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        transport = HttpXTransport(client=httpx.Client(transport=httpx.MockTransport(refuse)))
        connection = HttpConnection(_record(), ctx=_ctx(), transport=transport)

        with pytest.raises(HttpTransportError):
            tester.exchange(connection, "GET", "x")


class TestTheCommand:
    @pytest.fixture
    def wired(self, connect: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
        connection = connect()
        monkeypatch.setattr(tester, "_connection", lambda path, app, name: connection)
        return connect

    def _run(self, *args: str) -> int:
        return tester.main(["--config-path", "/cfg.yaml", "--connection", "example", *args])

    def test_it_prints_status_headers_and_pretty_json(
        self, wired: Any, capsys: pytest.CaptureFixture[str],
    ) -> None:
        code = self._run("--method", "POST", "--query", "a=1", "--header", "X-Trace=t",
                         "--json", '{"k": "v"}', "mapping")

        out = capsys.readouterr().out
        assert code == 0
        assert out.startswith("Status:  200 OK\nElapsed: ")
        assert "  content-type: application/json" in out
        assert '"path": "/v3/mapping"' in out
        assert '  "n": [\n' in out
        assert json.loads(wired.seen[0].content) == {"k": "v"}

    def test_any_status_exits_zero(self, wired: Any) -> None:
        assert self._run("--query", "status=500", "x") == 0

    def test_a_body_file_is_sent(self, wired: Any, tmp_path: Any) -> None:
        body = tmp_path / "body.txt"
        body.write_text("from file")

        assert self._run("--method", "POST", "--body-file", str(body), "x") == 0
        assert wired.seen[0].content == b"from file"

    def test_a_refusal_exits_one(self, wired: Any) -> None:
        assert self._run("--follow-redirects", "hop") == 1

    def test_an_unanswered_request_exits_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        transport = HttpXTransport(client=httpx.Client(transport=httpx.MockTransport(refuse)))
        connection = HttpConnection(_record(), ctx=_ctx(), transport=transport)
        monkeypatch.setattr(tester, "_connection", lambda path, app, name: connection)

        assert self._run("x") == 1

    @pytest.mark.parametrize("args", [
        ("--query", "novalue", "x"),
        ("--json", "{not json", "x"),
    ])
    def test_bad_arguments_exit_two(self, wired: Any, args: tuple[str, ...]) -> None:
        assert self._run(*args) == 2

    def test_a_database_connection_exits_two(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def lookup(path: str, app: str, name: str) -> Any:
            raise ConfigError(f"connection '{name}' is a postgres connection, not http.")

        monkeypatch.setattr(tester, "_connection", lookup)

        assert self._run("x") == 2

    def test_a_non_http_connection_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import rey_lib.config.config_context as config_context
        import rey_lib.db.connection as db_connection

        monkeypatch.setattr(config_context, "build_ctx_from_path", lambda path, app_name: object())
        monkeypatch.setattr(db_connection, "shared_connection",
                            lambda ctx, name: SimpleNamespace(provider="postgres"))

        with pytest.raises(ConfigError, match="postgres connection, not http"):
            tester._connection("/cfg.yaml", "rey_console", "control")

    def test_no_request_header_or_credential_is_printed_or_logged(
        self, wired: Any, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG)

        self._run("--header", "X-Trace=h-secret", "x")

        out = capsys.readouterr().out
        assert wired.seen[0].headers["X-API-KEY"] == _SECRET
        for text in (out, *(r.getMessage() for r in caplog.records)):
            assert _SECRET not in text
            assert "h-secret" not in text
