"""A configured HTTP endpoint, as one shared Rey connection over corehttp.

The outbound counterpart of a database ``Connection``: declared in
``connections.yaml`` with ``provider: http``, resolved by name through the same
``ConnectionOwner``, held for the runtime and closed at its shutdown.

**corehttp is the HTTP substrate** (backlog 664/665). Its ``PipelineClient``,
``HttpRequest``, ``HttpResponse``, transport and policies are used as they are;
this module only builds one client from the connection's record and owns its
lifetime. HTTPX is reached only through corehttp's ``HttpXTransport``.

What it owns
------------
- the connection's name, as immutable identity, and its resolved configuration
- building exactly one corehttp client from that configuration -- base URL,
  default headers, timeout, TLS verification, and the authentication header,
  whose value is a secret reference read at each request
- the client's lifetime: built on first use, reused after, closed once

What it does not own
--------------------
- the operation. A caller builds a corehttp ``HttpRequest``; ``format_url``
  joins a path onto the base URL.
- interpreting a response. Any status that came back is returned as it came;
  what it means belongs to the caller and its provider adapter. Only a request
  that could not be sent or answered raises.
- retries, logging, proxies and any other policy. The policy list is exactly
  default headers and the credential.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from corehttp.exceptions import ServiceRequestError, ServiceResponseError
from corehttp.rest import HttpRequest, HttpResponse
from corehttp.runtime import PipelineClient
from corehttp.runtime.policies import HeadersPolicy, ServiceKeyCredentialPolicy
from corehttp.transport.httpx import HttpXTransport

from rey_lib.config.env_reference import resolve_env_reference
from rey_lib.errors.error_utils import ConfigError, HttpTransportError

__all__ = ["HttpConnection"]


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


def _new_transport(**options: Any) -> HttpXTransport:
    """Build the corehttp transport. The one place this module constructs it."""
    return HttpXTransport(**options)


class _EnvKeyCredential:
    """An API key read from its secret reference each time it is asked for.

    ``ServiceKeyCredentialPolicy`` reads ``key`` on every request, so the value
    is resolved at the moment of use and held by nothing between requests. The
    policy's own construction checks ``hasattr(credential, "key")``, which reads
    it once more as the client is built; that value is discarded.
    """

    def __init__(self, ctx: Any, reference: Any) -> None:
        self._ctx = ctx
        self._reference = reference

    def __repr__(self) -> str:
        return f"<_EnvKeyCredential {self._reference}>"

    @property
    def key(self) -> str:
        """The resolved key."""
        return str(resolve_env_reference(self._ctx, self._reference))


class HttpConnection:
    """One configured HTTP endpoint, its corehttp client built once and shared."""

    def __init__(self, config: Any, ctx: Any = None) -> None:
        """Hold a resolved ``provider: http`` record; build nothing yet.

        Args:
            config: A resolved ``connections[]`` record. Must carry ``name``,
                ``base_url`` and ``timeout_seconds``.
            ctx: Carried so the authentication value, a secret reference, can be
                read at each request.

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
        self._client: Optional[PipelineClient] = None

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

    def _open(self) -> PipelineClient:
        """Return the client, building it on first use.

        Building opens no socket: the transport creates its HTTPX client on the
        first send. The policy list is exactly default headers and, where auth
        is declared, the credential -- no retry, logging, proxy or User-Agent.
        """
        if self._client is None:
            headers = {
                str(key): str(value)
                for key, value in _mapping(_field(self._config, "headers")).items()
            }
            policies: list[Any] = [HeadersPolicy(base_headers=headers)]
            auth = _mapping(_field(self._config, "auth"))
            if auth:
                policies.append(ServiceKeyCredentialPolicy(
                    _EnvKeyCredential(self._ctx, auth["value"]),
                    str(auth["header"]).strip(),
                ))
            self._client = PipelineClient(
                self._base_url,
                transport=_new_transport(
                    connection_timeout=self._timeout_seconds,
                    read_timeout=self._timeout_seconds,
                    connection_verify=self._verify,
                    use_env_settings=False,
                ),
                policies=policies,
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

    # -- corehttp's surface -------------------------------------------------

    def format_url(self, path: str, **kwargs: Any) -> str:
        """``path`` joined onto the base URL; an absolute URL passes through.

        Sends nothing.
        """
        return self._open().format_url(path, **kwargs)

    def send_request(self, request: HttpRequest) -> HttpResponse:
        """Send one request and return the response, whatever its status.

        Args:
            request: A corehttp request with an absolute URL -- see
                :meth:`format_url`.

        Returns:
            The response as it came back, its body already read. A 4xx or 5xx
            is returned, not raised.

        Raises:
            HttpTransportError: If the request could not be sent or no response
                came back. The corehttp error is its cause.
        """
        try:
            return self._open().send_request(request)
        except (ServiceRequestError, ServiceResponseError) as exc:
            raise HttpTransportError(
                f"connection '{self._name}': {request.method} {request.url} "
                f"was not answered: {exc}"
            ) from exc
