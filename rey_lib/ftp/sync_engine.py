"""Orchestrates the FTP sync: compare, filter, and download new files.

Accepts a ctx (global settings) and a conn (per-connection config) so the
same engine can run against any number of FTP connections without modification.
"""

from __future__ import annotations

import fnmatch
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from rey_lib.ftp.ftp_client import (
    Session,
    download_file,
    ftp_session,
    list_remote_dirs,
    list_remote_files,
)
from rey_lib.ftp.state_manager import (
    abandon_to_failed_file,
    add_to_retry_queue,
    increment_retry_count,
    is_new_or_updated,
    load_failed_files,
    load_last_stamp,
    load_retry_queue,
    load_state,
    record_downloaded,
    remove_from_retry_queue,
    save_last_stamp,
    save_state,
)
from rey_lib.errors.error_utils import FtpDownloadError
from rey_lib.logs.log_utils import log_enter, log_exit

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

    state      = load_state(ctx, conn)
    last_stamp = load_last_stamp(ctx, conn, state)
    retry_queue = load_retry_queue(state)

    if last_stamp is not None:
        log.info("Download cutoff (high-water mark): %s", last_stamp.isoformat())
    if retry_queue:
        log.info("Retry queue: %d file(s) pending from previous runs", len(retry_queue))

    all_downloaded: list[str] = []
    all_failed: list[str]     = []
    all_abandoned: list[str]  = []

    with ftp_session(conn.ftp) as session:
        # ── Pass 1: retry previously failed files regardless of stamp ──────
        if retry_queue:
            retried, still_failed, abandoned = _retry_failed(
                ctx, conn, session, retry_queue, state
            )
            all_downloaded.extend(retried)
            all_failed.extend(still_failed)
            all_abandoned.extend(abandoned)

        # ── Pass 2: expand glob paths then scan each resolved directory ────
        resolved_paths = _expand_remote_paths(session, conn.sync.remote_paths)
        for remote_path in resolved_paths:
            downloaded, failed = _sync_path(ctx, conn, session, remote_path, state, last_stamp)
            all_downloaded.extend(downloaded)
            all_failed.extend(failed)

    save_last_stamp(ctx, state)
    save_state(ctx, conn, state)

    _log_summary(conn.name, all_downloaded, all_failed, all_abandoned)
    log_exit(ctx, f"run_sync done: {conn.name}", log)
    return len(all_downloaded)


# ── Private helpers ───────────────────────────────────────────────────────────


def _retry_failed(
    ctx: Any,
    conn: Any,
    session: Session,
    retry_queue: list[dict],
    state: dict,
) -> tuple[list[str], list[str], list[str]]:
    """Attempt to download all files in the retry queue.

    On success: recorded in state, removed from queue.
    On failure: retry_count incremented. If retry_count >= max_retry_sessions
    the file is abandoned to conn.sync.failed_file and removed from queue.

    Args:
        ctx:         Global context (carries log_file path).
        conn:        Per-connection Namespace.
        session:     Active Session from ftp_session().
        retry_queue: List of retry entry dicts.
        state:       State dict mutated in place.

    Returns:
        Tuple of (downloaded filenames, still-failing filenames, abandoned filenames).
    """
    log_enter(ctx, f"_retry_failed: {len(retry_queue)} file(s)", log)
    downloaded: list[str]  = []
    still_failed: list[str] = []
    abandoned: list[str]   = []

    max_retries = int(getattr(conn.ftp, "max_retry_sessions", 3))
    failed_file = Path(conn.sync.failed_file)
    log_file    = getattr(ctx, "log_file", "unknown")

    for entry in retry_queue:
        remote_path = entry["remote_path"]
        filename    = entry["filename"]
        modified_dt = datetime.fromisoformat(entry["modified"])
        local_dir   = _local_dir_for_path(conn, remote_path)

        log.info("Retrying: %s/%s (attempt %d of %d)",
                 remote_path, filename, entry.get("retry_count", 1), max_retries)

        success = _download_one(session, remote_path, filename, modified_dt, local_dir, state)
        if success:
            remove_from_retry_queue(state, remote_path, filename)
            downloaded.append(filename)
        else:
            # Increment retry count for this entry.
            new_count = increment_retry_count(state, remote_path, filename)
            if new_count >= max_retries:
                # Exceeded limit — abandon permanently.
                abandon_to_failed_file(
                    conn_name=conn.name,
                    failed_file=failed_file,
                    remote_path=remote_path,
                    filename=filename,
                    modified_dt=modified_dt,
                    retry_count=new_count,
                    log_file=log_file,
                    state=state,
                )
                abandoned.append(filename)
            else:
                still_failed.append(filename)

    log_exit(ctx, (
        f"_retry_failed done: {len(downloaded)} recovered, "
        f"{len(still_failed)} still failing, {len(abandoned)} abandoned"
    ), log)
    return downloaded, still_failed, abandoned


