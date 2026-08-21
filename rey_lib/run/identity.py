"""
Where a run identity is created, and the only place it is created.

One execution, one ``run_id``, minted once. Everything downstream -- durable run
logging, execution tracking, controls, status, the Console and the CLI -- reads
that same identity rather than deriving one of its own. A second minting site is
how a process handle and a durable record end up describing one execution under
two names, and how a reader is left correlating them by name and start time.

Minting lives here rather than in the logging layer because logging is one
consumer of a run identity among several. Logs bind and stamp it; they do not
decide it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

__all__ = ["establish_run_identity", "mint_run_id"]


def mint_run_id() -> str:
    """Return a new canonical run identity.

    The single minting site for a top-level or child execution alike: every
    execution gets its own, and lineage is carried by ``parent_run_id`` rather
    than by sharing an id.

    Returns
    -------
    str
        A new UUID string.
    """
    return str(uuid.uuid4())


def establish_run_identity(ctx: Any) -> None:
    """Ensure the context carries the standard run identity fields, created once.

    Sets three fields on ``ctx`` when absent and leaves existing values untouched
    so the identity is stable for the whole execution:

    - ``run_id``         : UUID string -- the authoritative execution identity.
    - ``run_timestamp``  : ``YYYYMMDD_HHMMSS`` -- human-readable, filename-safe,
      time-sortable; used for artifact filenames and operator display.
    - ``run_started_at`` : ISO-8601 start time with timezone offset -- the full
      timestamp preserved separately from the filename-safe id.

    The timestamp is taken from local system time made timezone-aware, so the
    offset is recorded even when no runtime timezone is configured. Identity
    (``run_id``) and display (``run_timestamp``) are intentionally separate.

    A context arriving with a ``run_id`` already set keeps it. That is what lets
    a caller establish identity before logging opens, and it is not a second
    minting site: the value still came from :func:`mint_run_id`.

    Parameters
    ----------
    ctx : Any
        Application context, mutated in place.

    Returns
    -------
    None
    """
    if not getattr(ctx, "run_id", None):
        ctx.run_id = mint_run_id()
    if not getattr(ctx, "run_timestamp", None):
        started = datetime.now().astimezone()
        ctx.run_timestamp = started.strftime("%Y%m%d_%H%M%S")
        ctx.run_started_at = started.isoformat()
