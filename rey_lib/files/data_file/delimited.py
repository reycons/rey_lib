"""Delimited files whose first line names their columns.

The header is what makes this format cheap to refuse. It is one line, so a
file whose shape is wrong can be rejected without parsing a single row --
which is why ``read_validated`` here validates FIRST and reads after, the
opposite of a keyed source.

**Headerless delimited files are deliberately absent.** ``get_reader`` accepts
DELIMITED_NO_HEADER and dispatches it to the same reader, which always takes
the first line as a header -- so the first DATA row is silently consumed. A
subtype over that would be building on a defect; see
delimited_no_header_silently_eats_the_first_row.
"""

from __future__ import annotations

from typing import Any

from rey_lib.files.data_file import data_file
from rey_lib.files.data_file.base import DataFile, DataFileStructureError
from rey_lib.files.file_utils import get_reader

__all__ = ["DelimitedHeaderFile"]


@data_file("CSV", "DELIMITED_HEADER")
class DelimitedHeaderFile(DataFile):
    """A delimited file whose first line names its columns, in order."""

    @property
    def file_type(self) -> str:
        """Dispatched as CSV, which is what the delimited reader answers to."""
        return "CSV"

    def source_structure(self) -> list[str]:
        """The header line's fields, in the order it lists them.

        Read directly rather than through the reader: this is one line, and
        the point of having a header is not having to parse the file to learn
        the shape.

        Returns:
            The declared columns, or an empty list for a file with no content.

        Raises:
            DataFileStructureError: If the file cannot be read.
        """
        try:
            with self.path.open(encoding=self.encoding, errors="replace") as fh:
                for line in fh:
                    stripped = line.strip()
                    if stripped:
                        return stripped.split(self.settings.get("delimiter", ","))
        except OSError as exc:
            raise DataFileStructureError(
                f"Cannot read '{self.path.name}': {exc}"
            ) from exc
        return []

    def read(self) -> list[dict[str, Any]]:
        """Every row, keyed by the header's column names."""
        return list(
            get_reader(
                self.path,
                file_type=self.file_type,
                encoding=self.encoding,
                delimiter=self.settings.get("delimiter", ","),
            )
        )

    def validate(self, expected_columns: list[str] | None = None) -> None:
        """Check the header, against the destination or against itself.

        With a destination, the comparison is ORDERED: a header is an ordered
        artifact, and the column order is what the insert relies on.

        With no destination the header simply has to exist. A delimited file
        with no header has no internal consistency to check -- every row is
        read against whatever the first line declared, so there is nothing a
        later row can contradict.
        """
        actual = self.source_structure()

        if expected_columns is None:
            if not actual:
                raise DataFileStructureError(
                    f"'{self.path.name}' has no header line, so its columns "
                    "cannot be established."
                )
            return

        if actual != expected_columns:
            raise DataFileStructureError(
                f"'{self.path.name}' does not match the destination.\n"
                f"Expected: {','.join(expected_columns)}\n"
                f"Found:    {','.join(actual)}"
            )

    def read_validated(
        self,
        expected_columns: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Validate the header, THEN read -- the cheap order for this format."""
        self.validate(expected_columns)
        return self.read()
