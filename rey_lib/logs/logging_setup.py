"""Legacy logging access: get_logger, the transitional way in to AppLogger.

Logging configuration and call depth are AppLogger's
(rey_lib.logs.app_logger.AppLogger; backlog logging.app_logger_canonical_object).
setup_logging, log_enter and log_exit were removed in S6; get_logger returns
AppLogger(name) until S7 moves its callers to AppLogger(__name__) and deletes it.
"""

from __future__ import annotations

from rey_lib.logs.app_logger import AppLogger


def get_logger(name: str) -> AppLogger:
    """Return ``AppLogger(name)`` (compatibility wrapper, deleted in S7).

    Existing callers keep calling this and now hold the canonical object; S7
    moves them to ``AppLogger(__name__)`` directly.

    Parameters
    ----------
    name : str
        Logger name. Conventionally the module's __name__.

    Returns
    -------
    AppLogger
        The application logger for ``name``.
    """
    return AppLogger(name)
