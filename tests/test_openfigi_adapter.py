"""The OpenFIGI v3 mapping adapter (backlog 667).

Asserted: registration by discovery; the options contract; batching in input
order with positional correlation; one row per candidate, a no-match or an
error still one row; the fixed output schema, answered without a request;
rate-limit and retry behaviour for 429, 500 and 503; configuration statuses;
the request shape; the transform end to end; logs without values.

Synthetic only: an HttpConnection whose transport is httpx.MockTransport,
answering as OpenFIGI documents. Waits are recorded, never slept. No live
request and no asset data.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any, Callable, Optional

import httpx
import pytest
from corehttp.transport.httpx import HttpXTransport

from rey_lib.data.http_transform_adapters import openfigi
from rey_lib.data.http_transform_adapters.openfigi import OUTPUT_COLUMNS, OpenFigiAdapter, OpenFigiError
from rey_lib.data.http_transform import HTTPTransform, http_transform_adapter_for
from rey_lib.errors.error_utils import ConfigError
from rey_lib.load import Transform
from rey_lib.load import load_operation
from rey_lib.web_utils import HttpConnection

_SECRET = "synthetic-api-key"

Reply = Callable[[list[dict[str, Any]], int], httpx.Response]


def _candidate(figi: str, ticker: str) -> dict[str, Any]:
    return {"figi": figi, "name": f"{ticker} CO", "ticker": ticker, "exchCode": "US",
            "marketSector": "Equity", "securityType": "Common Stock",
            "securityType2": "Common Stock", "compositeFIGI": figi,
            "shareClassFIGI": "BBG001S5N8V8", "securityDescription": ticker}


def _by_value(jobs: list[dict[str, Any]], _call: int) -> httpx.Response:
    """Answer each job from its idValue: AMB -> two candidates, NONE -> warning,
    BAD -> error, anything else -> one candidate."""
    answers: list[dict[str, Any]] = []
    for job in jobs:
        value = job["idValue"]
        if value == "AMB":
            answers.append({"data": [_candidate("BBG0000A", "AMB"), _candidate("BBG0000B", "AMB")]})
        elif value == "NONE":
            answers.append({"warning": "No identifier found."})
        elif value == "BAD":
            answers.append({"error": "Invalid idValue format."})
        else:
            answers.append({"data": [_candidate(f"BBG-{value}", value)]})
    return httpx.Response(200, json=answers)


class _Provider:
    """A mock OpenFIGI: records every request and answers through ``reply``."""

    def __init__(self, reply: Reply = _by_value) -> None:
        self.requests: list[httpx.Request] = []
        self.reply = reply

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.reply(json.loads(request.content), len(self.requests))

    def jobs(self) -> list[list[dict[str, Any]]]:
        return [json.loads(request.content) for request in self.requests]


def _connection(provider: _Provider) -> HttpConnection:
    record = SimpleNamespace(
        name="openfigi", provider="http", base_url="https://api.openfigi.com/v3/",
        timeout_seconds=30, headers=SimpleNamespace(Accept="application/json"),
        auth=SimpleNamespace(header="X-OPENFIGI-APIKEY", value="env.openfigi_api_key"),
    )
    ctx = SimpleNamespace(
        config_path="/installations/test/openfigi.yaml",
        env=[SimpleNamespace(name="openfigi_api_key", env_var="OPENFIGI_TEST_KEY")],
    )
    transport = HttpXTransport(client=httpx.Client(transport=httpx.MockTransport(provider)))
    return HttpConnection(record, ctx=ctx, transport=transport)


@pytest.fixture(autouse=True)
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Every wait the adapter asks for, recorded instead of slept."""
    monkeypatch.setenv("OPENFIGI_TEST_KEY", _SECRET)
    asked: list[float] = []
    monkeypatch.setattr(openfigi, "_sleep", asked.append)
    return asked


def _apply(provider: _Provider, records: list[dict[str, Any]], **options: Any) -> list[dict]:
    return OpenFigiAdapter().apply(
        records, _connection(provider), {"id_column": "symbol", "id_type": "TICKER", **options},
    )


def _records(*symbols: str) -> list[dict[str, Any]]:
    return [{"symbol": symbol, "note": f"n{index}"} for index, symbol in enumerate(symbols)]


class TestRegistration:
    def test_it_is_discovered_as_openfigi(self) -> None:
        assert isinstance(http_transform_adapter_for("openfigi"), OpenFigiAdapter)


