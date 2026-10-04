"""Untyped files: a physical file no registered format claims (backlog 624).

Every physical file is a DataFile. A file whose declared type or suffix names no
registered format is still a file the estate holds -- it can be inventoried,
moved, governed and kicked out -- so ``data_file_for`` returns one of these
rather than refusing to represent it at all.

What it cannot do is anything that depends on its FORMAT: its records, its
structure, its validation. Each of those refuses by name, with the message
``data_file_for`` used to raise, so an unknown format still fails at the first
operation that needs its content, and nothing ever guesses a reader.

Not registered under any format token: it is what no token matched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rey_lib.errors.error_utils import ConfigError
from rey_lib.files.data_file.base import DataFile, RecordShape

__all__ = ["UntypedFile"]


class UntypedFile(DataFile):
    """A physical file of no registered format: governable, movable, unreadable."""

    def __init__(self, path: Path, *, refusal: str, **facts: Any) -> None:
        """Hold the file and its facts, and the reason its content is refused.

        Args:
            path: The file.
            refusal: Why no format claims it, stated as data_file_for states it.
            facts: The DataFile facts -- encoding and the governed facts.
        """
        super().__init__(path, **facts)
        self._refusal = refusal

    @property
    def file_type(self) -> str:
        """No registered format token: the empty token."""
        return ""

    def source_structure(self) -> list[str]:
        """Refused: with no format there is no structure to state."""
        raise ConfigError(self._refusal)

    def read(self) -> list[dict[str, Any]]:
        """Refused: with no format there is no reader."""
        raise ConfigError(self._refusal)

    def record_shape(self) -> RecordShape:
        """Refused: what this file holds is a fact of its format."""
        raise ConfigError(self._refusal)

    def validate(self, expected_columns: list[str] | None = None) -> None:
        """Refused: nothing can be validated without a format."""
        raise ConfigError(self._refusal)

    def read_validated(
        self,
        expected_columns: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Refused: as ``read``."""
        raise ConfigError(self._refusal)

    def write(self, rows: list[dict[str, Any]]) -> int:
        """Refused: there is no writer for an unknown format."""
        raise ConfigError(self._refusal)
