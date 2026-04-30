"""Orchestrates the FTP sync: compare, filter, and download new files.

Accepts a ctx (global settings) and a conn (per-connection config) so the
same engine can run against any number of FTP connections without modification.
"""

from __future__ import annotations

import fnmatch
import ftplib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from rey_lib.ftp_client import download_file, ftp_session, list_remote_files
from rey_lib.state_manager import (
    add_to_retry_queue,
    is_new_or_updated,
    load_last_stamp,
    load_retry_queue,
    load_state,
    record_downloaded,
    remove_from_retry_queue,
    save_last_stamp,
    save_state,
)
from rey_lib.error_utils import FtpDownloadError
from rey_lib.log_utils import log_enter, log_exit

__all__ = ["run_sync"]

log = logging.getLogger(__name__)


def run_sync(ctx: Any, conn: Any) -> int:
    """Execute a complete FTP sync run for a single connection.

    Loads persisted state, retries any previously failed files first,
    then scans remote paths for new files. Saves state when complete.

    Args:
        ctx:  Global context (chunk_size, log settings).
        conn: Per-connection Namespace (ftp credentials, paths, filters, state_file).

    Returns:
        Total number of files successfully downloaded in this run.
    """
    log_enter(ctx, f"run_sync: {conn.name}", log)
    log.info("=== Starting sync for connection: %s ===", conn.name)

    state      = load_state(conn)
    last_stamp = load_last_stamp(conn, state)
    retry_queue = load_retry_queue(state)

    if last_stamp is not None:
        log.info("Download cutoff (high-water mark): %s", last_stamp.isoformat())
    if retry_queue:
        log.info("Retry queue: %d file(s) pending from previous runs", len(retry_queue))

    all_downloaded: list[str] = []
    all_failed: list[str]     = []

    with ftp_session(conn.ftp) as ftp:
        # ── Pass 1: retry previously failed files regardless of stamp ──────
        if retry_queue:
            retried, still_failed = _retry_failed(ctx, conn, ftp, retry_queue, state)
            all_downloaded.extend(retried)
            all_failed.extend(still_failed)

        # ── Pass 2: scan remote paths for new files ────────────────────────
        for remote_path in conn.sync.remote_paths:
            downloaded, failed = _sync_path(ctx, conn, ftp, remote_path, state, last_stamp)
            all_downloaded.extend(downloaded)
            all_failed.extend(failed)

    save_last_stamp(conn, state)
    save_state(conn, state)

    _log_summary(conn.name, all_downloaded, all_failed)
    log_exit(ctx, f"run_sync done: {conn.name}", log)
    return len(all_downloaded)


# ── Private helpers ───────────────────────────────────────────────────────────


def _retry_failed(
    ctx: Any,
    conn: Any,
    ftp: ftplib.FTP,
    retry_queue: list[dict],
    state: dict,
) -> tuple[list[str], list[str]]:
    """Attempt to download all files in the retry queue.

    Each entry is attempted once. Successes are recorded in state and removed
    from the queue. Failures remain in the queue for the next run.

    Args:
        ctx:         Global context.
        conn:        Per-connection Namespace.
        ftp:         Authenticated FTP connection.
        retry_queue: List of retry entry dicts (remote_path, filename, modified).
        state:       State dict mutated in place.

    Returns:
        Tuple of (downloaded filenames, still-failed filenames).
    """
    log_enter(ctx, f"_retry_failed: {len(retry_queue)} file(s)", log)
    downloaded: list[str] = []
    still_failed: list[str] = []

    for entry in retry_queue:
        remote_path = entry["remote_path"]
        filename    = entry["filename"]
        modified_dt = datetime.fromisoformat(entry["modified"])
        local_dir   = _local_dir_for_path(conn, remote_path)

        log.info("Retrying: %s/%s", remote_path, filename)
        success = _download_one(ftp, remote_path, filename, modified_dt, local_dir, state)
        if success:
            remove_from_retry_queue(state, remote_path, filename)
            downloaded.append(filename)
        else:
            still_failed.append(filename)

    log_exit(ctx, f"_retry_failed done: {len(downloaded)} recovered, {len(still_failed)} still failing", log)
    return downloaded, still_failed


def _sync_path(
    ctx: Any,
    conn: Any,
    ftp: ftplib.FTP,
    remote_path: str,
    state: dict[str, str],
    last_stamp: datetime | None,
) -> tuple[list[str], list[str]]:
    """Sync a single remote directory.

    Args:
        ctx:         Global context.
        conn:        Per-connection Namespace.
        ftp:         Authenticated FTP connection.
        remote_path: Remote directory to process.
        state:       Current state dict, mutated in place as files are downloaded.
        last_stamp:  High-water mark — files at or before this are skipped.

    Returns:
        Tuple of (downloaded filenames, failed filenames).
    """
    log_enter(ctx, f"_sync_path: {remote_path}", log)
    log.info("Scanning: %s", remote_path)

    all_files = list_remote_files(ftp, remote_path)
    log.info("Remote files found: %d", len(all_files))

    after_stamp = _filter_by_stamp(all_files, last_stamp)
    log.info("After stamp cutoff: %d", len(after_stamp))

    filtered = _apply_filters(conn, after_stamp)
    log.info("After config filters: %d", len(filtered))

    to_download = [
        (name, dt)
        for name, dt in filtered
        if is_new_or_updated(state, remote_path, name, dt)
    ]
    log.info("New or updated: %d", len(to_download))

    downloaded, failed = _download_in_chunks(ctx, conn, ftp, remote_path, to_download, state)
    log_exit(ctx, f"_sync_path done: {len(downloaded)} downloaded, {len(failed)} failed", log)
    return downloaded, failed


