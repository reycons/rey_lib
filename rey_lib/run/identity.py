"""
The timestamps a run is displayed and filed under.

Identity itself is no longer created here. ``run_id`` is
``control.run_manifest.run_manifest_id``, generated when ``Run.start`` records
the run, so recording a run is what creates its identity. There is no local
minting site, which is what stops a process handle and a durable record
describing one execution under two names.

What remains is display and filing: ``run_timestamp`` names artifacts and log
files, and ``run_started_at`` keeps the full offset. Both are derived from local
time and neither identifies anything.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

__all__ = ["establish_run_identity"]


def establish_run_identity(ctx: Any) -> None:
    """Ensure the context carries the run's display timestamps, created once.

    Sets two fields on ``ctx`` when absent and leaves existing values untouched
    so they stay stable for the whole execution:

    - ``run_timestamp``  : ``YYYYMMDD_HHMMSS`` -- human-readable, filename-safe,
      time-sortable; used for artifact filenames and operator display.
    - ``run_started_at`` : ISO-8601 start time with timezone offset -- the full
      timestamp preserved separately from the filename-safe form.

    The timestamp is taken from local system time made timezone-aware, so the
    offset is recorded even when no runtime timezone is configured.

    ``run_id`` is *not* set here. It is the manifest's ``run_manifest_id``,
    established by ``Run.start`` before this is called, and a context arriving
    without one has not started a run.

    Parameters
    ----------
    ctx : Any
        Application context, mutated in place.

    Returns
    -------
    None
    """
    if not getattr(ctx, "run_timestamp", None):
        started = datetime.now().astimezone()
        ctx.run_timestamp = started.strftime("%Y%m%d_%H%M%S")
        ctx.run_started_at = started.isoformat()