def _sync_path(
    ctx: Any,
    conn: Any,
    session: Session,
    remote_path: str,
    state: dict[str, str],
    last_stamp: datetime | None,
) -> tuple[list[str], list[str]]:
    """Sync a single remote directory.

    Args:
        ctx:         Global context.
        conn:        Per-connection Namespace.
        session:     Active Session from ftp_session().
        remote_path: Remote directory to process.
        state:       Current state dict, mutated in place as files are downloaded.
        last_stamp:  High-water mark — files at or before this are skipped.

    Returns:
        Tuple of (downloaded filenames, failed filenames).
    """
    log_enter(ctx, f"_sync_path: {remote_path}", log)
    log.info("Scanning: %s", remote_path)

    all_files = list_remote_files(session, remote_path)
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

    downloaded, failed = _download_in_chunks(ctx, conn, session, remote_path, to_download, state)
    log_exit(ctx, f"_sync_path done: {len(downloaded)} downloaded, {len(failed)} failed", log)
    return downloaded, failed


def _download_in_chunks(
    ctx: Any,
    conn: Any,
    session: Session,
    remote_path: str,
    files: list[tuple[str, datetime]],
    state: dict[str, str],
) -> tuple[list[str], list[str]]:
    """Download files in groups of ctx.sync.chunk_size.

    Args:
        ctx:         Global context carrying chunk_size.
        conn:        Per-connection Namespace.
        session:     Active Session from ftp_session().
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
            success = _download_one(session, remote_path, filename, modified_dt, local_dir, state)
            if success:
                downloaded.append(filename)
            else:
                add_to_retry_queue(state, remote_path, filename, modified_dt)
                failed.append(filename)

    log_exit(ctx, "_download_in_chunks done", log)
    return downloaded, failed


def _download_one(
    session: Session,
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
        download_file(session, remote_path, filename, local_dir)
        record_downloaded(state, remote_path, filename, modified_dt)
        return True
    except FtpDownloadError as exc:
        log.error("Download failed — %s/%s: %s", remote_path, filename, exc)
        return False


def _log_summary(
    connection_name: str,
    downloaded: list[str],
    failed: list[str],
    abandoned: list[str],
) -> None:
    """Log a clean end-of-run summary listing downloaded, failed, and abandoned files.

    Args:
        connection_name: Name of the FTP connection for context.
        downloaded:      Filenames successfully downloaded this run.
        failed:          Filenames that failed and are queued for retry.
        abandoned:       Filenames that exceeded max_retry_sessions and were
                         moved to the connection's failed_file.
    """
    log.info("--- Summary for connection: %s ---", connection_name)

    log.info("Downloaded this run: %d", len(downloaded))
    for filename in downloaded:
        log.info("  ✓ %s", filename)
    if not downloaded:
        log.info("  (no new files)")

    if failed:
        log.warning("Queued for retry: %d", len(failed))
        for filename in failed:
            log.warning("  ↻ %s", filename)

    if abandoned:
        log.error("Abandoned (exceeded max retries): %d", len(abandoned))
        for filename in abandoned:
            log.error("  ✗ %s", filename)

    log.info("--- End summary ---")


def _expand_remote_paths(
    session: Session,
    remote_paths: list[str],
) -> list[str]:
    """Expand any glob patterns in remote_paths to concrete directory paths.

    Paths without glob characters are returned unchanged. Paths containing
    '*', '?', or '[' are resolved by listing the parent directory and
    matching subdirectory names against the pattern.

    Example:
        '/incoming/'          → ['/incoming/']
        '/archive/2026-*/'    → ['/archive/2026-01/', '/archive/2026-02/', ...]

    Args:
        session:      Active Session from ftp_session().
        remote_paths: Raw path list from config, may contain globs.

    Returns:
        Flat list of resolved concrete paths, preserving order.
    """
    resolved: list[str] = []
    for path in remote_paths:
        if not any(c in path for c in ("*", "?", "[")):
            resolved.append(path)
            continue

        # Split into parent dir and glob pattern.
        # e.g. '/archive/2026-*/' → parent='/archive/', pattern='2026-*'
        pure       = PurePosixPath(path.rstrip("/"))
        parent     = str(pure.parent) + "/"
        pattern    = pure.name

        log.debug("Expanding glob: parent=%s pattern=%s", parent, pattern)
        try:
            subdirs = list_remote_dirs(session, parent)
        except Exception as exc:
            log.warning("Cannot list '%s' for glob expansion: %s", parent, exc)
            continue

        matched = sorted(
            f"{parent}{name}/"
            for name in subdirs
            if fnmatch.fnmatch(name, pattern)
        )
        log.info("Glob '%s' expanded to %d path(s): %s", path, len(matched), matched)
        resolved.extend(matched)

    return resolved


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
    result = _filter_by_exclude_extension(conn, result)
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



def _filter_by_exclude_extension(
    conn: Any,
    files: list[tuple[str, datetime]],
) -> list[tuple[str, datetime]]:
    """Exclude files whose extension is in conn.filters.exclude_extensions."""
    exclude = getattr(conn.filters, 'exclude_extensions', [])
    if not exclude:
        return files
    normalised = {
        e.lower() if e.startswith('.') else f'.{e.lower()}'
        for e in exclude
    }
    return [
        (name, dt)
        for name, dt in files
        if Path(name).suffix.lower() not in normalised
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