def _download_in_chunks(
    ctx: Any,
    conn: Any,
    ftp: ftplib.FTP,
    remote_path: str,
    files: list[tuple[str, datetime]],
    state: dict[str, str],
) -> tuple[list[str], list[str]]:
    """Download files in groups of ctx.sync.chunk_size.

    Args:
        ctx:         Global context carrying chunk_size.
        conn:        Per-connection Namespace.
        ftp:         Authenticated FTP connection.
        remote_path: Remote directory the files live in.
        files:       List of (filename, modified_utc) tuples to download.
        state:       State dict mutated in place on each download attempt.

    Returns:
        Tuple of (downloaded filenames, failed filenames).
    """
    log_enter(ctx, "_download_in_chunks", log)
    local_dir   = _local_dir_for_path(conn, remote_path)
    downloaded: list[str] = []
    failed: list[str]     = []
    chunk_size: int = ctx.sync.chunk_size

    for chunk_start in range(0, len(files), chunk_size):
        chunk     = files[chunk_start: chunk_start + chunk_size]
        chunk_end = min(chunk_start + chunk_size, len(files))
        log.debug(
            "Processing chunk: files %d–%d of %d",
            chunk_start + 1, chunk_end, len(files),
        )
        for filename, modified_dt in chunk:
            success = _download_one(ftp, remote_path, filename, modified_dt, local_dir, state)
            if success:
                downloaded.append(filename)
            else:
                # Add to retry queue so the file is not permanently lost.
                add_to_retry_queue(state, remote_path, filename, modified_dt)
                failed.append(filename)

    log_exit(ctx, "_download_in_chunks done", log)
    return downloaded, failed


def _download_one(
    ftp: ftplib.FTP,
    remote_path: str,
    filename: str,
    modified_dt: datetime,
    local_dir: Path,
    state: dict[str, str],
) -> bool:
    """Download a single file and record it in state on success.

    Does not add to retry queue on failure — that is the caller's responsibility
    so the same function can be used for both normal downloads and retries.

    Returns:
        True on success, False on failure.
    """
    try:
        download_file(ftp, remote_path, filename, local_dir)
        record_downloaded(state, remote_path, filename, modified_dt)
        return True
    except FtpDownloadError as exc:
        log.error("Download failed — %s/%s: %s", remote_path, filename, exc)
        return False


def _log_summary(
    connection_name: str,
    downloaded: list[str],
    failed: list[str],
) -> None:
    """Log a clean end-of-run summary listing downloaded and failed files.

    Args:
        connection_name: Name of the FTP connection for context.
        downloaded:      Filenames successfully downloaded this run.
        failed:          Filenames that failed and are queued for retry.
    """
    log.info("--- Summary for connection: %s ---", connection_name)
    log.info("Downloaded this run : %d", len(downloaded))
    for filename in downloaded:
        log.info("  ✓ %s", filename)
    if not downloaded:
        log.info("  (no new files)")

    if failed:
        log.warning("Failed (queued for retry): %d", len(failed))
        for filename in failed:
            log.warning("  ✗ %s", filename)

    log.info("--- End summary ---")


def _filter_by_stamp(
    files: list[tuple[str, datetime]],
    last_stamp: datetime | None,
) -> list[tuple[str, datetime]]:
    """Discard files at or before the high-water mark."""
    if last_stamp is None:
        return files
    result: list[tuple[str, datetime]] = []
    for name, dt in files:
        dt_aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        if dt_aware > last_stamp:
            result.append((name, dt))
    return result


def _apply_filters(
    conn: Any,
    files: list[tuple[str, datetime]],
) -> list[tuple[str, datetime]]:
    """Apply all config-driven filters from conn.filters in sequence."""
    result = files
    result = _filter_by_extension(conn, result)
    result = _filter_by_name_pattern(conn, result)
    result = _filter_by_max_age(conn, result)
    return result


def _filter_by_extension(
    conn: Any,
    files: list[tuple[str, datetime]],
) -> list[tuple[str, datetime]]:
    """Retain only files whose extension is in conn.filters.extensions."""
    extensions = getattr(conn.filters, "extensions", [])
    if not extensions:
        return files
    # Normalise to lowercase with leading dot.
    normalised = {
        e.lower() if e.startswith(".") else f".{e.lower()}"
        for e in extensions
    }
    return [
        (name, dt)
        for name, dt in files
        if Path(name).suffix.lower() in normalised
    ]


def _filter_by_name_pattern(
    conn: Any,
    files: list[tuple[str, datetime]],
) -> list[tuple[str, datetime]]:
    """Retain only files matching conn.filters.name_pattern (glob)."""
    pattern = getattr(conn.filters, "name_pattern", None)
    if not pattern:
        return files
    pattern = pattern.lower()
    return [
        (name, dt)
        for name, dt in files
        if fnmatch.fnmatch(name.lower(), pattern)
    ]


def _filter_by_max_age(
    conn: Any,
    files: list[tuple[str, datetime]],
) -> list[tuple[str, datetime]]:
    """Retain only files within conn.filters.max_age_days."""
    max_age_days = getattr(conn.filters, "max_age_days", None)
    if max_age_days is None:
        return files
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=int(max_age_days))
    result: list[tuple[str, datetime]] = []
    for name, dt in files:
        dt_aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        if dt_aware >= cutoff:
            result.append((name, dt))
    return result


def _local_dir_for_path(conn: Any, remote_path: str) -> Path:
    """Map a remote path to a mirrored local subdirectory under conn.sync.local_destination."""
    relative = remote_path.lstrip("/")
    return Path(conn.sync.local_destination) / relative