class TestOptions:
    @pytest.mark.parametrize(("options", "message"), [
        ({"id_type": "TICKER"}, "id_column is required"),
        ({"id_column": "symbol"}, "id_type is required"),
        ({"job": ["exchCode"]}, "job must be a mapping"),
        ({"job": {"exchCode": "US", "colour": "red"}}, "does not document: colour"),
        ({"batch_size": 0}, "batch_size must be 1..100"),
        ({"batch_size": 101}, "batch_size must be 1..100"),
        ({"batch_size": "10"}, "batch_size must be a whole number"),
        ({"max_retries": -1}, "max_retries must not be negative"),
    ])
    def test_a_malformed_option_is_refused_before_any_request(
        self, options: dict[str, Any], message: str,
    ) -> None:
        provider = _Provider()
        # The two required options are given unless the case is about one of them.
        required = {} if {"id_column", "id_type"} & set(options) else {
            "id_column": "symbol", "id_type": "TICKER",
        }

        with pytest.raises(ConfigError, match=message):
            OpenFigiAdapter().apply(_records("A"), _connection(provider), {**required, **options})
        assert provider.requests == []

    def test_an_id_column_the_records_lack_is_refused(self) -> None:
        provider = _Provider()

        with pytest.raises(ConfigError, match="'ticker' is not a column"):
            _apply(provider, _records("A"), id_column="ticker")
        assert provider.requests == []


class TestBatchingAndCorrelation:
    def test_the_default_batch_is_five(self) -> None:
        provider = _Provider()

        _apply(provider, _records(*[f"S{n}" for n in range(12)]))

        assert [len(jobs) for jobs in provider.jobs()] == [5, 5, 2]

    def test_batches_go_in_input_order_and_results_come_back_by_position(self) -> None:
        provider = _Provider()
        symbols = [f"S{n}" for n in range(25)]

        rows = _apply(provider, _records(*symbols), batch_size=10)

        assert [len(jobs) for jobs in provider.jobs()] == [10, 10, 5]
        assert [job["idValue"] for jobs in provider.jobs() for job in jobs] == symbols
        assert [(row["figi_job"], row["symbol"], row["figi_figi"]) for row in rows] == [
            (n, f"S{n}", f"BBG-S{n}") for n in range(25)
        ]

    def test_a_record_without_an_identifier_is_not_sent(self) -> None:
        provider = _Provider()

        rows = _apply(provider, _records("A", "", "B"))

        assert [job["idValue"] for job in provider.jobs()[0]] == ["A", "B"]
        assert [(row["figi_job"], row["figi_status"]) for row in rows] == [
            (0, "matched"), (1, "no_match"), (2, "matched"),
        ]
        assert rows[1]["figi_message"] == "no identifier in symbol"

    def test_a_response_of_the_wrong_length_is_refused(self) -> None:
        provider = _Provider(lambda jobs, _call: httpx.Response(200, json=[{"data": []}]))

        with pytest.raises(OpenFigiError, match="cannot be correlated"):
            _apply(provider, _records("A", "B"))


class TestResults:
    def test_one_row_per_candidate_and_none_dropped(self) -> None:
        rows = _apply(_Provider(), _records("AMB"))

        assert [(row["figi_status"], row["figi_candidate_count"], row["figi_figi"])
                for row in rows] == [("matched", 2, "BBG0000A"), ("matched", 2, "BBG0000B")]
        assert all(row["symbol"] == "AMB" and row["note"] == "n0" for row in rows)

    def test_a_warning_is_one_no_match_row(self) -> None:
        (row,) = _apply(_Provider(), _records("NONE"))

        assert (row["figi_status"], row["figi_candidate_count"], row["figi_message"]) == (
            "no_match", 0, "No identifier found.",
        )
        assert row["figi_figi"] == ""

    def test_a_job_error_is_one_error_row_not_raised(self) -> None:
        (row,) = _apply(_Provider(), _records("BAD"))

        assert (row["figi_status"], row["figi_message"]) == ("error", "Invalid idValue format.")

    def test_every_row_carries_exactly_the_declared_columns(self) -> None:
        rows = _apply(_Provider(), _records("A", "AMB", "NONE", "BAD"))

        expected = OpenFigiAdapter().output_columns(["symbol", "note"])
        assert all(list(row) == expected for row in rows)
        assert expected[2:] == list(OUTPUT_COLUMNS)

    def test_metadata_is_kept_as_text(self) -> None:
        reply = lambda jobs, _call: httpx.Response(200, json=[{"data": [{"metadata": {"k": 1}}]}])

        (row,) = _apply(_Provider(reply), _records("A"))

        assert row["figi_metadata"] == '{"k": 1}'

    def test_output_columns_sends_nothing(self) -> None:
        provider = _Provider()
        built = HTTPTransform(_connection(provider), OpenFigiAdapter(), {})

        built.columns_for_names(["symbol"])

        assert provider.requests == []


def _status(status: int, headers: Optional[dict[str, str]] = None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {}, json={"error": "x"})


def _then_ok(*first: httpx.Response) -> Reply:
    def reply(jobs: list[dict[str, Any]], call: int) -> httpx.Response:
        return first[call - 1] if call <= len(first) else _by_value(jobs, call)
    return reply


