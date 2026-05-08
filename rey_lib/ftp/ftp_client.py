"""FTP/FTPS/SFTP connection management and file operations.

Supports three protocols, selected via ftp_cfg.protocol:
  - 'ftp'  — plain FTP (ftplib, stdlib)
  - 'ftps' — FTP with TLS (ftplib.FTP_TLS, stdlib)
  - 'sftp' — SSH File Transfer Protocol (paramiko, optional dependency)

All callers receive an opaque Session object — protocol details are
fully contained within this module. No other module imports ftplib or
paramiko directly.

Public API
----------
ftp_session(ftp_cfg)                    Context manager → Session
list_remote_files(session, path)        List files with timestamps
list_remote_dirs(session, path)         List subdirectory names (for glob expansion)
download_file(session, path, name, dir) Download one file
"""

from __future__ import annotations

import fnmatch
import ftplib
import logging
import stat as stat_module
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Generator

from rey_lib.errors.error_utils import FtpConnectionError, FtpDownloadError

__all__ = ["ftp_session", "list_remote_files", "list_remote_dirs", "download_file"]

log = logging.getLogger(__name__)

_FTP_TIMEOUT_SECONDS = 30
_VALID_PROTOCOLS     = frozenset({"ftp", "ftps", "sftp"})


# ── Session wrapper ───────────────────────────────────────────────────────────

@dataclass
class Session:
    """Opaque wrapper around a live protocol connection.

    Callers never access .conn or .protocol directly — they pass the Session
    to the public functions in this module which handle all protocol dispatch.
    """

    protocol: str   # 'ftp' | 'ftps' | 'sftp'
    conn: Any       # ftplib.FTP, ftplib.FTP_TLS, or paramiko.SFTPClient


# ── Public API ────────────────────────────────────────────────────────────────

@contextmanager
def ftp_session(ftp_cfg: Any) -> Generator[Session, None, None]:
    """Context manager that yields an authenticated Session.

    Dispatches to the correct protocol handler based on ftp_cfg.protocol.
    The session is always closed on exit, even when an exception occurs.

    Args:
        ftp_cfg: Namespace with host, port, user, password, protocol.
                 protocol must be 'ftp', 'ftps', or 'sftp'.

    Yields:
        Session wrapping the live connection.

    Raises:
        FtpConnectionError: If the protocol is invalid or connection fails.
    """
    protocol = str(getattr(ftp_cfg, "protocol", "ftp")).lower()
    if protocol not in _VALID_PROTOCOLS:
        raise FtpConnectionError(
            f"Unknown protocol '{protocol}'. Must be one of: {sorted(_VALID_PROTOCOLS)}"
        )

    log.debug("→ ftp_session [%s] → %s:%s", protocol, ftp_cfg.host, ftp_cfg.port)

    if protocol == "sftp":
        with _sftp_session(ftp_cfg) as session:
            yield session
    elif protocol == "ftps":
        with _ftps_session(ftp_cfg) as session:
            yield session
    else:
        with _ftp_session(ftp_cfg) as session:
            yield session


def list_remote_files(
    session: Session,
    remote_path: str,
) -> list[tuple[str, datetime]]:
    """Return all files in *remote_path* as (filename, modified_utc) tuples.

    Subdirectories are excluded. Protocol-aware.

    Args:
        session:     Active Session from ftp_session().
        remote_path: Absolute remote directory path.

    Returns:
        List of (filename, utc_datetime) tuples; empty if directory is empty.

    Raises:
        FtpConnectionError: If the listing command fails.
    """
    log.debug("list_remote_files [%s]: %s", session.protocol, remote_path)
    try:
        if session.protocol == "sftp":
            return _sftp_list_files(session.conn, remote_path)
        return _ftp_list_files(session.conn, remote_path)
    except Exception as exc:
        raise FtpConnectionError(
            f"Failed to list '{remote_path}': {exc}"
        ) from exc


def list_remote_dirs(
    session: Session,
    remote_path: str,
) -> list[str]:
    """Return subdirectory names directly inside *remote_path*.

    Used by sync_engine to expand glob patterns in remote_paths config.

    Args:
        session:     Active Session from ftp_session().
        remote_path: Absolute remote directory to list.

    Returns:
        List of subdirectory names (basenames only, not full paths).

    Raises:
        FtpConnectionError: If the listing command fails.
    """
    log.debug("list_remote_dirs [%s]: %s", session.protocol, remote_path)
    try:
        if session.protocol == "sftp":
            return _sftp_list_dirs(session.conn, remote_path)
        return _ftp_list_dirs(session.conn, remote_path)
    except Exception as exc:
        raise FtpConnectionError(
            f"Failed to list directories in '{remote_path}': {exc}"
        ) from exc


