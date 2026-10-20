"""The data formats: Files with record / data semantics (#1133).

One module per format, each registering itself with ``file_format`` so a new
format needs **no change to any existing module**. Which format a path is, and
the registry that answers it, are ``File``'s (``rey_lib.files.file``);
``DataFile`` adds what records mean.

Public API
----------
DataFile            The record contract, a File.
RecordShape         Whether a file holds records, and where.
data_file_for       COMPATIBILITY DELEGATE ONLY, kept for Console imports and
                    owning no behaviour: ``DataFile.for_path``. Deleted with
                    its last Console importer (backlog 1131).

A structural failure is NOT re-exported here. It is
``rey_lib.data.errors.DataStructureError`` -- a data object's structure being
wrong is not a fact about files, and a convenience alias in this package would
put the file family back in front of it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rey_lib.files.data_file.base import DEFAULT_ENCODING, DataFile, RecordShape

__all__ = [
    "DEFAULT_ENCODING",
    "DataFile",
    "RecordShape",
    "data_file_for",
]


def data_file_for(path: Path | str, **facts: Any) -> DataFile:
    """Compatibility delegate for Console imports (backlog 1131): ``DataFile.for_path``."""
    return DataFile.for_path(path, **facts)
