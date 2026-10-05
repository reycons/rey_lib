"""A configured HTTP endpoint, as one shared Rey connection over corehttp.

The outbound counterpart of a database ``Connection``: declared in
``connections.yaml`` with ``provider: http``, resolved by name through the same
``ConnectionOwner``, held for the runtime and closed at its shutdown.

**corehttp is the HTTP substrate** (backlogs 664-666). Its ``PipelineClient``,
``HttpRequest``, ``HttpResponse``, transport and policies are used as they are;
this module builds one client from the connection's record and owns its
lifetime. HTTPX is reached only through corehttp's ``HttpXTransport``.

What it owns
------------
- the connection's name, as immutable identity, and its resolved configuration
- building exactly one corehttp client from that configuration -- base URL,
  default headers, timeout, TLS verification, and the authentication header,
  whose value is a secret reference read at each request
- the client's lifetime: built on first use, reused after, closed once
- the credential's boundary: an authenticated connection sends its credential
  only to the HTTPS origin its base URL declares, never through a redirect
- keeping the shared client stateless: it stores no cookies

What it does not own
--------------------
- the operation. A caller builds a corehttp ``HttpRequest``; ``format_url``
  joins a path onto the base URL; per-call options go to corehttp unchanged.
- interpreting a response. Any status that came back is returned as it came.
  Only a request that could not be sent or answered raises.
- retries, proxies, logging and tracing policies. The policy list is exactly
  default headers and the credential.
"""

from __future__ import annotations

import http.cookiejar
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

import httpx
from corehttp.exceptions import ServiceRequestError, ServiceResponseError
from corehttp.rest import HttpRequest, HttpResponse
from corehttp.runtime import PipelineClient
from corehttp.runtime.policies import HeadersPolicy, ServiceKeyCredentialPolicy
from corehttp.transport import HttpTransport
from corehttp.transport.httpx import HttpXTransport

from rey_lib.config.env_reference import resolve_env_reference
from rey_lib.errors.error_utils import ConfigError, HttpTransportError
from rey_lib.logs import get_logger
from rey_lib.logs.logging_setup import redacted_url

__all__ = ["HttpConnection"]

_logger = get_logger(__name__)

#: The port a scheme means when a URL names none.
_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


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


def _origin(url: Any) -> tuple[str, str, Optional[int]]:
    """Normalised scheme, host and effective port of ``url``."""
    parts = urlsplit(str(url))
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    return scheme, (parts.hostname or "").lower(), port or _DEFAULT_PORTS.get(scheme)


def _refusing_jar() -> http.cookiejar.CookieJar:
    """A cookie jar that accepts no cookie, so the shared client keeps none."""
    return http.cookiejar.CookieJar(
        policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[]),
    )


def _new_wire_client(**options: Any) -> httpx.Client:
    """Build the HTTPX client. The one place this module constructs it."""
    return httpx.Client(**options)


