"""FTP connection management and low-level file operations.

Owns all direct ftplib interaction.  No other module may call ftplib directly.

Provides:
- ftp_session()       — context manager for an authenticated FTP connection.
- list_remote_files() — list files with modification timestamps.
- download_file()     — download a single file to a local directory.
"""

from __future__ import annotations

import ftplib
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Generator

from rey_lib.ctx import AppContext
from rey_lib.error_utils import FtpConnectionError, FtpDownloadError
from rey_lib.log_utils import log_enter, log_exit

__all__ = ["ftp_session", "list_remote_files", "download_file"]

log = logging.getLogger(__name__)

# Seconds to wait for the FTP server to respond before raising a timeout error.
_FTP_TIMEOUT_SECONDS = 30


@contextmanager
def ftp_session(ctx: AppContext) -> Generator[ftplib.FTP, None, None]:
    """Context manager that yields an authenticated FTP connection.

    The connection is always closed on exit, even when an exception occurs.
    The caller must not close the connection manually.

    Args:
        ctx: AppContext carrying ftp_host, ftp_port, ftp_user, ftp_password.

    Yields:
        An authenticated ftplib.FTP instance.

    Raises:
        FtpConnectionError: If the connection or login fails.
    """
    log_enter(ctx, f"ftp_session → {ctx.ftp_host}:{ctx.ftp_port}")
    ftp: ftplib.FTP | None = None
    try:
        ftp = ftplib.FTP()
        ftp.connect(host=ctx.ftp_host, port=ctx.ftp_port, timeout=_FTP_TIMEOUT_SECONDS)
        ftp.login(user=ctx.ftp_user, passwd=ctx.ftp_password)
        log.info("FTP connected: %s@%s", ctx.ftp_user, ctx.ftp_host)
        yield ftp
    except ftplib.all_errors as exc:
        raise FtpConnectionError(
            f"Cannot connect to {ctx.ftp_host}:{ctx.ftp_port} — {exc}"
        ) from exc
    finally:
        if ftp is not None:
            _close_connection(ftp)
        log_exit(ctx, "ftp_session closed")


def list_remote_files(
    ctx: AppContext,
    ftp: ftplib.FTP,
    remote_path: str,
) -> list[tuple[str, datetime]]:
    """Return all files in *remote_path* as a list of (filename, modified_utc) tuples.

    Attempts MLSD first (RFC 3659); falls back to NLST + MDTM for older servers.
    Subdirectories are excluded from the result.

    Args:
        ctx:         AppContext.
        ftp:         Authenticated FTP connection.
        remote_path: Absolute remote directory path to list.

    Returns:
        List of (filename, utc_datetime) tuples; empty list if the directory
        is empty or does not exist.

    Raises:
        FtpConnectionError: If the listing command fails at the protocol level.
    """
    log_enter(ctx, f"list_remote_files: {remote_path}")
    try:
        # Try the modern MLSD command first — it returns type and timestamps.
        result = _list_via_mlsd(ftp, remote_path)
        if result is not None:
            log.debug("MLSD listing for %s: %d file(s)", remote_path, len(result))
            log_exit(ctx, "list_remote_files done (MLSD)")
            return result

        # Fall back to NLST + individual MDTM queries for each file.
        result = _list_via_nlst_mdtm(ftp, remote_path)
        log.debug("NLST+MDTM listing for %s: %d file(s)", remote_path, len(result))
        log_exit(ctx, "list_remote_files done (NLST+MDTM)")
        return result

    except ftplib.all_errors as exc:
        raise FtpConnectionError(
            f"Failed to list remote path '{remote_path}': {exc}"
        ) from exc


def download_file(
    ctx: AppContext,
    ftp: ftplib.FTP,
    remote_path: str,
    filename: str,
    local_dir: Path,
) -> Path:
    """Download a single file from the FTP server to *local_dir*.

    If the download fails, any partial local file is deleted before raising.

    Args:
        ctx:         AppContext.
        ftp:         Authenticated FTP connection.
        remote_path: Absolute remote directory containing the file.
        filename:    Name of the file to download (basename only).
        local_dir:   Local directory to save the file into (created if absent).

    Returns:
        The local Path where the file was saved.

    Raises:
        FtpDownloadError: If the download fails for any reason.
    """
    log_enter(ctx, f"download_file: {filename}")
    remote_file = str(PurePosixPath(remote_path) / filename)
    local_file = local_dir / filename

    # Create the destination directory tree if it does not already exist.
    local_dir.mkdir(parents=True, exist_ok=True)

    try:
        with local_file.open("wb") as f:
            ftp.retrbinary(f"RETR {remote_file}", f.write)
        log.info("Downloaded: %s → %s", remote_file, local_file)
        log_exit(ctx, "download_file done")
        return local_file
    except ftplib.all_errors as exc:
        # Remove the partial file so a retry starts from a clean state.
        if local_file.exists():
            local_file.unlink()
        raise FtpDownloadError(
            f"Failed to download '{remote_file}': {exc}"
        ) from exc


# ── Private helpers ───────────────────────────────────────────────────────────


def _list_via_mlsd(
    ftp: ftplib.FTP,
    remote_path: str,
) -> list[tuple[str, datetime]] | None:
    """Attempt to list files using MLSD (RFC 3659).

    Returns None if the server does not support MLSD so the caller can
    fall back to NLST+MDTM.
    """
    try:
        entries = list(ftp.mlsd(remote_path, facts=["type", "modify"]))
    except ftplib.error_perm:
        # Server replied with a permission/not-implemented error — no MLSD support.
        return None

    result: list[tuple[str, datetime]] = []
    for name, facts in entries:
        # Skip directories and other non-file entries.
        if facts.get("type", "").lower() != "file":
            continue
        modified_dt = _parse_ftp_timestamp(facts.get("modify", ""))
        result.append((name, modified_dt))
    return result


def _list_via_nlst_mdtm(
    ftp: ftplib.FTP,
    remote_path: str,
) -> list[tuple[str, datetime]]:
    """List files using NLST and retrieve each file's timestamp via MDTM.

    Slower than MLSD because it requires one extra round-trip per file,
    but works with older FTP servers that pre-date RFC 3659.
    """
    names = ftp.nlst(remote_path)
    result: list[tuple[str, datetime]] = []
    for full_path in names:
        filename = PurePosixPath(full_path).name
        try:
            # MDTM response format: '213 YYYYMMDDHHMMSS'
            mdtm_response = ftp.sendcmd(f"MDTM {full_path}")
            timestamp_str = mdtm_response.split()[-1]
            modified_dt = _parse_ftp_timestamp(timestamp_str)
        except ftplib.all_errors:
            # If MDTM is unavailable, treat the file as epoch so it is
            # always considered new and downloaded.
            modified_dt = datetime(1970, 1, 1, tzinfo=timezone.utc)
        result.append((filename, modified_dt))
    return result


def _parse_ftp_timestamp(timestamp_str: str) -> datetime:
    """Parse a FTP MLSD/MDTM timestamp (YYYYMMDDHHMMSS[.sss]) into a UTC datetime.

    Returns the Unix epoch (1970-01-01 UTC) if the string is blank or invalid
    so that files with unparseable timestamps are always treated as new.
    """
    if not timestamp_str:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    # Strip optional sub-second component before parsing.
    timestamp_str = timestamp_str.split(".")[0]
    try:
        dt = datetime.strptime(timestamp_str, "%Y%m%d%H%M%S")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _close_connection(ftp: ftplib.FTP) -> None:
    """Gracefully close an FTP connection, falling back to a hard close on error."""
    try:
        ftp.quit()
    except Exception:
        ftp.close()