def download_file(
    session: Session,
    remote_path: str,
    filename: str,
    local_dir: Path,
) -> Path:
    """Download a single file to *local_dir*.

    Partial files are deleted on failure. Protocol-aware.

    Args:
        session:     Active Session from ftp_session().
        remote_path: Absolute remote directory containing the file.
        filename:    Basename of the file to download.
        local_dir:   Local directory to save the file into (created if absent).

    Returns:
        Local Path where the file was saved.

    Raises:
        FtpDownloadError: If the download fails for any reason.
    """
    log.debug("download_file [%s]: %s", session.protocol, filename)
    remote_file = str(PurePosixPath(remote_path) / filename)
    local_file  = local_dir / filename
    local_dir.mkdir(parents=True, exist_ok=True)

    try:
        if session.protocol == "sftp":
            _sftp_download(session.conn, remote_file, local_file)
        else:
            _ftp_download(session.conn, remote_file, local_file)
        log.info("Downloaded: %s → %s", remote_file, local_file)
        return local_file
    except Exception as exc:
        if local_file.exists():
            local_file.unlink()
        raise FtpDownloadError(
            f"Failed to download '{remote_file}': {exc}"
        ) from exc


# ── FTP session ───────────────────────────────────────────────────────────────

@contextmanager
def _ftp_session(ftp_cfg: Any) -> Generator[Session, None, None]:
    """Open a plain FTP connection."""
    ftp: ftplib.FTP | None = None
    try:
        ftp = ftplib.FTP()
        ftp.connect(host=ftp_cfg.host, port=int(ftp_cfg.port), timeout=_FTP_TIMEOUT_SECONDS)
        ftp.login(user=ftp_cfg.user, passwd=ftp_cfg.password)
        log.info("FTP connected: %s@%s", ftp_cfg.user, ftp_cfg.host)
        yield Session(protocol="ftp", conn=ftp)
    except ftplib.all_errors as exc:
        raise FtpConnectionError(
            f"FTP connection failed {ftp_cfg.host}:{ftp_cfg.port} — {exc}"
        ) from exc
    finally:
        if ftp is not None:
            _close_ftp(ftp)
        log.debug("← FTP session closed")


# ── FTPS session ──────────────────────────────────────────────────────────────

@contextmanager
def _ftps_session(ftp_cfg: Any) -> Generator[Session, None, None]:
    """Open an FTP-over-TLS (FTPS) connection."""
    ftp: ftplib.FTP_TLS | None = None
    try:
        ftp = ftplib.FTP_TLS()
        ftp.connect(host=ftp_cfg.host, port=int(ftp_cfg.port), timeout=_FTP_TIMEOUT_SECONDS)
        ftp.login(user=ftp_cfg.user, passwd=ftp_cfg.password)
        # Switch data channel to TLS as well.
        ftp.prot_p()
        log.info("FTPS connected: %s@%s", ftp_cfg.user, ftp_cfg.host)
        yield Session(protocol="ftps", conn=ftp)
    except ftplib.all_errors as exc:
        raise FtpConnectionError(
            f"FTPS connection failed {ftp_cfg.host}:{ftp_cfg.port} — {exc}"
        ) from exc
    finally:
        if ftp is not None:
            _close_ftp(ftp)
        log.debug("← FTPS session closed")


# ── SFTP session ──────────────────────────────────────────────────────────────

@contextmanager
def _sftp_session(ftp_cfg: Any) -> Generator[Session, None, None]:
    """Open an SSH/SFTP connection using paramiko."""
    try:
        import paramiko  # noqa: PLC0415 — optional dependency, imported on demand
    except ImportError as exc:
        raise FtpConnectionError(
            "SFTP requires paramiko. Install it with: pip install paramiko"
        ) from exc

    ssh: paramiko.SSHClient | None = None
    sftp: paramiko.SFTPClient | None = None
    try:
        ssh = paramiko.SSHClient()
        # Accept host keys automatically — for stricter security, load known_hosts.
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            hostname=ftp_cfg.host,
            port=int(ftp_cfg.port),
            username=ftp_cfg.user,
            password=ftp_cfg.password,
            timeout=_FTP_TIMEOUT_SECONDS,
            look_for_keys=False,   # skip local SSH key search — use password only
            allow_agent=False,     # skip SSH agent — use password only
        )
        sftp = ssh.open_sftp()
        log.info("SFTP connected: %s@%s", ftp_cfg.user, ftp_cfg.host)
        yield Session(protocol="sftp", conn=sftp)
    except Exception as exc:
        raise FtpConnectionError(
            f"SFTP connection failed {ftp_cfg.host}:{ftp_cfg.port} — {exc}"
        ) from exc
    finally:
        if sftp is not None:
            sftp.close()
        if ssh is not None:
            ssh.close()
        log.debug("← SFTP session closed")