class TestRateLimitsAndRetries:
    def test_a_spent_window_waits_its_reset_before_the_next_batch(self, waits: list[float]) -> None:
        def reply(jobs: list[dict[str, Any]], call: int) -> httpx.Response:
            answer = _by_value(jobs, call)
            answer.headers["ratelimit-remaining"] = "0" if call == 1 else "5"
            answer.headers["ratelimit-reset"] = "6"
            return answer

        provider = _Provider(reply)
        _apply(provider, _records("A", "B"), batch_size=1)

        assert len(provider.requests) == 2
        assert waits == [6.0]

    def test_a_429_waits_retry_after_and_repeats_the_same_batch(self, waits: list[float]) -> None:
        provider = _Provider(_then_ok(_status(429, {"Retry-After": "7", "ratelimit-reset": "9"})))

        rows = _apply(provider, _records("A"))

        assert waits == [7.0]
        assert provider.jobs()[0] == provider.jobs()[1]
        assert rows[0]["figi_status"] == "matched"

    def test_a_429_without_retry_after_follows_the_reset(self, waits: list[float]) -> None:
        _apply(_Provider(_then_ok(_status(429, {"ratelimit-reset": "9"}))), _records("A"))

        assert waits == [9.0]

    def test_a_429_naming_no_wait_waits_a_minute(self, waits: list[float]) -> None:
        _apply(_Provider(_then_ok(_status(429))), _records("A"))

        assert waits == [60.0]

    @pytest.mark.parametrize("status", [500, 503])
    def test_a_500_or_503_backs_off_exponentially(self, status: int, waits: list[float]) -> None:
        provider = _Provider(_then_ok(_status(status), _status(status), _status(status)))

        rows = _apply(provider, _records("A"))

        assert waits == [1.0, 2.0, 4.0]
        assert len(provider.requests) == 4
        assert rows[0]["figi_status"] == "matched"

    def test_retries_exhausted_raise(self, waits: list[float]) -> None:
        provider = _Provider(lambda jobs, _call: _status(503))

        with pytest.raises(OpenFigiError, match="status 503 after 2 retr"):
            _apply(provider, _records("A"), max_retries=2)
        assert len(provider.requests) == 3

    def test_an_undocumented_status_raises_at_once(self, waits: list[float]) -> None:
        provider = _Provider(lambda jobs, _call: _status(502))

        with pytest.raises(OpenFigiError, match="502 is not one OpenFIGI documents"):
            _apply(provider, _records("A"))
        assert len(provider.requests) == 1 and waits == []

    @pytest.mark.parametrize(("status", "message"), [
        (400, "invalid payload"),
        (401, "the API key is invalid"),
        (413, "batch_size exceeds"),
    ])
    def test_a_configuration_status_is_a_config_error(self, status: int, message: str) -> None:
        provider = _Provider(lambda jobs, _call: _status(status))

        with pytest.raises(ConfigError, match=message):
            _apply(provider, _records("A"))
        assert len(provider.requests) == 1


class TestTheRequest:
    def test_it_posts_jobs_to_mapping_with_the_key(self) -> None:
        provider = _Provider()

        _apply(provider, _records("IBM"), job={"exchCode": "US"})

        (request,) = provider.requests
        assert request.method == "POST"
        assert str(request.url) == "https://api.openfigi.com/v3/mapping"
        assert request.headers["X-OPENFIGI-APIKEY"] == _SECRET
        assert json.loads(request.content) == [
            {"exchCode": "US", "idType": "TICKER", "idValue": "IBM"},
        ]


class TestThroughTheTransform:
    def test_an_http_transform_naming_openfigi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _Provider()
        connection = _connection(provider)
        monkeypatch.setattr(load_operation, "shared_connection", lambda _ctx, _name: connection)
        # The operator configuration for an authenticated connection: batch_size 100.
        transform = Transform({
            "http-connection": "openfigi", "http-adapter": "openfigi",
            "http-options": {"id_column": "symbol", "id_type": "TICKER", "batch_size": 100},
        }, selected="http")

        built = transform.resolve(SimpleNamespace())
        rows = built.transform(_records("A", "AMB"))

        assert isinstance(built.adapter, OpenFigiAdapter)
        assert [row["figi_figi"] for row in rows] == ["BBG-A", "BBG0000A", "BBG0000B"]
        assert built.columns_for_names(["symbol", "note"]) == list(rows[0])
        assert len(provider.requests) == 1


class TestLogging:
    def test_no_identifier_or_value_is_logged(
        self, caplog: pytest.LogCaptureFixture, waits: list[float],
    ) -> None:
        caplog.set_level(logging.DEBUG)

        _apply(_Provider(_then_ok(_status(429, {"Retry-After": "3"}))), _records("SECRETSYM"))

        lines = [r.getMessage() for r in caplog.records if r.name.startswith("rey_lib")]
        assert "openfigi: batch 1 of 1, 1 job(s)" in lines
        assert "openfigi: waiting 3.0 s (status 429)" in lines
        assert not any("SECRETSYM" in line or _SECRET in line for line in lines)
