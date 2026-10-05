"""A configured HTTP endpoint, as one shared Rey connection.

The outbound counterpart of a database ``Connection``: declared in
``connections.yaml`` with ``provider: http``, resolved by name through the same
``ConnectionOwner``, held for the runtime and closed at its shutdown. A
transform asks for it by name and sends its requests through it.

What it owns
------------
- the connection's name, as immutable identity, and its resolved configuration
- the base URL, default headers, timeout and TLS verification it was declared with
- the authentication header, whose value is a secret reference read when the
  client is built
- the client's lifetime: built on the first request, reused after, closed once

What it does not own
--------------------
- the operation. Method, path, query and body are the caller's; this sends what
  it is given.
- interpreting a response. Any status that came back is returned as it came;
  what a 404 or a provider error payload means belongs to the transform and its
  provider adapter. Only a request that could not be sent or answered raises.
- retries, batching and rate limits, which belong to the transform's execution.

The transport is HTTPX, and it stays inside this module: callers see
``HttpConnection`` and ``HttpResponse`` and never an HTTPX object.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

import httpx

from rey_lib.config.env_reference import resolve_env_reference
from rey_lib.errors.error_utils import ConfigError, HttpTransportError

__all__ = ["HttpConnection", "HttpResponse"]


@dataclass(frozen=True)
class HttpResponse:
    """One response, as it came back. Any status; nothing interpreted."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> Any:
        """The body read as JSON.

        Raises:
            ValueError: If the body is not JSON.
        """
        return json.loads(self.body)


def _field(record: Any, name: str) -> Any:
    """One field of a configuration record, whether a Namespace or a dict."""
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name, None)


def _mapping(value: Any) -> dict[str, Any]:
    """A configured mapping as a plain dict, whether a Namespace or a dict."""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    return dict(vars(value))


def _new_client(**options: Any) -> httpx.Client:
    """Build the HTTPX client. The one place this module constructs it."""
    return httpx.Client(**options)


class HttpConnection:
    """One configured HTTP endpoint, its client built once and shared."""

    def __init__(self, config: Any, ctx: Any = None) -> None:
        """Hold a resolved ``provider: http`` record; open nothing yet.

        Args:
            config: A resolved ``connections[]`` record. Must carry ``name``,
                ``base_url`` and ``timeout_seconds``.
            ctx: Carried so the authentication value, a secret reference, can be
                read when the client is built.

        Raises:
            ConfigError: If a required field is missing or malformed.
        """
        name = str(_field(config, "name") or "").strip()
        if not name:
            raise ConfigError("connection: a connection config must carry a name.")
        base_url = str(_field(config, "base_url") or "").strip()
        if not base_url:
            raise ConfigError(f"connection '{name}': an http connection needs base_url.")
        timeout = _field(config, "timeout_seconds")
        if timeout is None or isinstance(timeout, bool):
            raise ConfigError(
                f"connection '{name}': an http connection needs timeout_seconds, "
                "so a stalled request cannot hang a run."
            )
        try:
            timeout_seconds = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"connection '{name}': timeout_seconds must be a number of seconds."
            ) from exc
        if timeout_seconds <= 0:
            raise ConfigError(f"connection '{name}': timeout_seconds must be positive.")
        verify = _field(config, "verify")
        if verify is not None and not isinstance(verify, bool):
            raise ConfigError(f"connection '{name}': verify must be true or false.")
        auth = _mapping(_field(config, "auth"))
        if auth and not (str(auth.get("header") or "").strip() and auth.get("value")):
            raise ConfigError(
                f"connection '{name}': auth needs both header and value."
            )

        self._name = name
        self._config = config
        self._ctx = ctx
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds
        self._verify = True if verify is None else verify
        self._client: Optional[httpx.Client] = None

    def __repr__(self) -> str:
        state = "open" if self._client is not None else "closed"
        return f"<HttpConnection {self._name} {self._base_url} {state}>"

    # -- identity -----------------------------------------------------------

    @property
    def name(self) -> str:
        """The configured connection name. Immutable identity."""
        return self._name

    @property
    def provider(self) -> str:
        """Always ``http``: the provider this connection was declared with."""
        return "http"

    @property
    def llm(self) -> bool:
        """Never offered to a language model as a queryable connection."""
        return False

    @property
    def config(self) -> Any:
        """The resolved configuration this connection was built from."""
        return self._config

    @property
    def base_url(self) -> str:
        """The shared host and API prefix every request path is relative to."""
        return self._base_url

    @property
    def is_open(self) -> bool:
        """Whether a client is currently held."""
        return self._client is not None

    # -- lifecycle ----------------------------------------------------------

    def _open(self) -> httpx.Client:
        """Return the client, building it on first use.

        The authentication value is resolved here, as the client is built. It is
        held by that client until ``close`` -- HTTPX must hold the header to send
        it -- and is never written to the configuration or to this object.
        """
        if self._client is None:
            headers = {
                str(key): str(value)
                for key, value in _mapping(_field(self._config, "headers")).items()
            }
            auth = _mapping(_field(self._config, "auth"))
            if auth:
                headers[str(auth["header"]).strip()] = str(
                    resolve_env_reference(self._ctx, auth["value"])
                )
            self._client = _new_client(
                base_url=self._base_url,
                headers=headers,
                timeout=self._timeout_seconds,
                verify=self._verify,
            )
        return self._client

    def close(self) -> None:
        """Close the client if one is held. Safe to call more than once.

        Owned by runtime shutdown, as a database connection's close is: this
        object is shared, and a consumer closing it would take the client away
        from every other holder.
        """
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.close()
        except Exception:  # noqa: BLE001 -- a failed close must not mask shutdown.
            pass

    # -- the request primitive ----------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json: Any = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> HttpResponse:
        """Send one request and return the response, whatever its status.

        Args:
            method: The HTTP method.
            path: Relative to ``base_url``.
            params: Query parameters.
            json: A body sent as JSON.
            headers: Headers for this request, beside the connection's defaults.

        Returns:
            The response as it came back. A 4xx or 5xx is returned, not raised.

        Raises:
            HttpTransportError: If the request could not be sent or no response
                came back -- DNS, connect, timeout, protocol.
        """
        try:
            answered = self._open().request(
                method, path, params=params, json=json, headers=headers,
            )
        except httpx.HTTPError as exc:
            raise HttpTransportError(
                f"connection '{self._name}': {method} {path} was not answered: {exc}"
            ) from exc
        return HttpResponse(
            status=answered.status_code,
            headers=dict(answered.headers),
            body=answered.content,
        )
