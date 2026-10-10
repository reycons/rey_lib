"""HttpOperation: one complete governed HTTP job, executed generically (backlog 813).

Asserted rule by rule against the approved contract: what hydration refuses;
the one pre-execution check; the connection built from the job, its secret
resolved by HttpConnection; request filling and value maps; position and
request correlation; link pagination; retry with Retry-After; json and csv
responses; the outcome rows; and the reads through Control.

Synthetic only: neutral job rows, answered by httpx.MockTransport.
"""

from __future__ import annotations

import json
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable

import httpx
import pytest
from corehttp.transport.httpx import HttpXTransport

from rey_lib import web_utils
from rey_lib.control.control import Control
from rey_lib.errors.error_utils import ConfigError, HttpTransportError, StateError
from rey_lib.web_utils import operation as operation_module
from rey_lib.web_utils.connection import HttpConnection
from rey_lib.web_utils.operation import HttpOperation

_SECRET = "synthetic-api-key"


def _job(**overrides: Any) -> dict[str, Any]:
    """A neutral job row, as f_http_operation_get shapes it."""
    job: dict[str, Any] = {
        "adapter_key": "lookup", "connection_name": "site",
        "base_url": "https://api.example.test/v1/", "auth_header": None, "auth_prefix": None,
        "auth_secret_ref": None, "auth_required": True, "timeout_seconds": 5, "verify": True,
        "default_headers": None, "http_method": "POST", "path": "find",
        "request_format": "json", "response_format": "json", "is_enabled": True,
        "request_body_fields": ["id"], "request_body_kinds": ["reference"],
        "request_body_values": ["code"],
        "response_field_names": ["found"], "response_field_paths": ["$.name"],
        "batch_size": 2, "correlation_mode": "position", "response_records_path": "$.data",
    }
    job.update(overrides)
    return job


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        config_path="/installations/test/http_operation.yaml",
        env=[SimpleNamespace(name="example_api_key", env_var="EXAMPLE_API_KEY")],
    )


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    waits: list[float] = []
    monkeypatch.setattr(operation_module, "_sleep", waits.append)
    return waits


class _Seen(list):
    """The requests sent, and the connections the operation built to send them."""

    built: list[HttpConnection]


def _wire(monkeypatch: pytest.MonkeyPatch,
          handler: Callable[[httpx.Request], httpx.Response]) -> _Seen:
    """The job's own connection, built as HttpOperation builds it, over a mocked transport."""
    seen = _Seen()
    built: list[HttpConnection] = []
    seen.built = built

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def connection(config: Any, ctx: Any) -> HttpConnection:
        one = HttpConnection(config, ctx, transport=HttpXTransport(
            client=httpx.Client(transport=httpx.MockTransport(answer))))
        built.append(one)
        return one

    monkeypatch.setattr(web_utils, "HttpConnection", connection)
    return seen


