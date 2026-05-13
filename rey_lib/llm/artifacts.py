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

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

__all__ = ["ArtifactStore", "LocalArtifactStore"]


class ArtifactStore(ABC):
    """Abstract base class for artifact storage backends.

    Implementations must be idempotent — writing the same run_id + stage_id
    twice must not raise an error (may overwrite or skip).
    """

    @abstractmethod
    def write(
        self,
        run_id:   str,
        stage_id: str,
        data:     dict[str, Any],
    ) -> str:
        """Write an artifact and return its URI.

        Parameters
        ----------
        run_id : str
            UUID of the ExecutionRecord this artifact belongs to.
        stage_id : str
            Stage identifier — used to namespace the artifact file name.
        data : dict[str, Any]
            Parsed stage output to store.

        Returns
        -------
        str
            URI pointing to the stored artifact (e.g. 'file:///data/artifacts/…').
        """


class LocalArtifactStore(ArtifactStore):
    """Write stage artifacts as JSON files in a local directory.

    Files are named ``<stage_id>.<run_id>.json`` and written under
    ``base_dir``.  The returned URI uses the ``file://`` scheme so it can be
    stored in ExecutionRecord.artifact_uris and opened by any standard tool.

    Parameters
    ----------
    base_dir : Path
        Directory where artifact files are written.  Created on first write.
    """

    def __init__(self, base_dir: Path) -> None:
        """Initialise with the target directory."""
        self._base_dir = Path(base_dir)

    def write(
        self,
        run_id:   str,
        stage_id: str,
        data:     dict[str, Any],
    ) -> str:
        """Write the data as a JSON file and return its file:// URI.

        Parameters
        ----------
        run_id : str
            UUID of the ExecutionRecord.
        stage_id : str
            Stage identifier.
        data : dict[str, Any]
            Parsed stage output.

        Returns
        -------
        str
            Absolute file:// URI of the written artifact.
        """
        self._base_dir.mkdir(parents=True, exist_ok=True)
        safe_stage = stage_id.replace("/", "_").replace("\\", "_")
        path = self._base_dir / f"{safe_stage}.{run_id}.json"
        path.write_text(
            json.dumps(data, indent=2, default=str),
            encoding="utf-8",
        )
        return path.resolve().as_uri()
