"""How long something took, as a label a reader can scan."""

from __future__ import annotations

from datetime import datetime, timezone


def duration_label(started_at: str, ended_at: str) -> str:
    """Return the established compact duration label.

    An absent end is read as "still going", so an unfinished run reports the
    time it has taken so far rather than nothing. An unparseable timestamp
    reports nothing rather than a misleading zero.

    Parameters
    ----------
    started_at : str
        ISO-8601 start. Empty means there is nothing to measure.
    ended_at : str
        ISO-8601 end, or empty while the thing is still running.

    Returns
    -------
    str
        ``1h 2m 3s``, ``2m 3s`` or ``3s``. Empty when it cannot be measured.
    """
    if not started_at:
        return ""
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(ended_at) if ended_at else datetime.now(timezone.utc)
        seconds = max(0, int((end - start).total_seconds()))
    except ValueError:
        return ""
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {sec}s"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"
