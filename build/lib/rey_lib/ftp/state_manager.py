"""Tracks which FTP files have already been downloaded using a JSON state file.

State format:
    {
        "_last_downloaded_stamp": "<ISO-8601 UTC>",
        "_retry_queue": [
            {"remote_path": "/incoming/", "filename": "file.csv", "modified": "<ISO-8601 UTC>"},
            ...
        ],
        "<remote_path>/<filename>": "<ISO-8601 UTC timestamp>",
        ...
    }

A file is considered new if its key is absent from state.
A file is considered updated if its remote modification time is later than stored.
Failed downloads are placed in the retry queue and retried on every subsequent
run regardless of the high-water mark stamp — preventing permanent data loss.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rey_lib.errors.error_utils import StateError
from rey_lib.logs.log_utils import log_enter, log_exit

__all__ = [
    "load_state",
    "save_state",
    "is_new_or_updated",
    "record_downloaded",
    "load_last_stamp",
    "save_last_stamp",
    "load_retry_queue",
    "add_to_retry_queue",
    "remove_from_retry_queue",
    "increment_retry_count",
    "load_failed_files",
    "abandon_to_failed_file",
]

log = logging.getLogger(__name__)


def load_state(ctx: Any) -> dict[str, str]:
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
    log_enter(ctx, "load_state", log)
    state_file: Path = ctx.state_file

    if not state_file.exists():
        log.info("No state file at '%s' — starting fresh.", state_file)
        log_exit(ctx, "load_state done (fresh)", log)
        return {}

    try:
        with state_file.open(encoding="utf-8") as f:
            state: dict[str, str] = json.load(f)
        log.info("Loaded state: %d entry/entries from '%s'", len(state), state_file)
        log_exit(ctx, "load_state done", log)
        return state
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"Cannot read state file '{state_file}'.") from exc


def save_state(ctx: Any, state: dict[str, str]) -> None:
    """Persist the download state to the JSON state file.

    Creates parent directories if they do not exist. Keys are written in
    sorted order to produce stable, diff-friendly output.

    Args:
        ctx:   Namespace carrying the state_file path.
        state: Current state dict to persist.

    Raises:
        StateError: If the file cannot be written.
    """
    log_enter(ctx, "save_state", log)
    state_file: Path = ctx.state_file
    state_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with state_file.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        log.info("Saved state: %d entry/entries to '%s'", len(state), state_file)
    except OSError as exc:
        raise StateError(f"Cannot write state file '{state_file}'.") from exc
    finally:
        log_exit(ctx, "save_state done", log)


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
    modified_dt  = _ensure_utc(modified_dt)
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
    key        = _state_key(remote_path, filename)
    state[key] = _ensure_utc(modified_dt).isoformat()

    # Update high-water mark if this file is the newest seen so far.
    current_stamp = _read_stamp_from_state(state)
    if current_stamp is None or _ensure_utc(modified_dt) > current_stamp:
        state[_STAMP_KEY] = _ensure_utc(modified_dt).isoformat()


def load_last_stamp(ctx: Any, state: dict[str, str]) -> datetime | None:
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


def save_last_stamp(ctx: Any, state: dict[str, str]) -> None:
    """Log the current high-water mark after a completed run.

    The stamp is embedded in the state dict and persisted by save_state().
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


def load_retry_queue(state: dict) -> list[dict]:
    """Return the current retry queue from state.

    Each entry is a dict with keys: remote_path, filename, modified.

    Args:
        state: Current state dict.

    Returns:
        List of retry entries; empty list if none are queued.
    """
    return list(state.get(_RETRY_KEY, []))


def add_to_retry_queue(
    state: dict,
    remote_path: str,
    filename: str,
    modified_dt: datetime,
) -> None:
    """Add a failed file to the retry queue with retry_count = 1.

    If the file is already in the queue its entry is updated in place —
    use increment_retry_count() to advance the count on subsequent failures.

    Args:
        state:       State dict mutated in place.
        remote_path: Remote directory the file lives in.
        filename:    Basename of the failed file.
        modified_dt: Modification timestamp — preserved for recording on success.
    """
    queue: list[dict] = state.get(_RETRY_KEY, [])

    # Preserve existing retry_count if already queued.
    existing = next(
        (e for e in queue if e["remote_path"] == remote_path and e["filename"] == filename),
        None,
    )
    retry_count = existing["retry_count"] if existing else 1

    queue = [
        e for e in queue
        if not (e["remote_path"] == remote_path and e["filename"] == filename)
    ]
    queue.append({
        "remote_path":  remote_path,
        "filename":     filename,
        "modified":     _ensure_utc(modified_dt).isoformat(),
        "retry_count":  retry_count,
    })
    state[_RETRY_KEY] = queue
    log.warning(
        "Added to retry queue: %s/%s (attempt %d)",
        remote_path, filename, retry_count,
    )


