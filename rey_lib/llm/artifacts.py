"""
Artifact storage for LLM orchestration outputs.

After a stage executes successfully, its parsed_response can be written to
an artifact store.  The store returns a URI that is recorded in
ExecutionRecord.artifact_uris for later retrieval or audit.

Public API
----------
ArtifactStore
    Abstract base class.  Subclass to implement custom backends (S3, GCS, etc.).
LocalArtifactStore
    Writes JSON files to a local directory.  URI scheme: file://<abs_path>.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from rey_lib.files.file_utils import run_artifact_path, write_file

__all__ = ["ArtifactStore", "LocalArtifactStore"]


class ArtifactStore(ABC):
    """Abstract base class for artifact storage backends.

    LLM artifacts follow the shared run-artifact naming convention like every other
    Rey run-created artifact (SGC_Rey_LLM_Artifact_Naming_Uses_Run_Timestamp): the
    operator-facing filename is ``<stage_id>.<run_timestamp>.<extension>``. The UUID
    ``run_id`` remains the authoritative internal identity (kept in the
    ExecutionRecord and runtime context) but must not drive operator-facing names.
    Implementations must be idempotent — writing the same stage twice must not raise.
    """

    @abstractmethod
    def write(
        self,
        run_id:        str,
        run_timestamp: str,
        stage_id:      str,
        data:          dict[str, Any],
    ) -> str:
        """Write an artifact and return its URI.

        Parameters
        ----------
        run_id : str
            UUID of the ExecutionRecord this artifact belongs to (internal identity;
            not used in the operator-facing filename).
        run_timestamp : str
            Filename-safe ``YYYYMMDD_HHMMSS`` run timestamp used in the artifact name.
        stage_id : str
            Stage identifier — used as the artifact name.
        data : dict[str, Any]
            Parsed stage output to store.

        Returns
        -------
        str
            URI pointing to the stored artifact (e.g. 'file:///data/artifacts/…').
        """


class LocalArtifactStore(ArtifactStore):
    """Write stage artifacts as JSON files in a local directory.

    Files are named ``<stage_id>.<run_timestamp>.json`` through the central naming
    authority (:func:`rey_lib.files.file_utils.run_artifact_path`) and written with
    the shared writer (:func:`rey_lib.files.file_utils.write_file`), exactly like
    every other Rey app writes files — no bespoke naming or writing logic here. The
    returned URI uses the ``file://`` scheme so it can be stored in
    ExecutionRecord.artifact_uris and opened by any standard tool.

    Parameters
    ----------
    base_dir : Path
        Directory where artifact files are written. Created on first write.
    """

    def __init__(self, base_dir: Path) -> None:
        """Initialise with the target directory."""
        self._base_dir = Path(base_dir)

    def write(
        self,
        run_id:        str,
        run_timestamp: str,
        stage_id:      str,
        data:          dict[str, Any],
    ) -> str:
        """Write the data as a JSON file and return its file:// URI.

        The UUID ``run_id`` stays the internal identity (recorded on the
        ExecutionRecord); the operator-facing filename uses ``run_timestamp`` via
        the shared naming authority.

        Parameters
        ----------
        run_id : str
            UUID of the ExecutionRecord (internal identity, not in the filename).
        run_timestamp : str
            Filename-safe ``YYYYMMDD_HHMMSS`` run timestamp.
        stage_id : str
            Stage identifier — used as the artifact name.
        data : dict[str, Any]
            Parsed stage output.

        Returns
        -------
        str
            Absolute file:// URI of the written artifact.
        """
        safe_stage = stage_id.replace("/", "_").replace("\\", "_")
        path = run_artifact_path(self._base_dir, safe_stage, run_timestamp, "json")
        write_file(path, data, "JSON")
        return path.resolve().as_uri()
