"""Recognised file formats that hold no records (#1133).

Each is a File whose format is known and that is not a DataFile: it has text,
it can be moved and governed, and it has no records to read. Each holds only
what its format adds; for now that is its type.
"""

from __future__ import annotations

from rey_lib.files.file import File, file_format

__all__ = ["PythonFile", "TextFile", "YamlFile"]


@file_format("PYTHON")
class PythonFile(File):
    """A Python source file."""

    @property
    def file_type(self) -> str:
        """The PYTHON format token."""
        return "PYTHON"


@file_format("YAML")
class YamlFile(File):
    """A YAML document."""

    @property
    def file_type(self) -> str:
        """The YAML format token."""
        return "YAML"


@file_format("TEXT")
class TextFile(File):
    """A plain text file."""

    @property
    def file_type(self) -> str:
        """The TEXT format token."""
        return "TEXT"