# ── FTP/FTPS file and directory operations ────────────────────────────────────

def _ftp_list_files(
    ftp: ftplib.FTP,
    remote_path: str,
) -> list[tuple[str, datetime]]:
    """List files in remote_path via FTP. Tries MLSD first, falls back to NLST+MDTM."""
    result = _list_via_mlsd(ftp, remote_path)
    if result is not None:
        return result
    return _list_via_nlst_mdtm(ftp, remote_path)


def _ftp_list_dirs(ftp: ftplib.FTP, remote_path: str) -> list[str]:
    """List subdirectory names in remote_path via FTP."""
    # Try MLSD first — it includes type facts.
    try:
        entries = list(ftp.mlsd(remote_path, facts=["type"]))
        return [
            name for name, facts in entries
            if facts.get("type", "").lower() == "dir" and name not in (".", "..")
        ]
    except ftplib.error_perm:
        pass

    # Fallback: NLST then probe each entry with CWD.
    dirs: list[str] = []
    try:
        names = ftp.nlst(remote_path)
    except ftplib.all_errors:
        return []

    original_dir = ftp.pwd()
    for full_path in names:
        name = PurePosixPath(full_path).name
        if name in (".", ".."):
            continue
        try:
            ftp.cwd(full_path)
            ftp.cwd(original_dir)
            dirs.append(name)
        except ftplib.all_errors:
            pass
    return dirs


def _ftp_download(ftp: ftplib.FTP, remote_file: str, local_file: Path) -> None:
    """Download a file via FTP retrbinary."""
    with local_file.open("wb") as f:
        ftp.retrbinary(f"RETR {remote_file}", f.write)


def _list_via_mlsd(
    ftp: ftplib.FTP,
    remote_path: str,
) -> list[tuple[str, datetime]] | None:
    """List files via MLSD (RFC 3659). Returns None if server does not support it."""
    try:
        entries = list(ftp.mlsd(remote_path, facts=["type", "modify"]))
    except ftplib.error_perm:
        return None

    result: list[tuple[str, datetime]] = []
    for name, facts in entries:
        if facts.get("type", "").lower() != "file":
            continue
        modified_dt = _parse_ftp_timestamp(facts.get("modify", ""))
        result.append((name, modified_dt))
    return result


def _list_via_nlst_mdtm(
    ftp: ftplib.FTP,
    remote_path: str,
) -> list[tuple[str, datetime]]:
    """List files via NLST + per-file MDTM. Slower but works on older servers."""
    names  = ftp.nlst(remote_path)
    result: list[tuple[str, datetime]] = []
    for full_path in names:
        filename = PurePosixPath(full_path).name
        try:
            mdtm_response = ftp.sendcmd(f"MDTM {full_path}")
            timestamp_str = mdtm_response.split()[-1]
            modified_dt   = _parse_ftp_timestamp(timestamp_str)
        except ftplib.all_errors:
            modified_dt = datetime(1970, 1, 1, tzinfo=timezone.utc)
        result.append((filename, modified_dt))
    return result


def _parse_ftp_timestamp(timestamp_str: str) -> datetime:
    """Parse MLSD/MDTM timestamp (YYYYMMDDHHMMSS[.sss]) to UTC datetime."""
    if not timestamp_str:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    timestamp_str = timestamp_str.split(".")[0]
    try:
        dt = datetime.strptime(timestamp_str, "%Y%m%d%H%M%S")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _close_ftp(ftp: ftplib.FTP) -> None:
    """Gracefully close an FTP/FTPS connection."""
    try:
        ftp.quit()
    except Exception:
        ftp.close()


# ── SFTP file and directory operations ───────────────────────────────────────

def _sftp_list_files(sftp: Any, remote_path: str) -> list[tuple[str, datetime]]:
    """List files in remote_path via SFTP using listdir_attr."""
    result: list[tuple[str, datetime]] = []
    for attr in sftp.listdir_attr(remote_path):
        # Skip directories.
        if stat_module.S_ISDIR(attr.st_mode):
            continue
        mtime = attr.st_mtime or 0
        modified_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
        result.append((attr.filename, modified_dt))
    return result


def _sftp_list_dirs(sftp: Any, remote_path: str) -> list[str]:
    """List subdirectory names in remote_path via SFTP."""
    return [
        attr.filename
        for attr in sftp.listdir_attr(remote_path)
        if stat_module.S_ISDIR(attr.st_mode) and attr.filename not in (".", "..")
    ]


def _sftp_download(sftp: Any, remote_file: str, local_file: Path) -> None:
    """Download a file via SFTP get."""
    sftp.get(remote_file, str(local_file))
