"""
Reusable HTTP server helpers for Rey Apps.

This module intentionally stays small. It wraps the standard library HTTP
server with JSON/HTML response helpers and routes access logging through
``rey_lib`` logging.
"""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rey_lib.logs import get_logger

__all__: list[str] = [
    "ReyRequestHandler",
    "first_query_value",
    "serve",
]

_logger = get_logger(__name__)


class ReyRequestHandler(BaseHTTPRequestHandler):
    """Base request handler with shared response helpers."""

    def log_message(self, format: str, *args: object) -> None:
        """Route HTTP access logs through ``rey_lib`` logging."""
        _logger.info("http %s", format % args)

    def send_json(
        self,
        payload: object,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        """Send a JSON response body."""
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(
        self,
        html: str,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        """Send an HTML response body."""
        body = html.encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_not_found(self) -> None:
        """Send a standard JSON 404 response."""
        self.send_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)


def serve(
    handler_class: type[BaseHTTPRequestHandler],
    host: str,
    port: int,
    app_name: str,
) -> None:
    """Start a threaded HTTP server for a Rey app."""
    httpd = ThreadingHTTPServer((host, port), handler_class)

    _logger.info("%s listening on http://%s:%s", app_name, host, port)
    with httpd:
        httpd.serve_forever()


def first_query_value(query: dict[str, list[str]], name: str) -> str | None:
    """Return the first query-string value for ``name``."""
    values = query.get(name, [])
    return values[0] if values else None