def increment_retry_count(
    state: dict,
    remote_path: str,
    filename: str,
) -> int:
    """Increment the retry_count for a queued file and return the new count.

    Called after each failed retry attempt so the count accurately reflects
    how many runs have attempted this file.

    Args:
        state:       State dict mutated in place.
        remote_path: Remote directory the file lives in.
        filename:    Basename of the file.

    Returns:
        Updated retry_count, or 0 if the entry was not found in the queue.
    """
    queue: list[dict] = state.get(_RETRY_KEY, [])
    for entry in queue:
        if entry["remote_path"] == remote_path and entry["filename"] == filename:
            entry["retry_count"] = entry.get("retry_count", 1) + 1
            log.debug(
                "Retry count incremented: %s/%s → %d",
                remote_path, filename, entry["retry_count"],
            )
            return entry["retry_count"]
    return 0


def remove_from_retry_queue(
    state: dict,
    remote_path: str,
    filename: str,
) -> None:
    """Remove a file from the retry queue after successful download.

    No-op if the file is not in the queue.

    Args:
        state:       State dict mutated in place.
        remote_path: Remote directory the file lives in.
        filename:    Basename of the file.
    """
    queue: list[dict] = state.get(_RETRY_KEY, [])
    updated = [
        e for e in queue
        if not (e["remote_path"] == remote_path and e["filename"] == filename)
    ]
    if len(updated) < len(queue):
        state[_RETRY_KEY] = updated
        log.info("Removed from retry queue: %s/%s", remote_path, filename)


def load_failed_files(failed_file: Path) -> list[dict]:
    """Load the abandoned files record from the per-connection failed_file.

    Returns an empty list if the file does not exist.

    Args:
        failed_file: Path to the connection's .failed.json file.

    Returns:
        List of abandoned file entry dicts.

    Raises:
        StateError: If the file exists but cannot be read or parsed.
    """
    if not failed_file.exists():
        return []
    try:
        with failed_file.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"Cannot read failed file '{failed_file}'.") from exc


def abandon_to_failed_file(
    conn_name: str,
    failed_file: Path,
    remote_path: str,
    filename: str,
    modified_dt: datetime,
    retry_count: int,
    log_file: str,
    state: dict,
) -> None:
    """Move a file from the retry queue to the permanent failed record.

    Called when a file has exceeded max_retry_sessions. The entry is removed
    from the retry queue in state and appended to the per-connection
    failed_file JSON so operators have a clear actionable list.

    Args:
        conn_name:   Connection name (embedded in the failed record).
        failed_file: Path to the connection's .failed.json file.
        remote_path: Remote directory the file lives in.
        filename:    Basename of the abandoned file.
        modified_dt: File modification timestamp.
        retry_count: Number of retry sessions attempted before abandoning.
        log_file:    Path to the log file from the run that abandoned this file.
        state:       State dict — the queue entry is removed in place.
    """
    # Remove from retry queue first.
    remove_from_retry_queue(state, remote_path, filename)

    # Build the failure record.
    entry = {
        "connection":  conn_name,
        "remote_path": remote_path,
        "filename":    filename,
        "modified":    _ensure_utc(modified_dt).isoformat(),
        "retry_count": retry_count,
        "abandoned_at": datetime.now(tz=timezone.utc).isoformat(),
        "log_file":    log_file,
    }

    # Append to the persistent failed file.
    failed_file.parent.mkdir(parents=True, exist_ok=True)
    existing = load_failed_files(failed_file)
    existing.append(entry)
    try:
        with failed_file.open("w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, sort_keys=True)
    except OSError as exc:
        raise StateError(f"Cannot write failed file '{failed_file}'.") from exc

    log.error(
        "ABANDONED after %d retries: %s/%s — see %s and %s",
        retry_count, remote_path, filename, failed_file, log_file,
    )


# ── Private helpers ───────────────────────────────────────────────────────────

# Reserved keys inside the state dict — prefixed with underscore so they
# cannot collide with real remote file path keys.
_STAMP_KEY = "_last_downloaded_stamp"
_RETRY_KEY = "_retry_queue"


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