def _new_transport(*, timeout_seconds: float, verify: Any) -> HttpXTransport:
    """Build the corehttp transport over a client that keeps no cookies.

    ``HttpXTransport`` has no cookie option, so the HTTPX client is built here
    and handed over through corehttp's own ``client=`` parameter -- the only
    reason this module builds it. Environment proxies and certificates are off
    (``trust_env=False``) and redirects are not followed unless a call asks.
    """
    return HttpXTransport(
        client=_new_wire_client(
            trust_env=False,
            verify=verify,
            cookies=_refusing_jar(),
            follow_redirects=False,
        ),
        client_owner=True,
        connection_timeout=timeout_seconds,
        read_timeout=timeout_seconds,
    )


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

    def __init__(
        self,
        config: Any,
        ctx: Any = None,
        *,
        transport: Optional[HttpTransport] = None,
    ) -> None:
        """Hold a resolved ``provider: http`` record; build nothing yet.

        Args:
            config: A resolved ``connections[]`` record. Must carry ``name``,
                ``base_url`` and ``timeout_seconds``.
            ctx: Carried so the authentication value, a secret reference, can be
                read at each request.
            transport: A corehttp transport to send through instead of the one
                this connection would build -- the supported way to answer
                requests in a test. It is BORROWED: this connection never closes
                it.

        Raises:
            ConfigError: If a required field is missing or malformed, or auth is
                declared on a base URL that is not https.
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
        if verify is None:
            verify = True
        elif isinstance(verify, str):
            if not verify.strip() or not Path(verify).exists():
                raise ConfigError(
                    f"connection '{name}': verify names a CA bundle that does not "
                    f"exist: {verify}"
                )
        elif not isinstance(verify, bool):
            raise ConfigError(
                f"connection '{name}': verify must be true, false or a CA-bundle path."
            )
        auth = _mapping(_field(config, "auth"))
        if auth:
            if not (str(auth.get("header") or "").strip() and auth.get("value")):
                raise ConfigError(
                    f"connection '{name}': auth needs both header and value."
                )
            prefix = auth.get("prefix")
            if prefix is not None and not str(prefix).strip():
                raise ConfigError(
                    f"connection '{name}': auth.prefix, when given, must not be empty."
                )
            if _origin(base_url)[0] != "https":
                raise ConfigError(
                    f"connection '{name}': auth is sent only over https; base_url "
                    f"is {redacted_url(base_url)}."
                )

        self._name = name
        self._config = config
        self._ctx = ctx
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds
        self._verify = verify
        self._auth = auth
        self._supplied = transport
        self._client: Optional[PipelineClient] = None

    def __repr__(self) -> str:
        state = "open" if self._client is not None else "closed"
        return f"<HttpConnection {self._name} {redacted_url(self._base_url)} {state}>"

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

        Building opens no socket. The policy list is exactly default headers
        and, where auth is declared, the credential -- no retry, logging, proxy
        or User-Agent.
        """
        if self._client is None:
            headers = {
                str(key): str(value)
                for key, value in _mapping(_field(self._config, "headers")).items()
            }
            policies: list[Any] = [HeadersPolicy(base_headers=headers)]
            if self._auth:
                prefix = self._auth.get("prefix")
                policies.append(ServiceKeyCredentialPolicy(
                    _EnvKeyCredential(self._ctx, self._auth["value"]),
                    str(self._auth["header"]).strip(),
                    prefix=str(prefix).strip() if prefix is not None else None,
                ))
            self._client = PipelineClient(
                self._base_url,
                transport=self._supplied or _new_transport(
                    timeout_seconds=self._timeout_seconds, verify=self._verify,
                ),
                policies=policies,
            )
        return self._client

    def close(self) -> None:
        """Close the client if one is held. Safe to call more than once.

        Owned by runtime shutdown, as a database connection's close is: this
        object is shared, and a consumer closing it would take the client away
        from every other holder. A supplied transport is borrowed: it is
        released, never closed.
        """
        client, self._client = self._client, None
        if client is None or self._supplied is not None:
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

    def _refuse(self, request: HttpRequest, why: str) -> ConfigError:
        """Log the refusal context and return the error to raise."""
        _logger.debug(
            "http connection %s: refused %s %s (%s)",
            self._name, request.method, redacted_url(request.url), why,
        )
        return ConfigError(
            f"connection '{self._name}': {request.method} "
            f"{redacted_url(request.url)} refused: {why}."
        )

    def send_request(
        self,
        request: HttpRequest,
        *,
        stream: bool = False,
        **kwargs: Any,
    ) -> HttpResponse:
        """Send one request and return the response, whatever its status.

        corehttp's own signature: per-call options (``connection_timeout``,
        ``read_timeout``, ``follow_redirects``, ...) go to corehttp unchanged.

        Args:
            request: A corehttp request with an absolute URL -- see
                :meth:`format_url`.
            stream: Return before the body is read; the caller iterates and
                closes the response, as corehttp's contract says.
            **kwargs: Per-call options, passed through.

        Returns:
            The response as it came back. A 4xx or 5xx -- and a 3xx not
            followed -- is returned, not raised.

        Raises:
            ConfigError: When auth is declared and the request would carry the
                credential outside the base URL's HTTPS origin, or asks to
                follow redirects (a custom credential header follows a redirect
                to any host).
            HttpTransportError: If the request could not be sent or no response
                came back. The corehttp error is its cause.
        """
        if self._auth:
            if _origin(request.url) != _origin(self._base_url):
                raise self._refuse(
                    request, "the credential is sent only to the base_url origin",
                )
            if kwargs.get("follow_redirects"):
                raise self._refuse(
                    request,
                    "an authenticated connection does not follow redirects",
                )
        _logger.debug(
            "http connection %s: %s %s",
            self._name, request.method, redacted_url(request.url),
        )
        try:
            return self._open().send_request(request, stream=stream, **kwargs)
        except (ServiceRequestError, ServiceResponseError) as exc:
            _logger.debug(
                "http connection %s: %s %s not answered (%s)",
                self._name, request.method, redacted_url(request.url),
                type(exc).__name__,
            )
            raise HttpTransportError(
                f"connection '{self._name}': {request.method} "
                f"{redacted_url(request.url)} was not answered "
                f"({type(exc).__name__})."
            ) from exc
