"""Tracks which FTP files have already been downloaded using a JSON state file.

State format:
    { "<remote_path>/<filename>": "<ISO-8601 UTC timestamp>", ... }

A file is considered new if its key is absent from state.
A file is considered updated if its remote modification time is later than stored.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from argparse import Namespace
from rey_lib.error_utils import StateError
from rey_lib.log_utils import log_enter, log_exit

__all__ = ["load_state", "save_state", "is_new_or_updated", "record_downloaded",
           "load_last_stamp", "save_last_stamp"]

log = logging.getLogger(__name__)


def load_state(ctx: Namespace) -> dict[str, str]:
    """Load the download state from the JSON state file.

    Returns an empty dict if the file does not yet exist — this is the
    expected condition on the very first run.

    Args:
        ctx: Namespace carrying the state_file path.

    Returns:
        Dict mapping '<remote_path>/<filename>' → ISO-8601 UTC timestamp string.

    Raises:
        StateError: If the file exists but cannot be read or parsed.
    """
    log_enter(ctx, "load_state")
    state_file: Path = ctx.state_file

    if not state_file.exists():
        log.info("No state file at '%s' — starting fresh.", state_file)
        log_exit(ctx, "load_state done (fresh)")
        return {}

    try:
        with state_file.open(encoding="utf-8") as f:
            state: dict[str, str] = json.load(f)
        log.info("Loaded state: %d entry/entries from '%s'", len(state), state_file)
        log_exit(ctx, "load_state done")
        return state
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"Cannot read state file '{state_file}'.") from exc


def save_state(ctx: Namespace, state: dict[str, str]) -> None:
    """Persist the download state to the JSON state file.

    Creates parent directories if they do not exist.  Keys are written in
    sorted order to produce stable, diff-friendly output.

    Args:
        ctx:   Namespace carrying the state_file path.
        state: Current state dict to persist.

    Raises:
        StateError: If the file cannot be written.
    """
    log_enter(ctx, "save_state")
    state_file: Path = ctx.state_file
    state_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with state_file.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        log.info("Saved state: %d entry/entries to '%s'", len(state), state_file)
    except OSError as exc:
        raise StateError(f"Cannot write state file '{state_file}'.") from exc
    finally:
        log_exit(ctx, "save_state done")


def is_new_or_updated(
    state: dict[str, str],
    remote_path: str,
    filename: str,
    modified_dt: datetime,
) -> bool:
    """Return True if the file is absent from state or has a newer modification time.

    Args:
        state:       Current state dict.
        remote_path: Remote directory the file lives in.
        filename:    Basename of the file.
        modified_dt: Modification timestamp reported by the FTP server.

    Returns:
        True  — file should be downloaded.
        False — file is already current in state.
    """
    key = _state_key(remote_path, filename)

    # File has never been downloaded.
    if key not in state:
        return True

    # Parse the stored timestamp; if it is corrupt, treat the file as new.
    try:
        last_seen_dt = datetime.fromisoformat(state[key])
    except ValueError:
        return True

    # Ensure both timestamps are timezone-aware before comparing.
    modified_dt = _ensure_utc(modified_dt)
    last_seen_dt = _ensure_utc(last_seen_dt)

    return modified_dt > last_seen_dt


def record_downloaded(
    state: dict[str, str],
    remote_path: str,
    filename: str,
    modified_dt: datetime,
) -> None:
    """Record that a file was successfully downloaded by updating state in place.

    Also updates the high-water mark if modified_dt is later than the currently
    stored stamp, so the next run can use it as a download cutoff.

    Args:
        state:       State dict to update (mutated in place).
        remote_path: Remote directory the file lives in.
        filename:    Basename of the file.
        modified_dt: Modification timestamp to record.
    """
    key = _state_key(remote_path, filename)
    state[key] = _ensure_utc(modified_dt).isoformat()

    # Update high-water mark if this file is the newest seen so far.
    current_stamp = _read_stamp_from_state(state)
    if current_stamp is None or _ensure_utc(modified_dt) > current_stamp:
        state[_STAMP_KEY] = _ensure_utc(modified_dt).isoformat()


def load_last_stamp(ctx: Namespace, state: dict[str, str]) -> datetime | None:
    """Return the high-water mark timestamp for use as a download cutoff.

    Priority:
    1. High-water mark stored in state (set by the previous run).
    2. initial_stamp from ctx (used only when no state file exists yet).
    3. None — no cutoff, all files are eligible.

    Args:
        ctx:   Namespace carrying initial_stamp.
        state: Current state dict loaded from disk.

    Returns:
        A timezone-aware UTC datetime, or None if no stamp is available.
    """
    persisted = _read_stamp_from_state(state)
    if persisted is not None:
        log.debug("Using persisted high-water mark: %s", persisted.isoformat())
        return persisted

    initial_stamp = getattr(ctx, "initial_stamp", None)

    if initial_stamp is not None:
        log.info(
            "No persisted stamp found — using initial_stamp from config: %s",
            initial_stamp.isoformat(),
        )
        return initial_stamp

    log.info("No stamp available — all files are eligible for download.")
    return None


def save_last_stamp(ctx: Namespace, state: dict[str, str]) -> None:
    """Log the current high-water mark after a completed run.

    The stamp itself is embedded in the state dict and persisted by save_state().
    This function only logs it for operator visibility.

    Args:
        ctx:   Namespace (used for logging only).
        state: State dict that already contains the updated stamp.
    """
    stamp = _read_stamp_from_state(state)
    if stamp is not None:
        log.info("High-water mark after this run: %s", stamp.isoformat())
    else:
        log.info("No files downloaded — high-water mark unchanged.")


# ── Private helpers ───────────────────────────────────────────────────────────

# Reserved key inside the state dict where the high-water mark is stored.
# Prefixed with underscore so it cannot collide with a real remote file path.
_STAMP_KEY = "_last_downloaded_stamp"


def _state_key(remote_path: str, filename: str) -> str:
    """Build the canonical state dict key for a remote file.

    Example: remote_path='/incoming/', filename='data.csv' → '/incoming/data.csv'
    """
    return f"{remote_path.rstrip('/')}/{filename}"


def _ensure_utc(dt: datetime) -> datetime:
    """Return *dt* with UTC timezone attached if it is naive."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _read_stamp_from_state(state: dict[str, str]) -> datetime | None:
    """Extract and parse the high-water mark from the state dict.

    Returns None if the key is absent or the stored value is unparseable.
    """
    raw = state.get(_STAMP_KEY)
    if raw is None:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        return _ensure_utc(dt)
    except ValueError:
        return None
