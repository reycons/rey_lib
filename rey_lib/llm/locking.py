"""
File-based pipeline execution lock.

Prevents concurrent execution of the same pipeline_id by writing a PID-file
alongside the execution log.  The lock is a lightweight local-process guard —
it does not coordinate across network file systems or container boundaries.

The lock file is removed on clean exit.  If a process dies without releasing
the lock, the next caller detects that the recorded PID is no longer alive
and takes over the lock.

Public API
----------
PipelineLock
    Context manager.  Acquire before pipeline execution, release on exit.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType
from typing import Optional

from rey_lib.llm.exceptions import LockConflict

__all__ = ["PipelineLock"]


class PipelineLock:
    """Context manager that acquires a PID-file lock for a named pipeline.

    The lock file is written at ``<log_stem>.<pipeline_id>.lock`` in the
    same directory as the execution log.

    Parameters
    ----------
    log : Path
        Path to the pipeline JSONL execution log.  The lock file is placed
        in the same directory.
    pipeline_id : str
        Logical pipeline identifier — used to namespace the lock file so
        multiple pipelines sharing a log directory do not block each other.

    Raises
    ------
    LockConflict
        On __enter__ if another living process holds the lock.
    """

    def __init__(self, log: Path, pipeline_id: str) -> None:
        """Initialise the lock targeting the given log and pipeline."""
        log_path        = Path(log)
        safe_id         = pipeline_id.replace("/", "_").replace("\\", "_")
        self._lock_path = log_path.parent / f"{log_path.stem}.{safe_id}.lock"
        self._pipeline_id = pipeline_id

    def __enter__(self) -> "PipelineLock":
        """Acquire the lock or raise LockConflict if another process holds it."""
        if self._lock_path.exists():
            pid = _read_pid(self._lock_path)
            if pid is not None and _pid_is_alive(pid):
                raise LockConflict(
                    f"Pipeline '{self._pipeline_id}' is already running "
                    f"(PID {pid}).  Lock file: {self._lock_path}. "
                    "If the owning process is dead, delete the lock file manually."
                )
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path.write_text(str(os.getpid()), encoding="utf-8")
        return self

    def __exit__(
        self,
        exc_type:  Optional[type[BaseException]],
        exc_val:   Optional[BaseException],
        exc_tb:    Optional[TracebackType],
    ) -> None:
        """Release the lock unconditionally."""
        try:
            self._lock_path.unlink(missing_ok=True)
        except OSError:
            pass  # Best-effort — do not mask the original exception.


def _read_pid(path: Path) -> Optional[int]:
    """Read the PID from a lock file, returning None on parse failure."""
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_is_alive(pid: int) -> bool:
    """Return True if the process with the given PID is currently running."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we cannot signal it — treat as alive.
        return True
