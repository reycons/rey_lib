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
    is_new_or_updated,
    load_last_stamp,
    load_state,
    record_downloaded,
    save_last_stamp,
    save_state,
)
from rey_lib.error_utils import FtpDownloadError
from rey_lib.log_utils import log_enter, log_exit

__all__ = ["run_sync"]

log = logging.getLogger(__name__)


def run_sync(ctx: Any, conn: Any) -> int:
    """Execute a complete FTP sync run for a single connection.

    Loads persisted state, opens one FTP session, syncs every remote path
    defined in conn, then saves state when all paths are complete.

    Args:
        ctx:  Global context (chunk_size, log settings).
        conn: Per-connection Namespace (ftp credentials, paths, filters, state_file).

    Returns:
        Total number of files successfully downloaded in this run.
    """
    log_enter(ctx, f"run_sync: {conn.name}", log)
    log.info("=== Starting sync for connection: %s ===", conn.name)

    state = load_state(conn)

    # Resolve the high-water mark — persisted stamp takes priority over
    # initial_stamp from config on the very first run.
    last_stamp = load_last_stamp(conn, state)
    if last_stamp is not None:
        log.info("Download cutoff (high-water mark): %s", last_stamp.isoformat())

    # Accumulate every downloaded filename for the end-of-run summary.
    all_downloaded: list[str] = []

    with ftp_session(conn.ftp) as ftp:
        for remote_path in conn.sync.remote_paths:
            files = _sync_path(ctx, conn, ftp, remote_path, state, last_stamp)
            all_downloaded.extend(files)

    # Save state once after all paths complete — partial runs don't corrupt state.
    save_last_stamp(conn, state)
    save_state(conn, state)

    _log_summary(conn.name, all_downloaded)
    log_exit(ctx, f"run_sync done: {conn.name}", log)
    return len(all_downloaded)


# ── Private helpers ───────────────────────────────────────────────────────────


def _sync_path(
    ctx: Any,
    conn: Any,
    ftp: ftplib.FTP,
    remote_path: str,
    state: dict[str, str],
    last_stamp: datetime | None,
) -> list[str]:
    """Sync a single remote directory, returning list of downloaded filenames.

    Args:
        ctx:         Global context.
        conn:        Per-connection Namespace.
        ftp:         Authenticated FTP connection.
        remote_path: Remote directory to process.
        state:       Current state dict, mutated in place as files are downloaded.
        last_stamp:  High-water mark — files at or before this are skipped.

    Returns:
        List of filenames successfully downloaded from this path.
    """
    log_enter(ctx, f"_sync_path: {remote_path}", log)
    log.info("Scanning: %s", remote_path)

    all_files = list_remote_files(ftp, remote_path)
    log.info("Remote files found: %d", len(all_files))

    # Stamp cutoff first — eliminates bulk of already-seen files cheaply.
    after_stamp = _filter_by_stamp(all_files, last_stamp)
    log.info("After stamp cutoff: %d", len(after_stamp))

    filtered = _apply_filters(conn, after_stamp)
    log.info("After config filters: %d", len(filtered))

    # Per-file state check — catches files missed by stamp (e.g. updated files).
    to_download = [
        (name, dt)
        for name, dt in filtered
        if is_new_or_updated(state, remote_path, name, dt)
    ]
    log.info("New or updated: %d", len(to_download))

    downloaded = _download_in_chunks(ctx, conn, ftp, remote_path, to_download, state)
    log_exit(ctx, f"_sync_path done: {len(downloaded)} downloaded", log)
    return downloaded


def _download_in_chunks(
    ctx: Any,
    conn: Any,
    ftp: ftplib.FTP,
    remote_path: str,
    files: list[tuple[str, datetime]],
    state: dict[str, str],
) -> list[str]:
    """Download files in groups of ctx.sync.chunk_size.

    Args:
        ctx:         Global context carrying chunk_size.
        conn:        Per-connection Namespace.
        ftp:         Authenticated FTP connection.
        remote_path: Remote directory the files live in.
        files:       List of (filename, modified_utc) tuples to download.
        state:       State dict mutated in place on each successful download.

    Returns:
        List of filenames successfully downloaded.
    """
    log_enter(ctx, "_download_in_chunks", log)
    local_dir  = _local_dir_for_path(conn, remote_path)
    downloaded: list[str] = []
    chunk_size: int = ctx.sync.chunk_size

    for chunk_start in range(0, len(files), chunk_size):
        chunk     = files[chunk_start: chunk_start + chunk_size]
        chunk_end = min(chunk_start + chunk_size, len(files))
        log.debug(
            "Processing chunk: files %d–%d of %d",
            chunk_start + 1, chunk_end, len(files),
        )
        for filename, modified_dt in chunk:
            success = _download_one(
                conn, ftp, remote_path, filename, modified_dt, local_dir, state
            )
            if success:
                downloaded.append(filename)

    log_exit(ctx, "_download_in_chunks done", log)
    return downloaded


def _download_one(
    conn: Any,
    ftp: ftplib.FTP,
    remote_path: str,
    filename: str,
    modified_dt: datetime,
    local_dir: Path,
    state: dict[str, str],
) -> bool:
    """Download a single file and record it in state on success.

    A per-file failure is logged but does not abort the run.

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


def _log_summary(connection_name: str, downloaded: list[str]) -> None:
    """Log a clean end-of-run summary listing every downloaded file.

    Args:
        connection_name: Name of the FTP connection for context.
        downloaded:      List of downloaded filenames.
    """
    log.info("--- Summary for connection: %s ---", connection_name)
    log.info("Files downloaded this run: %d", len(downloaded))
    for filename in downloaded:
        log.info("  ✓ %s", filename)
    if not downloaded:
        log.info("  (no new files)")
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
