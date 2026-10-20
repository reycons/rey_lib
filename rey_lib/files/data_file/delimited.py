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

from rey_lib.files.file import file_format
from rey_lib.data.errors import DataStructureError
from rey_lib.files.data_file.base import DataFile
from rey_lib.logs import AppLogger

__all__ = ["DelimitedHeaderFile"]

_logger = AppLogger(__name__)


@file_format("CSV", "DELIMITED_HEADER")
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
        """Every row, keyed by the header's column names (#1133: this format's own read).

        Parsing and header location belong to ``rey_lib.files.csv``; what this
        format adds is its reading policy -- blank lines and a header repeated
        mid-file are not data. Which line is the header is one of three format
        settings, the first that applies:

            header_line       that exact whole line, or there are no rows
            required_header   the line holding these column names, wherever
                              it sits in the opening window
            neither           the header rey_lib.files.csv locates by its own rule

        A ``delimiter`` setting of None leaves the delimiter to be detected.
        """
        from rey_lib.files.csv import open_csv

        delimiter = self.settings.get("delimiter", ",")
        header_line = self.settings.get("header_line")
        required = [header_line] if header_line else list(self.settings.get("required_header") or ())
        stream = open_csv(
            self.path,
            encoding=self.encoding,
            errors="replace",
            delimiter=delimiter,
            required_header=required,
            skip_blank_lines=True,
        )
        fieldnames = [name.strip() for name in stream.header_fields]
        if not fieldnames:
            return []
        if header_line is not None and fieldnames != [
                name.strip() for name in header_line.split(delimiter)]:
            return []
        first_column = fieldnames[0]
        rows: list[dict[str, Any]] = []
        for row in stream.rows:
            if not any((value or "").strip() for value in row.fields):
                continue
            record = dict(zip(fieldnames, row.fields))
            if (record.get(first_column, "") or "").strip() == first_column:
                continue
            rows.append(record)
        return rows

    def write(self, rows: list[dict[str, Any]]) -> int:
        """Write the rows, header first, through this format's own writer.

        ``csv.py`` is the declared sole owner of delimited format and
        ``write_delimited_rows`` is its writer; this hands it the path and the
        records and adds nothing. The header and its order come from the first
        record's keys, which is what that writer already reads them from --
        the same order ``declares_structure`` promises on the way in.

        A ``minimal_quoting`` format setting asks for standard quoting of
        fields that need it, instead of writing every value verbatim.

        Raises:
            DataStructureError: When there are no rows: with no first record
                there is no header to write.
        """
        from rey_lib.files.csv import write_delimited_rows

        if not rows:
            raise DataStructureError(f"Cannot write {self!r}: there are no rows.")
        write_delimited_rows(
            self.path, rows, minimal_quoting=bool(self.settings.get("minimal_quoting")),
        )
        _logger.debug("Wrote %d row(s) to %r", len(rows), self)
        return len(rows)

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

        # The diagnosis is failure evidence, so it travels on the error
        # (rule 79), never in an ERROR log line.
        raise DataStructureError(
            f"Header mismatch for '{self.path.name}': expected table columns "
            f"[{','.join(expected_columns)}], actual file columns [{','.join(actual)}]",
            validation_name="load_header",
        )

    def read_validated(
        self,
        expected_columns: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Validate the header, THEN read -- the cheap order for this format."""
        self.validate(expected_columns)
        return self.read()
