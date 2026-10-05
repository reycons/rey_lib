"""Records sent through an HTTP service and back: the record-to-HTTP boundary.

    DataObject -> HTTPTransform -> DataObject

**HTTPTransform is only the boundary** (backlog 671). It hands the load's
records, a named HTTP connection and the transform's options to a provider
adapter, and returns what the adapter produced. It owns no execution machinery
and no correlation.

**The adapter owns the provider** (backlog 664). Batching, item correlation,
rate limits, pagination, retry and response interpretation are an adapter's,
because they are the provider's API contract, not HTTP's. An adapter reaches the
service only through Rey's public HTTP surface -- ``rey_lib.web_utils``
``HttpRequest``, ``connection.format_url()`` and ``connection.send_request()``
-- with paths relative to the connection's base URL, and never asks an
authenticated connection to follow redirects.

**Adapters are a registry.** Each registers itself with :func:`http_adapter`,
the way a file-level kind registers with ``@file_transform``; nothing keeps a
central list. Adapter modules live in ONE package, ``rey_lib.data.http_adapters``,
imported once on the first lookup -- as ``rey_lib.files.data_file`` discovers its
formats -- so ``import rey_lib.data`` loads no adapter (backlog 667).
"""

from __future__ import annotations

import functools
import importlib
import pkgutil
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from rey_lib.data.data_transform import DeclaredTransform
from rey_lib.logs import get_logger

if TYPE_CHECKING:
    from rey_lib.web_utils import HttpConnection

__all__ = [
    "HTTPTransform", "HttpAdapter", "http_adapter", "http_adapter_for", "http_adapters",
]

_logger = get_logger(__name__)


class HttpAdapter(ABC):
    """One provider's API contract, applied to a load's records."""

    @abstractmethod
    def output_columns(self, input_columns: list[str]) -> list[str]:
        """The columns this adapter produces from records with these columns.

        DESCRIPTIVE ONLY: answered from the names alone. It sends no request
        and reads no response, so asking it -- as a preview does for its
        header -- never becomes a second outbound operation.

        Args:
            input_columns: The input records' column names, in order.

        Returns:
            The produced records' column names, in order.
        """

    @abstractmethod
    def apply(
        self,
        records: list[dict[str, Any]],
        connection: "HttpConnection",
        options: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Send the records through the provider and return what it produced.

        Args:
            records: The input records, in order.
            connection: The named HTTP connection. Reached only through
                ``format_url``, ``send_request`` and ``HttpRequest``.
            options: This transform's provider configuration.

        Returns:
            The produced records, carrying :meth:`output_columns`.
        """


_ADAPTERS: dict[str, type[HttpAdapter]] = {}


def http_adapter(name: str) -> Callable[[type[HttpAdapter]], type[HttpAdapter]]:
    """Register the decorated HttpAdapter under ``name``."""
    def register(cls: type[HttpAdapter]) -> type[HttpAdapter]:
        _ADAPTERS[name] = cls
        return cls
    return register


@functools.cache
def _discover_adapters() -> None:
    """Import every module of ``rey_lib.data.http_adapters`` once, so each registers.

    Lazy, on the first lookup, so importing this module -- or ``rey_lib.data`` --
    loads no adapter, and an adapter module can import from this one.
    """
    package = importlib.import_module("rey_lib.data.http_adapters")
    for module in pkgutil.iter_modules(package.__path__):
        importlib.import_module(f"{package.__name__}.{module.name}")


def http_adapter_for(name: str) -> Optional[HttpAdapter]:
    """An instance of the adapter registered as ``name``, or None."""
    _discover_adapters()
    registered = _ADAPTERS.get(name)
    return registered() if registered is not None else None


def http_adapters() -> tuple[str, ...]:
    """Every registered adapter name, in registration order."""
    _discover_adapters()
    return tuple(_ADAPTERS)


class HTTPTransform(DeclaredTransform):
    """A load's records, sent through one adapter over one HTTP connection."""

    def __init__(
        self,
        connection: "HttpConnection",
        adapter: HttpAdapter,
        options: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Hold the connection, the adapter and its options; send nothing.

        Args:
            connection: The named HTTP connection, already resolved.
            adapter: The provider adapter.
            options: The adapter's configuration.
        """
        super().__init__()
        self.connection = connection
        self.adapter = adapter
        self.options: dict[str, Any] = dict(options or {})

    def transform(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The records the adapter produces from these, through the connection."""
        _logger.debug(
            "http transform: %d record(s) through adapter %s on connection %s",
            len(records), type(self.adapter).__name__, self.connection.name,
        )
        return self.adapter.apply(list(records), self.connection, dict(self.options))

    def columns_for_names(self, actual: list[str]) -> list[str]:
        """The produced columns, given the INPUT column names.

        Asked by a preview of the source's names. The adapter answers from the
        names alone -- nothing is sent.
        """
        return list(self.adapter.output_columns(list(actual)))

    def _columns_for(self, records: list[dict[str, Any]]) -> list[str]:
        """The PRODUCED records' own columns, for ``logical_schema``."""
        return list(records[0].keys())
