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

**Adapters are a registry.** Each registers itself with :func:`http_transform_adapter`,
the way a file-level kind registers with ``@file_transform``; nothing keeps a
central list. Adapter modules live in ONE package, ``rey_lib.data.http_transform_adapters``,
imported once on the first lookup -- as ``rey_lib.files.data_file`` discovers its
formats -- so ``import rey_lib.data`` loads no adapter (backlog 667).
"""

from __future__ import annotations

import functools
import importlib
import pkgutil
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from rey_lib.data.data_transform import DataTransform, DeclaredTransform
from rey_lib.logs import get_logger

if TYPE_CHECKING:
    from rey_lib.web_utils import HttpConnection

__all__ = [
    "HTTPTransform", "HttpTransformAdapter", "http_transform_adapter",
    "http_transform_adapter_for", "http_transform_adapters",
]

_logger = get_logger(__name__)


class HttpTransformAdapter(ABC):
    """One provider's API contract, applied to a load's records.

    **A RECORD-TRANSFORM STRATEGY, NOT REY'S HTTP PROVIDER ABSTRACTION.** It
    turns a record set into a provider's HTTP operations and the answers back
    into records, for HTTPTransform and nothing else. Other HTTP consumers --
    single-resource fetches, downloads, uploads, metadata clients -- use
    ``rey_lib.web_utils`` directly and are not forced through this contract or
    its registry.
    """

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

    def options(self) -> list[dict[str, Any]]:
        """The options this adapter takes, as descriptors a surface can draw.

        Each is ``{"name", "required", "kind"}``, with ``"choices"`` and
        ``"default"`` where the option has them. ``kind`` is ``column`` (one of
        the mapped columns), ``choice`` (one of ``choices``), ``integer`` or
        ``properties`` (a mapping whose keys are among ``choices``). DESCRIPTIVE
        ONLY: validating the options stays :meth:`apply`'s (backlog 687).

        Returns:
            The declared options, in order; none where an adapter declares none.
        """
        return []

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


_ADAPTERS: dict[str, type[HttpTransformAdapter]] = {}


def http_transform_adapter(name: str) -> Callable[[type[HttpTransformAdapter]], type[HttpTransformAdapter]]:
    """Register the decorated HttpTransformAdapter under ``name``."""
    def register(cls: type[HttpTransformAdapter]) -> type[HttpTransformAdapter]:
        _ADAPTERS[name] = cls
        return cls
    return register


@functools.cache
def _discover_transform_adapters() -> None:
    """Import every module of ``rey_lib.data.http_transform_adapters`` once, so each registers.

    Lazy, on the first lookup, so importing this module -- or ``rey_lib.data`` --
    loads no adapter, and an adapter module can import from this one.
    """
    package = importlib.import_module("rey_lib.data.http_transform_adapters")
    for module in pkgutil.iter_modules(package.__path__):
        importlib.import_module(f"{package.__name__}.{module.name}")


def http_transform_adapter_for(name: str) -> Optional[HttpTransformAdapter]:
    """An instance of the adapter registered as ``name``, or None."""
    _discover_transform_adapters()
    registered = _ADAPTERS.get(name)
    return registered() if registered is not None else None


def http_transform_adapters() -> tuple[str, ...]:
    """Every registered adapter name, in registration order."""
    _discover_transform_adapters()
    return tuple(_ADAPTERS)


class HTTPTransform(DeclaredTransform):
    """A load's records, sent through one adapter over one HTTP connection."""

    def __init__(
        self,
        connection: "HttpConnection",
        adapter: HttpTransformAdapter,
        options: Optional[Mapping[str, Any]] = None,
        mapping: Optional[DataTransform] = None,
    ) -> None:
        """Hold the connection, the adapter, its options and the mapping; send nothing.

        Args:
            connection: The named HTTP connection, already resolved.
            adapter: The provider adapter.
            options: The adapter's configuration.
            mapping: The column mapping applied to the records BEFORE they are
                sent (backlog 679) -- the existing ``ColumnTransform``, built by
                its single builder -- or None for a pass-through. It is applied
                here, once; the adapter only ever sees its output.
        """
        super().__init__()
        self.connection = connection
        self.adapter = adapter
        self.options: dict[str, Any] = dict(options or {})
        self.mapping = mapping

    def transform(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The records the adapter produces from these, mapped first, through the connection."""
        _logger.debug(
            "http transform: %d record(s) through adapter %s on connection %s%s",
            len(records), type(self.adapter).__name__, self.connection.name,
            ", mapped first" if self.mapping is not None else "",
        )
        sent = self.mapping.transform(list(records)) if self.mapping is not None else list(records)
        return self.adapter.apply(sent, self.connection, dict(self.options))

    def columns_for_names(self, actual: list[str]) -> list[str]:
        """The produced columns, given the INPUT column names.

        Asked by a preview of the source's names. The mapping, where there is
        one, answers which columns it produces; the adapter answers from those
        names alone -- nothing is sent.
        """
        mapped = (
            self.mapping.columns_for_names(list(actual))
            if self.mapping is not None else list(actual)
        )
        return list(self.adapter.output_columns(list(mapped)))

    def _columns_for(self, records: list[dict[str, Any]]) -> list[str]:
        """The PRODUCED records' own columns, for ``logical_schema``."""
        return list(records[0].keys())