def _run(job: dict[str, Any], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return HttpOperation.hydrate(job).execute(records, _ctx())


class TestHydrationRefuses:
    @pytest.mark.parametrize(("overrides", "message"), [
        ({"is_enabled": False}, "it is disabled"),
        ({"correlation_mode": None}, "correlation_mode is not set"),
        ({"batch_size": None}, "position correlation needs batch_size"),
        ({"correlation_mode": "request", "batch_size": 5}, "one input per HTTP request"),
        ({"correlation_mode": "request", "batch_size": None, "pagination_mode": "link"},
         "link pagination needs pagination_next_path"),
        ({"pagination_mode": "link", "pagination_next_path": "$.next"},
         "link pagination under position correlation is not supported"),
        ({"response_text_format": "csv"}, "applies only to a text response"),
        ({"request_format": "query"}, "body fields are sent only as a json request"),
        ({"request_query_fields": ["q"], "request_query_kinds": ["reference"],
          "request_query_values": ["code"]},
         "query or header field referencing an input is not supported"),
        ({"value_map_parts": ["body"], "value_map_fields": ["other"],
          "value_map_inputs": ["a"], "value_map_outputs": ["b"]},
         "value map names body field other"),
    ])
    def test_a_job_that_cannot_be_executed(self, overrides: dict, message: str) -> None:
        with pytest.raises(ConfigError, match=message):
            HttpOperation.hydrate(_job(**overrides))

    def test_a_literal_query_or_header_is_fine_in_a_position_batch(self) -> None:
        HttpOperation.hydrate(_job(
            request_header_fields=["Accept"], request_header_kinds=["literal"],
            request_header_values=["application/json"],
        ))

    def test_a_referenced_query_is_fine_in_a_position_batch_of_one(self) -> None:
        HttpOperation.hydrate(_job(
            batch_size=1, request_query_fields=["q"], request_query_kinds=["reference"],
            request_query_values=["code"],
        ))


class TestThePreExecutionCheck:
    def test_a_referenced_column_must_be_supplied(self) -> None:
        with pytest.raises(ConfigError, match="reads column\\(s\\) code"):
            HttpOperation.hydrate(_job()).check_references(["other"])

    def test_a_response_field_may_not_overwrite_an_input_column(self) -> None:
        with pytest.raises(ConfigError, match="found.*would overwrite"):
            HttpOperation.hydrate(_job()).check_references(["code", "found"])

    def test_execute_checks_before_sending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _wire(monkeypatch, lambda request: httpx.Response(200, json=[]))

        with pytest.raises(ConfigError):
            _run(_job(), [{"other": "x"}])
        assert seen == []

    def test_output_columns_send_nothing(self) -> None:
        assert HttpOperation.hydrate(_job()).output_columns(["code"]) == [
            "code", "found", "operation_status", "operation_message",
        ]


class TestTheConnection:
    def test_the_secret_reference_is_resolved_by_the_connection(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("EXAMPLE_API_KEY", _SECRET)
        seen = _wire(monkeypatch, lambda request: httpx.Response(200, json=[{"data": []}]))

        _run(_job(auth_header="X-API-KEY", auth_secret_ref="env.example_api_key", batch_size=1),
             [{"code": "A"}])

        assert seen[0].headers["X-API-KEY"] == _SECRET
        assert "env.example_api_key" not in str(seen[0].headers)

    def test_an_optional_unset_secret_sends_no_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EXAMPLE_API_KEY", raising=False)
        seen = _wire(monkeypatch, lambda request: httpx.Response(200, json=[{"data": []}]))

        _run(_job(auth_header="X-API-KEY", auth_secret_ref="env.example_api_key",
                  auth_required=False, batch_size=1), [{"code": "A"}])

        assert "X-API-KEY" not in seen[0].headers

class TestTheSharedConnection:
    """The job's connection is the shared owner's, built once (backlog 838)."""

    def test_one_connection_is_held_across_executes_and_none_is_closed(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen = _wire(monkeypatch, lambda request: httpx.Response(200, json=[{"data": []}]))
        operation = HttpOperation.hydrate(_job(batch_size=1))

        operation.execute([{"code": "A"}], _ctx())
        operation.execute([{"code": "B"}], _ctx())

        (built,) = seen.built
        assert built.is_open is True

    def test_two_jobs_naming_one_connection_share_it(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen = _wire(monkeypatch, lambda request: httpx.Response(200, json=[{"data": []}]))

        _run(_job(batch_size=1), [{"code": "A"}])
        _run(_job(adapter_key="other", path="other", batch_size=1), [{"code": "B"}])

        assert len(seen.built) == 1
        assert [request.url.path for request in seen] == ["/v1/find", "/v1/other"]

    def test_a_disagreeing_definition_is_refused_naming_fields_not_values(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _wire(monkeypatch, lambda request: httpx.Response(200, json=[{"data": []}]))
        _run(_job(batch_size=1), [{"code": "A"}])

        with pytest.raises(ConfigError, match=r"\(base_url differ\)") as refused:
            _run(_job(base_url="https://elsewhere.example.test/", batch_size=1), [{"code": "B"}])

        assert "elsewhere" not in str(refused.value)

    def test_jobs_with_different_default_headers_share_one_connection(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen = _wire(monkeypatch, lambda request: httpx.Response(200, json=[{"data": []}]))

        _run(_job(default_headers={"User-Agent": "one"}, batch_size=1), [{"code": "A"}])
        _run(_job(default_headers={"User-Agent": "two"}, batch_size=1), [{"code": "B"}])

        assert len(seen.built) == 1
        assert [request.headers["User-Agent"] for request in seen] == ["one", "two"]

    def test_a_default_header_wins_a_same_named_header_field(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen = _wire(monkeypatch, lambda request: httpx.Response(200, json=[{"data": []}]))

        _run(_job(
            default_headers={"Accept": "application/json"},
            request_header_fields=["Accept"], request_header_kinds=["literal"],
            request_header_values=["text/plain"], batch_size=1,
        ), [{"code": "A"}])

        assert seen[0].headers["Accept"] == "application/json"


class TestRequests:
    def test_position_batches_send_one_body_element_per_input(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen = _wire(monkeypatch, lambda request: httpx.Response(
            200, json=[{"data": []} for _ in json.loads(request.content)]))

        _run(_job(), [{"code": "A"}, {"code": "B"}, {"code": "C"}])

        assert [json.loads(one.content) for one in seen] == [
            [{"id": "A"}, {"id": "B"}], [{"id": "C"}],
        ]

    def test_a_null_reference_leaves_its_field_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _wire(monkeypatch, lambda request: httpx.Response(200, json=[{"data": []}]))

        _run(_job(batch_size=1, request_body_fields=["id", "kind"],
                  request_body_kinds=["reference", "literal"],
                  request_body_values=["code", "TICKER"]), [{"code": None}])

        assert json.loads(seen[0].content) == [{"kind": "TICKER"}]

    def test_query_and_header_fields_under_request_correlation(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen = _wire(monkeypatch, lambda request: httpx.Response(200, json={"data": []}))

        _run(_job(http_method="GET", request_format="query", correlation_mode="request",
                  batch_size=None, request_body_fields=None, request_body_kinds=None,
                  request_body_values=None,
                  request_query_fields=["filter[code]"], request_query_kinds=["reference"],
                  request_query_values=["code"],
                  request_header_fields=["Accept"], request_header_kinds=["literal"],
                  request_header_values=["application/json"]),
             [{"code": "A"}, {"code": "B"}])

        assert [one.url.params["filter[code]"] for one in seen] == ["A", "B"]
        assert seen[0].headers["Accept"] == "application/json"


class TestValueMaps:
    _MAPPED = {"value_map_parts": ["body"], "value_map_fields": ["id"],
               "value_map_inputs": ["A"], "value_map_outputs": ["SITE_A"]}

    def test_a_mapped_value_is_sent_translated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _wire(monkeypatch, lambda request: httpx.Response(200, json=[{"data": []}]))

        _run(_job(batch_size=1, **self._MAPPED), [{"code": "A"}])

        assert json.loads(seen[0].content) == [{"id": "SITE_A"}]

    @pytest.mark.parametrize(("value", "reason"), [
        ("B", "has no value map entry"), (7, "is not a string"),
    ])
    def test_an_unmappable_record_is_not_sent_and_is_an_error_row(
        self, monkeypatch: pytest.MonkeyPatch, value: Any, reason: str,
    ) -> None:
        seen = _wire(monkeypatch, lambda request: httpx.Response(200, json=[{"data": []}]))

        rows = _run(_job(**self._MAPPED), [{"code": value}, {"code": "A"}])

        assert json.loads(seen[0].content) == [{"id": "SITE_A"}]
        refused = rows[0]
        assert refused["operation_status"] == "error"
        assert reason in refused["operation_message"] and "body field id" in refused["operation_message"]
        assert str(value) not in refused["operation_message"].replace("body field id", "")


class TestOutcomes:
    def test_records_errors_and_no_match_by_position(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _wire(monkeypatch, lambda request: httpx.Response(200, json=[
            {"data": [{"name": "one"}, {"name": "two"}]},
            {"error": "Invalid idValue."},
        ]))

        rows = _run(_job(response_error_path="$.error", response_message_path="$.warning"),
                    [{"code": "A"}, {"code": "B"}])

        assert [(r["code"], r["found"], r["operation_status"], r["operation_message"]) for r in rows] == [
            ("A", "one", "ok", None), ("A", "two", "ok", None),
            ("B", None, "error", "Invalid idValue."),
        ]

    def test_no_match_carries_the_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _wire(monkeypatch, lambda request: httpx.Response(200, json=[{"warning": "No match."}]))

        (row,) = _run(_job(batch_size=1, response_message_path="$.warning"), [{"code": "A"}])

        assert (row["operation_status"], row["operation_message"]) == ("no_match", "No match.")

    def test_a_position_answer_of_the_wrong_length_is_a_state_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _wire(monkeypatch, lambda request: httpx.Response(200, json=[{"data": []}]))

        with pytest.raises(StateError, match="2 sent, 1 answered"):
            _run(_job(), [{"code": "A"}, {"code": "B"}])


class TestLinkPagination:
    _PAGED = {"http_method": "GET", "request_format": "query", "correlation_mode": "request",
              "batch_size": None, "request_body_fields": None, "request_body_kinds": None,
              "request_body_values": None, "request_query_fields": ["q"],
              "request_query_kinds": ["reference"], "request_query_values": ["code"],
              "request_header_fields": ["Accept"], "request_header_kinds": ["literal"],
              "request_header_values": ["application/json"],
              "pagination_mode": "link", "pagination_next_path": "$.next",
              "response_error_path": "$.error", "response_message_path": "$.note"}

    def test_every_page_is_followed_as_a_get_with_the_same_headers(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pages = iter([
            {"data": [{"name": "one"}], "next": "https://api.example.test/v1/find?page=2"},
            {"error": "partial", "next": "https://api.example.test/v1/find?page=3"},
            {"data": [{"name": "three"}]},
        ])
        seen = _wire(monkeypatch, lambda request: httpx.Response(200, json=next(pages)))

        rows = _run(_job(**self._PAGED), [{"code": "A"}])

        assert [(r["operation_status"], r["found"], r["operation_message"]) for r in rows] == [
            ("ok", "one", None), ("error", None, "partial"), ("ok", "three", None),
        ]
        assert [(one.method, one.url.params.get("page"), one.content) for one in seen[1:]] == [
            ("GET", "2", b""), ("GET", "3", b""),
        ]
        assert all(one.headers["Accept"] == "application/json" for one in seen)

    def test_no_match_takes_the_first_message_in_page_order(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pages = iter([
            {"data": [], "next": "https://api.example.test/v1/find?page=2"},
            {"data": [], "note": "first", "next": "https://api.example.test/v1/find?page=3"},
            {"data": [], "note": "second"},
        ])
        _wire(monkeypatch, lambda request: httpx.Response(200, json=next(pages)))

        (row,) = _run(_job(**self._PAGED), [{"code": "A"}])

        assert (row["operation_status"], row["operation_message"]) == ("no_match", "first")


class TestRetry:
    _RETRIED = {"batch_size": 1, "retry_status_codes": [429, 503], "retry_max_attempts": 3,
                "retry_backoff": "exponential"}

    def test_retry_after_seconds_then_success(
        self, monkeypatch: pytest.MonkeyPatch, _no_waiting: list[float],
    ) -> None:
        answers = iter([httpx.Response(429, headers={"Retry-After": "7"}),
                        httpx.Response(200, json=[{"data": []}])])
        seen = _wire(monkeypatch, lambda request: next(answers))

        _run(_job(**self._RETRIED), [{"code": "A"}])

        assert len(seen) == 2 and _no_waiting == [7.0]

    def test_retry_after_as_an_http_date(
        self, monkeypatch: pytest.MonkeyPatch, _no_waiting: list[float],
    ) -> None:
        when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
        answers = iter([httpx.Response(503, headers={"Retry-After": when}),
                        httpx.Response(200, json=[{"data": []}])])
        _wire(monkeypatch, lambda request: next(answers))

        _run(_job(**self._RETRIED), [{"code": "A"}])

        assert 25 < _no_waiting[0] <= 30

    def test_without_retry_after_the_waits_double(
        self, monkeypatch: pytest.MonkeyPatch, _no_waiting: list[float],
    ) -> None:
        _wire(monkeypatch, lambda request: httpx.Response(503))

        with pytest.raises(HttpTransportError, match="HTTP 503 after 3 attempt"):
            _run(_job(**self._RETRIED), [{"code": "A"}])
        assert _no_waiting == [1.0, 2.0]

    def test_an_unlisted_status_fails_at_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _wire(monkeypatch, lambda request: httpx.Response(400))

        with pytest.raises(HttpTransportError, match="HTTP 400 after 1 attempt"):
            _run(_job(**self._RETRIED), [{"code": "A"}])
        assert len(seen) == 1


class TestTextResponses:
    def test_a_csv_is_read_by_its_owner_and_its_columns_by_header(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _wire(monkeypatch, lambda request: httpx.Response(200, text="CODE,NAME\nA,Alpha\nB,Beta\n"))

        rows = _run(_job(http_method="GET", request_format="query", response_format="text",
                         response_text_format="csv", correlation_mode="request", batch_size=None,
                         request_body_fields=None, request_body_kinds=None,
                         request_body_values=None, response_records_path=None,
                         response_field_names=["found", "missing"],
                         response_field_paths=["NAME", "ABSENT"]),
                    [{"code": "x"}])

        assert [(r["found"], r["missing"], r["operation_status"]) for r in rows] == [
            ("Alpha", None, "ok"), ("Beta", None, "ok"),
        ]

    def test_an_unimplemented_format_is_refused_at_use(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _wire(monkeypatch, lambda request: httpx.Response(200, content=b"\x00"))

        with pytest.raises(ConfigError, match="response_format 'binary' is not implemented"):
            _run(_job(response_format="binary", batch_size=1), [{"code": "A"}])


class TestTheControlRead:
    def test_http_operation_dispatches_the_one_binding(self) -> None:
        calls: list[tuple[str, dict[str, Any]]] = []
        control = Control.__new__(Control)
        control._call_rows = lambda action, variables, required=False: (  # type: ignore[method-assign]
            calls.append((action, dict(variables))) or [_job()]
        )

        rows = control.http_operation("local", "lookup")

        assert calls == [("get_http_operation", {"installation": "local", "adapter_key": "lookup"})]
        assert rows == [_job()]

    def test_every_operation_when_no_key_is_named(self) -> None:
        calls: list[tuple[str, dict[str, Any]]] = []
        control = Control.__new__(Control)
        control._call_rows = lambda action, variables, required=False: (  # type: ignore[method-assign]
            calls.append((action, dict(variables))) or []
        )

        control.http_operation("local")

        assert calls == [("get_http_operation", {"installation": "local", "adapter_key": None})]
