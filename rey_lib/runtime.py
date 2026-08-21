"""Final cleanup of the shared objects a launch created.

Bootstrap composes shared runtime objects -- today the Connection per
configured connection -- and nothing closed them. Every consumer correctly
refuses to: a shared object closed by whichever holder finishes first is taken
from all the others. That leaves exactly one place the decision belongs, which
is the boundary that created them.

    ctx = build_ctx_for_app(...)
    try:
        ...application work...
    finally:
        collect_runtime(ctx)

Registration is explicit. The collector never scans the context for anything
with a ``close`` method: a context carries configuration, application state and
whatever an app has put on it, and closing things because they happen to look
closeable is how something gets shut down that nobody meant to own. An object
is collected because it was registered, and for no other reason.

What is collected, and what it means to collect it, stay apart. Connection owns
how it closes; this owns when the last close happens. A later shared object
participates by being registered at composition, with no change here and none
in any app.
"""

from __future__ import annotations

from typing import Any, Optional

from rey_lib.logs import get_logger

__all__ = ["register_runtime_object", "registered_runtime_objects", "collect_runtime"]

_logger = get_logger(__name__)

_REGISTRY = "runtime_objects"


def register_runtime_object(ctx: Any, obj: Any, *, name: str = "") -> None:
    """Register one shared object for final collection.

    Parameters
    ----------
    ctx : Any
        Application context, which carries the registry for this launch.
    obj : Any
        The shared object. It must expose ``close()``.
    name : str
        A label for reporting. Defaults to the object's own ``name`` when it
        has one, else its type.
    """
    label = name or str(getattr(obj, "name", "") or type(obj).__name__)
    registered = list(getattr(ctx, _REGISTRY, None) or [])
    if any(existing is obj for _, existing in registered):
        return  # Registering twice must not collect twice.
    registered.append((label, obj))
    setattr(ctx, _REGISTRY, registered)


def registered_runtime_objects(ctx: Any) -> list[tuple[str, Any]]:
    """Return the shared objects registered for collection, in creation order."""
    return list(getattr(ctx, _REGISTRY, None) or [])


def collect_runtime(ctx: Any, *, suppress: bool = False) -> list[str]:
    """Close every registered shared object, once.

    Each object is collected at most once: the registry is emptied as it runs,
    so a second call after a successful collection has nothing to do. An object
    a consumer already closed is collected harmlessly, because closing is
    idempotent and the object knows it is already shut.

    One failure does not stop the rest. Every remaining object is still
    attempted, because the alternative is that one broken handle leaves the
    others open.

    Parameters
    ----------
    ctx : Any
        The launch context holding the registry.
    suppress : bool
        Set when an application failure is already propagating. Failures are
        reported but not raised, so cleanup never replaces the error that
        actually ended the run.

    Returns
    -------
    list[str]
        One description per failed cleanup; empty when all succeeded.

    Raises
    ------
    StateError
        When a cleanup failed and ``suppress`` is False.
    """
    registered = registered_runtime_objects(ctx)
    setattr(ctx, _REGISTRY, [])

    failures: list[str] = []
    for label, obj in registered:
        try:
            obj.close()
        except Exception as exc:  # noqa: BLE001 — one failure must not strand the rest.
            failures.append(f"{label}: {exc}")
            _logger.warning("runtime cleanup failed for %s: %s", label, exc)

    if failures and not suppress:
        from rey_lib.errors.error_utils import StateError

        raise StateError(
            "runtime cleanup failed for " + "; ".join(failures)
        )
    return failures


def collected_cleanly(ctx: Any) -> bool:
    """Whether nothing remains registered for this launch."""
    return not registered_runtime_objects(ctx)
