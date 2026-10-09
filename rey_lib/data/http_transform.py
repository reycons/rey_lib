"""Records sent through a governed HTTP job and back: the record-to-HTTP boundary.

    DataObject -> HTTPTransform -> DataObject

**HTTPTransform is only the boundary** (backlogs 671, 813). It hands the load's
records -- mapped first, where a mapping is authored -- to one ``HttpOperation``
and returns what the operation produced. It owns no execution machinery and no
correlation.

**The operation owns the job** (backlog 813). Its complete configuration --
connection, request, response, batching, retry, pagination, correlation -- is
one governed row read through ``Control.http_operation``; ``HttpOperation``
executes it. No provider is named here and there is no adapter registry: an
operation is rows, resolved by its ``adapter_key``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from rey_lib.data.data_transform import DataTransform, DeclaredTransform
from rey_lib.logs import AppLogger

if TYPE_CHECKING:
    from rey_lib.web_utils.operation import HttpOperation

__all__ = ["HTTPTransform"]

_logger = AppLogger(__name__)


class HTTPTransform(DeclaredTransform):
    """A load's records, sent through one governed HTTP operation."""

    def __init__(
        self,
        operation: "HttpOperation",
        ctx: Any,
        mapping: Optional[DataTransform] = None,
    ) -> None:
        """Hold the operation, the runtime context and the mapping; send nothing.

        Args:
            operation: The hydrated HTTP job.
            ctx: The runtime context the job's connection resolves its secret
                reference through.
            mapping: The column mapping applied to the records BEFORE they are
                sent (backlog 679) -- the existing ``ColumnTransform``, built by
                its single builder -- or None for a pass-through. It is applied
                here, once; the operation only ever sees its output.
        """
        super().__init__()
        self.operation = operation
        self._ctx = ctx
        self.mapping = mapping

    def transform(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The records the operation produces from these, mapped first."""
        _logger.debug(
            "http transform: %d record(s) through operation %s%s",
            len(records), self.operation.adapter_key,
            ", mapped first" if self.mapping is not None else "",
        )
        sent = self.mapping.transform(list(records)) if self.mapping is not None else list(records)
        return self.operation.execute(sent, self._ctx)

    def columns_for_names(self, actual: list[str]) -> list[str]:
        """The produced columns, given the INPUT column names.

        Asked by a preview of the source's names. The mapping, where there is
        one, answers which columns it produces; the operation answers from those
        names alone -- nothing is sent.
        """
        mapped = (
            self.mapping.columns_for_names(list(actual))
            if self.mapping is not None else list(actual)
        )
        return list(self.operation.output_columns(list(mapped)))

    def _columns_for(self, records: list[dict[str, Any]]) -> list[str]:
        """The PRODUCED records' own columns, for ``logical_schema``."""
        return list(records[0].keys())
