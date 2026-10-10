"""One installation's governed HTTP jobs, as the Transform chooses from them (backlog 829).

    Control.http_operation(installation) -> HttpTransformSource
        -> transform_choices / choices() -> request(key) -> select_operation(key)

**The Manifest lifecycle, for HTTP.** ``ManifestSource`` reads one governed
file's context once, keeps the rows and offers the file type's saved
settings. This object does the same for the installation's HTTP jobs: ONE
control read in :meth:`create`, the rows kept, the enabled operations offered,
and the selected one's request answered from the kept rows.

**The Transform owns it.** A Transform in kind ``http`` creates this from the
Control it was handed, chooses through it and writes its own state from what it
answers; nothing outside rey_lib holds or sequences it. Removing the Console
removes none of this (rule ``loader_semantics_survive_without_the_console``).

**It owns no execution.** A preview or run resolves the Transform as it always
has, and that reads the governed job again (``load_operation``'s
``_build_http_transform``); nothing here is consulted.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from rey_lib.errors.error_utils import ConfigError
from rey_lib.logs import AppLogger
from rey_lib.web_utils.operation import HttpOperation

__all__ = ["HttpTransformSource"]

_logger = AppLogger(__name__)

#: What a configured header is, beside a request field's ``literal`` or
#: ``reference``: a header the job sends on every request, not a request field.
_CONFIGURED = "configured"


class HttpTransformSource:
    """One installation's governed HTTP jobs, and the operation selected from them.

    Built through :meth:`create`, which is where the single control call
    happens. Constructing one directly with rows is legitimate and is what a
    test does; nothing here reaches for a reader.
    """

    #: The Transform field an operation is chosen through.
    SELECTION_FIELD = "http-adapter"

    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        """Keep the jobs the one read returned; nothing is selected.

        Args:
            rows: The installation's jobs, one row each, as
                ``control.http_operation_vw`` shapes them and in the order the
                read returned them.
        """
        #: The rows the ONE read returned, kept: choosing between operations
        #: reads nothing.
        self._rows: list[dict[str, Any]] = [dict(row) for row in rows]
        #: The enabled operations, by key, in the order the read returned them.
        self.transform_choices: list[str] = [
            str(row["adapter_key"]) for row in self._rows if row.get("is_enabled")
        ]
        #: The selected operation's key, or None while unresolved.
        self.selected_operation: Optional[str] = None

    @classmethod
    def create(cls, control: Any, installation: str) -> "HttpTransformSource":
        """Read the installation's HTTP jobs and return the source for them.

        **EXACTLY ONE control call**, made here.

        Args:
            control: What answers ``http_operation``.
            installation: The installation whose jobs these are.

        Returns:
            The source, with nothing selected.
        """
        _logger.debug("http transform source: reading the HTTP jobs of installation %s",
                      installation)
        return cls(control.http_operation(str(installation)))

    def choices(self) -> list[dict[str, str]]:
        """The enabled operations as a field offers them: value and label, the key."""
        return [{"value": key, "label": key} for key in self.transform_choices]

    def request(self, adapter_key: str) -> dict[str, Any]:
        """One enabled operation's request, as plain display facts. Nothing changes.

        The operation is hydrated from its kept row -- the one place that pairs
        the row's arrays and refuses a job that cannot execute -- its request
        read off it, and the operation dropped. Asked BEFORE anything is
        selected, so a refusal leaves the selection as it was.

        Raises:
            ConfigError: If the key is not one of the enabled operations, or its
                job cannot be executed.
        """
        key = self._enabled(adapter_key)
        row = next(one for one in self._rows if str(one["adapter_key"]) == key)
        return _request_of(HttpOperation.hydrate(row))

    def select_operation(self, adapter_key: str) -> None:
        """Make one operation the selected one, in memory. Nothing is read.

        Raises:
            ConfigError: If the key is not one of the enabled operations.
        """
        self.selected_operation = self._enabled(adapter_key)

    def _enabled(self, adapter_key: str) -> str:
        """The key, refused unless it is one of the enabled operations."""
        key = str(adapter_key or "")
        if key not in self.transform_choices:
            _logger.debug("http transform source: refused -- operation %s is not one of %d enabled",
                          key, len(self.transform_choices))
            raise ConfigError(
                f"HTTP operation '{key}' is not an enabled operation of this "
                f"installation. Operations: {', '.join(self.transform_choices) or 'none'}."
            )
        return key


def _request_of(operation: HttpOperation) -> dict[str, Any]:
    """The operation's request, as plain display facts. No credential is in it.

    Fields keep the operation's order -- body, query, then header, each as the
    job orders them. Configured headers are ordered by name: a JSON object
    states no order of its own. Each row states its own part and kind, so a
    reader shows them without deciding what they are. The authentication
    header and its secret reference are never included.
    """
    return {
        "method": operation.http_method,
        "fields": [
            {"part": one.part, "name": one.name, "kind": one.kind, "value": one.value}
            for one in operation.request_fields
        ],
        "headers": [
            {"part": "header", "name": str(name), "kind": _CONFIGURED, "value": value}
            for name, value in sorted((operation.default_headers or {}).items())
        ],
    }
