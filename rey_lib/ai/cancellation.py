"""How a withdrawal reaches an execution.

The ownership, stated once:

    the caller           owns why and when
    the execution owner  owns propagating it correctly
    the provider adapter owns provider-specific mechanics

So this subsystem never learns what a keystroke is, and autocomplete
supersession, an operator's abort, a workflow cancellation, a shutdown and a
timeout all arrive the same way.

A predicate rather than a token, because that is what a caller can always
supply: the old runner asked ``cancelled()`` before each attempt and that shape
already fits every one of those cases.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

__all__ = ["CancellationToken", "after", "never"]


def never() -> bool:
    """Nothing is cancelling this."""
    return False


class CancellationToken:
    """One withdrawal a caller can trigger, and executions can observe.

    Offered because a caller usually wants something to hold rather than a
    closure to write. Nothing here requires it: any ``() -> bool`` is accepted
    everywhere this is, which is what keeps the boundary the caller's.
    """

    def __init__(self) -> None:
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        """Withdraw the work. Safe to call more than once."""
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def __call__(self) -> bool:
        """So the token is usable wherever a predicate is."""
        return self._cancelled.is_set()


def after(seconds: float) -> Callable[[], bool]:
    """A timeout, expressed as the same predicate everything else uses."""
    deadline = time.monotonic() + float(seconds)
    return lambda: time.monotonic() >= deadline
