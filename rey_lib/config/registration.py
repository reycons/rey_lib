"""Which Python applications are installed, and what each one publishes.

Installing a distribution is what registers an application. Bootstrap asks the
environment rather than reading a list someone maintained by hand, so an
application that is not installed cannot be configured into existence and one
that is removed disappears without an edit.

**Discovery is existence and capability. It is not membership.** Which of the
installed applications an installation actually has is that installation's own
answer, declared in its ``apps:`` collection. A shared virtual environment
serves several installations, so discovery alone would give every one of them
every application.

**One entry point per application**, never one per operation -- packaging is how
an application announces itself, not how a workflow dispatches.

This module owns the packaging vocabulary. Nothing else imports
``importlib.metadata``, so what an application publishes and how it was found
stay separable.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any

from rey_lib.errors.error_utils import ConfigError
from rey_lib.logs.logging_setup import get_logger

__all__ = ["REGISTRATION_GROUP", "discover_registrations"]

logger = get_logger(__name__)

#: The entry-point group an application publishes itself under. One place knows
#: the word.
REGISTRATION_GROUP = "rey.applications"


def discover_registrations() -> dict[str, dict[str, Any]]:
    """Return every installed application's registration, keyed by name.

    Returns:
        Application name to what that application published.

    Raises:
        ConfigError: If a registration cannot be loaded, does not answer with a
            mapping, names no application, or two registrations claim one name.
            Every one of those is refused rather than skipped: a registration
            that fails quietly becomes an application that silently is not
            there, which is worse than a stopped bootstrap.
    """
    found: dict[str, dict[str, Any]] = {}
    claimed_by: dict[str, str] = {}

    for entry in entry_points(group=REGISTRATION_GROUP):
        registration = _loaded(entry)
        name = str(registration.get("name") or "")
        if not name:
            raise ConfigError(
                f"Registration '{entry.value}' declares no application name."
            )
        if name in claimed_by:
            # Two installed distributions claiming one application is not
            # something to resolve by order. Which one wins would depend on how
            # the environment happened to enumerate them.
            raise ConfigError(
                f"Two registrations claim the application '{name}': "
                f"{claimed_by[name]} and {entry.value}."
            )
        claimed_by[name] = entry.value
        found[name] = registration

    logger.debug("Discovered %d application registrations", len(found))
    return found


def _loaded(entry: Any) -> dict[str, Any]:
    """Return what one entry point publishes.

    Args:
        entry: The discovered entry point.

    Returns:
        The registration mapping.

    Raises:
        ConfigError: If it cannot be loaded or does not answer with a mapping.
    """
    try:
        registration = entry.load()()
    except Exception as exc:  # noqa: BLE001 — reported as configuration
        raise ConfigError(
            f"Registration '{entry.value}' could not be loaded: {exc}"
        ) from exc
    if not isinstance(registration, dict):
        raise ConfigError(
            f"Registration '{entry.value}' must return a mapping; "
            f"found {type(registration).__name__}."
        )
    return registration
