"""Delimited files whose first line names their columns.

The header is what makes this format cheap to refuse. It is one line, so a
file whose shape is wrong can be rejected without parsing a single row --
which is why ``read_validated`` here validates FIRST and reads after, the
opposite of a keyed source.

**Headerless delimited files are its sibling**, in
``delimited_no_header.py``. They were deliberately absent while the only
reader took the first line as a header whatever the declared type -- a subtype
over that would have been built on a defect. ``DelimitedNoHeaderFile`` does
not use that reader, which is how it reads every line as data.
"""

from __future__ import annotations

from typing import Any

from rey_lib.files import file_utils
from rey_lib.files.data_file import data_file
from rey_lib.data.errors import DataStructureError
from rey_lib.files.data_file.base import DataFile
from rey_lib.logs import get_logger

__all__ = ["DelimitedHeaderFile"]

_logger = get_logger(__name__)


@data_file("CSV", "DELIMITED_HEADER")
class DelimitedHeaderFile(DataFile):
    """A delimited file whose first line names its columns, in order."""

    @property
    def file_type(self) -> str:
        """Dispatched as CSV, which is what the delimited reader answers to."""
        return "CSV"

    @property
    def declares_structure(self) -> bool:
        """The header is the declaration: every column, in order, before any
        row is read."""
        return True

    def source_structure(self) -> list[str]:
        """The header line's fields, in the order it lists them.

        Read directly rather than through the reader: this is one line, and
        the point of having a header is not having to parse the file to learn
        the shape.

        Returns:
            The declared columns, or an empty list for a file with no content.

        Raises:
            DataStructureError: If the file cannot be read.
        """
        try:
            with self.path.open(encoding=self.encoding, errors="replace") as fh:
                for line in fh:
                    stripped = line.strip()
                    if stripped:
                        return stripped.split(self.settings.get("delimiter", ","))
        except OSError as exc:
            raise DataStructureError(
                f"Cannot read '{self.path.name}': {exc}"
            ) from exc
        return []

    def read(self) -> list[dict[str, Any]]:
        """Every row, keyed by the header's column names."""
        return list(
            file_utils.get_reader(
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
        if expected_columns is None:
            if not self.source_structure():
                raise DataStructureError(
                    f"'{self.path.name}' has no header line, so its columns "
                    "cannot be established."
                )
            return

        try:
            actual = self.source_structure()
        except DataStructureError as exc:
            # An unreadable file was recorded as a header failure before this
            # moved, so it still is. The message says what actually happened;
            # only the run log's NAME is preserved, because changing recorded
            # evidence is not this step's business.
            raise DataStructureError(
                str(exc), validation_name="load_header"
            ) from exc

        if actual == expected_columns:
            return

        _logger.error(
            "Load header validation failed for '%s'\n"
            "Expected table columns:\n%s\n\n"
            "Actual file columns:\n%s",
            self.path.name,
            ",".join(expected_columns),
            ",".join(actual),
        )
        # SHORT, because this is what the run log records. The diagnosis above
        # goes to the logger; putting it here instead would change recorded
        # evidence.
        raise DataStructureError(
            "Header mismatch", validation_name="load_header"
        )

    def read_validated(
        self,
        expected_columns: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Validate the header, THEN read -- the cheap order for this format."""
        self.validate(expected_columns)
        return self.read()
