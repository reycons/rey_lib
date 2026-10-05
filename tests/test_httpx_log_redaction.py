"""httpx's own request records cannot expose URL query values (backlog 666).

httpx logs every request at INFO with its full URL. The provider filter every
Rey handler carries removes query, fragment and userinfo from those URLs and
keeps method, host, path and status.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from rey_lib.logs.logging_setup import _ProviderWarningFilter, redacted_url


def _httpx_record(url: object, status: int = 200, reason: str = "OK") -> logging.LogRecord:
    """The record httpx emits for one request."""
    return logging.LogRecord(
        "httpx", logging.INFO, __file__, 1, 'HTTP Request: %s %s "%s %d %s"',
        ("GET", url, "HTTP/1.1", status, reason), None,
    )


class TestRedactedUrl:
    @pytest.mark.parametrize("url, expected", [
        ("https://api.test/v3/x?key=s#frag", "https://api.test/v3/x"),
        ("https://user:pw@api.test:8443/p?q=1", "https://api.test:8443/p"),
        ("http://[::1]:8080/p?q=1", "http://[::1]:8080/p"),
        ("https://api.test/plain", "https://api.test/plain"),
    ])
    def test_only_scheme_host_port_and_path_remain(self, url: str, expected: str) -> None:
        assert redacted_url(url) == expected


class TestTheHttpxRecord:
    def test_its_query_is_removed_and_the_rest_kept(self) -> None:
        record = _httpx_record(httpx.URL("https://user:pw@api.test/v3/x?apikey=q-secret"))

        assert _ProviderWarningFilter().filter(record)

        assert record.getMessage() == (
            'HTTP Request: GET https://api.test/v3/x "HTTP/1.1 200 OK"'
        )

    def test_a_rate_limit_is_still_promoted(self) -> None:
        record = _httpx_record("https://api.test/x?k=q-secret", 429, "Too Many Requests")

        _ProviderWarningFilter().filter(record)

        assert record.levelno == logging.WARNING
        assert "q-secret" not in record.getMessage()

    def test_another_logger_is_untouched(self) -> None:
        record = logging.LogRecord(
            "other", logging.INFO, __file__, 1, "%s", ("https://x.test/?k=v",), None,
        )

        _ProviderWarningFilter().filter(record)

        assert record.getMessage() == "https://x.test/?k=v"

    def test_a_real_httpx_request_record_is_redacted(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.INFO, logger="httpx")
        caplog.handler.addFilter(_ProviderWarningFilter())
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))

        client.get("https://api.test/v3/x?apikey=q-secret")

        messages = [r.getMessage() for r in caplog.records if r.name == "httpx"]
        assert messages == ['HTTP Request: GET https://api.test/v3/x "HTTP/1.1 200 OK"']
