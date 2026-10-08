"""AppLogger, the application logging object.

``AppLogger(name)`` is how application code logs. It wraps the stdlib logger of
that name, so every record still travels the one Python logging pipeline and
its handlers; this object adds no second path.

The public surface is what application code is allowed to do: ``debug``,
``info``, ``warning`` and ``isEnabledFor``. There is no ``error``,
``exception``, ``critical`` or ``setLevel`` -- an ERROR record is AppError's to
emit (rule 79), and configuring the logging pipeline is not a caller's job.

Records are attributed to the caller, not to this wrapper: each method raises
the stdlib ``stacklevel`` by one, so ``funcName``, ``lineno`` and ``pathname``
name the line that called ``AppLogger``.
"""

from __future__ import annotations

import logging
from typing import Any


class AppLogger:
    """The application logger for one logger name.

    Args:
        name: Logger name. Conventionally the module's ``__name__``.
    """

    __slots__ = ("_logger",)

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    def debug(self, msg: object, *args: Any, **kwargs: Any) -> None:
        """Log ``msg % args`` at DEBUG; keyword arguments such as ``extra`` pass through."""
        self._logger.debug(msg, *args, **_from_caller(kwargs))

    def info(self, msg: object, *args: Any, **kwargs: Any) -> None:
        """Log ``msg % args`` at INFO; keyword arguments such as ``extra`` pass through."""
        self._logger.info(msg, *args, **_from_caller(kwargs))

    def warning(self, msg: object, *args: Any, **kwargs: Any) -> None:
        """Log ``msg % args`` at WARNING; keyword arguments such as ``extra`` pass through."""
        self._logger.warning(msg, *args, **_from_caller(kwargs))

    def isEnabledFor(self, level: int) -> bool:  # noqa: N802 -- the stdlib name callers use
        """Return whether a record at ``level`` would be processed by this logger."""
        return self._logger.isEnabledFor(level)


def _from_caller(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return ``kwargs`` with ``stacklevel`` raised past this wrapper's frame."""
    return {**kwargs, "stacklevel": kwargs.get("stacklevel", 1) + 1}
