"""HTTP tester: manual tooling for proving the HTTP layer against a real service.

    rey-http-tester -> shared_connection -> HttpConnection -> corehttp -> HTTPX

**A TESTER, NOT A CLIENT SURFACE** (backlog 672). Nothing in a provider, a
transform or an application's runtime calls this module; it is not exported from
``rey_lib.web_utils``. It uses only the public HTTP layer, so what it proves is
what an application gets.

``exchange`` sends one request and reports what came back; ``main`` is the
``rey-http-tester`` command around it. A future Console test panel would call
``exchange``.

Usage
-----
    rey-http-tester --config-path <installation config.yaml> [--app rey_console]
                    --connection NAME [--method GET] [--query k=v ...]
                    [--header k=v ...] [--json TEXT | --body TEXT | --body-file PATH]
                    [--follow-redirects] [--timeout SECONDS] PATH_OR_URL

Prints status, elapsed time, response headers and body. Request headers are
never printed: the credential is added by the connection. The final URL after a
redirect is not shown -- corehttp's public response reports the request's URL;
an unfollowed 3xx shows its Location header instead.

Exit codes: 0 a response came back (any status); 1 the request was refused or
not answered; 2 a usage or configuration error.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from rey_lib.errors.error_utils import ConfigError, HttpTransportError
from rey_lib.logs import get_logger
from rey_lib.web_utils.connection import HttpConnection
from rey_lib.web_utils import HttpRequest

__all__ = ["Exchange", "exchange", "main"]

_logger = get_logger(__name__)


@dataclass(frozen=True)
class Exchange:
    """What one tester request got back."""

    status: int
    reason: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    content_type: str = ""
    elapsed_ms: float = 0.0


def exchange(
    connection: HttpConnection,
    method: str,
    target: str,
    *,
    query: Optional[Mapping[str, str]] = None,
    headers: Optional[Mapping[str, str]] = None,
    json_body: Any = None,
    content: Optional[str | bytes] = None,
    follow_redirects: bool = False,
    timeout_seconds: Optional[float] = None,
) -> Exchange:
    """Send one request through ``connection`` and report what came back.

    Args:
        connection: The HTTP connection to test.
        method: The HTTP method.
        target: A path relative to the base URL, or an absolute URL.
        query: Query parameters.
        headers: Request headers, beside the connection's defaults.
        json_body: A body sent as JSON.
        content: A raw body.
        follow_redirects: Ask corehttp to follow redirects for this call. An
            authenticated connection refuses it.
        timeout_seconds: Per-call connect and read timeout.

    Returns:
        Status, reason, response headers, body and the wall-clock time of the
        send.

    Raises:
        ConfigError: The connection refused the request.
        HttpTransportError: The request could not be sent or was not answered.
    """
    options: dict[str, Any] = {}
    if follow_redirects:
        options["follow_redirects"] = True
    if timeout_seconds is not None:
        options["connection_timeout"] = timeout_seconds
        options["read_timeout"] = timeout_seconds
    request = HttpRequest(
        method.upper(),
        connection.format_url(target),
        params=dict(query) if query else None,
        headers=dict(headers) if headers else None,
        json=json_body,
        content=content,
    )
    started = time.perf_counter()
    response = connection.send_request(request, **options)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return Exchange(
        status=response.status_code,
        reason=str(response.reason or ""),
        headers=dict(response.headers),
        body=response.content,
        content_type=str(response.content_type or ""),
        elapsed_ms=elapsed_ms,
    )


def render(result: Exchange) -> str:
    """The exchange as text: status, elapsed, headers, then the body."""
    lines = [
        f"Status:  {result.status} {result.reason}".rstrip(),
        f"Elapsed: {result.elapsed_ms:.0f} ms",
        "",
        "Headers",
        *(f"  {name}: {value}" for name, value in result.headers.items()),
        "",
        "Body",
    ]
    text = result.body.decode("utf-8", errors="replace")
    if "json" in result.content_type.lower():
        try:
            text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except ValueError:
            pass
    lines.append(text)
    return "\n".join(lines) + "\n"


def _pairs(values: list[str], option: str) -> dict[str, str]:
    """``k=v`` arguments as a mapping."""
    pairs: dict[str, str] = {}
    for value in values:
        key, sep, item = value.partition("=")
        if not sep or not key.strip():
            raise ConfigError(f"{option} takes key=value, got '{value}'.")
        pairs[key.strip()] = item
    return pairs


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Build and parse the tester's arguments."""
    parser = argparse.ArgumentParser(
        prog="rey-http-tester",
        description="HTTP TESTER: send one request through a named Rey HTTP "
                    "connection and show what came back. Manual tooling only.",
    )
    parser.add_argument("--config-path", required=True, dest="config_path",
                        help="Path to the installation config.yaml.")
    parser.add_argument("--app", default="rey_console",
                        help="App whose context is built (default: rey_console).")
    parser.add_argument("--connection", required=True,
                        help="Name of a provider: http connection.")
    parser.add_argument("--method", default="GET", help="HTTP method (default: GET).")
    parser.add_argument("--query", action="append", default=[], metavar="K=V",
                        help="Query parameter; repeatable.")
    parser.add_argument("--header", action="append", default=[], metavar="K=V",
                        help="Request header; repeatable.")
    body = parser.add_mutually_exclusive_group()
    body.add_argument("--json", dest="json_text", help="JSON body text.")
    body.add_argument("--body", help="Raw body text.")
    body.add_argument("--body-file", dest="body_file", help="Raw body read from a file.")
    parser.add_argument("--follow-redirects", action="store_true", dest="follow_redirects",
                        help="Follow redirects (refused on an authenticated connection).")
    parser.add_argument("--timeout", type=float, default=None,
                        help="Per-call connect and read timeout in seconds.")
    parser.add_argument("target", help="Path relative to base_url, or an absolute URL.")
    return parser.parse_args(argv)


def _connection(config_path: str, app: str, name: str) -> HttpConnection:
    """The shared connection named ``name``, which must be an HTTP connection."""
    from rey_lib.config.config_context import build_ctx_from_path  # noqa: PLC0415
    from rey_lib.db.connection import shared_connection  # noqa: PLC0415

    ctx = build_ctx_from_path(Path(config_path), app_name=app)
    connection = shared_connection(ctx, name)
    if not isinstance(connection, HttpConnection):
        raise ConfigError(
            f"connection '{name}' is a {connection.provider} connection, not http."
        )
    return connection


def main(argv: list[str] | None = None) -> int:
    """Run the HTTP tester. Returns the process exit code."""
    args = _parse_args(argv)
    try:
        query = _pairs(args.query, "--query")
        headers = _pairs(args.header, "--header")
        json_body = json.loads(args.json_text) if args.json_text is not None else None
        content: Optional[str] = args.body
        if args.body_file:
            from rey_lib.files.file_utils import read_text_file  # noqa: PLC0415

            content = read_text_file(args.body_file)
        connection = _connection(args.config_path, args.app, args.connection)
    except (ConfigError, ValueError) as exc:
        _logger.error("http tester: %s", exc)
        return 2
    try:
        result = exchange(
            connection, args.method, args.target,
            query=query, headers=headers, json_body=json_body, content=content,
            follow_redirects=args.follow_redirects, timeout_seconds=args.timeout,
        )
    except (ConfigError, HttpTransportError) as exc:
        _logger.error("http tester: %s", exc)
        return 1
    finally:
        connection.close()
    sys.stdout.write(render(result))
    return 0
