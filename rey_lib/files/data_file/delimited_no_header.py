"""Delimited files that name nothing: every line is data.

The sibling of ``DelimitedHeaderFile``, and the opposite trade. A header file
knows its columns before it reads a row; this one knows its columns only from
somewhere else, because the file itself says nothing about them.

**THE DEFECT THIS EXISTS TO FIX.** ``get_reader`` accepted
``DELIMITED_NO_HEADER`` and dispatched it to the delimited reader, which takes
the first line as the column names -- always. So the first DATA row became the
names and was consumed:

    '1,x\\n2\\n'  ->  [{'1': '2'}]      two rows in, one row out

Silently: nothing raised, and the surviving row was keyed by the values of the
row that vanished. That is why a subtype was deliberately not written over
that reader -- it would have been built on the defect. This one does not use
it. ``open_csv(include_all_rows=True)`` streams every physical row exactly
once, so no line is spent on a header that does not exist.

Where the names come from
-------------------------
Declared, or positional. A file with no header cannot be asked, so:

    columns=[...] declared   the names, in order
    nothing declared         col001 .. colNNN, from the first row's width

Zero-padded to three, so a file wide enough to need them sorts and reads in
order: col002 before col010, which column_2 and column_10 do not. Wider than
999 keeps counting rather than truncating.

Positional names are an honest description of a file whose fields are
identified by POSITION -- which is what a headerless format means. They are
not a guess at what the columns mean.

``read`` still knows nothing of any destination. Names arrive as a format
setting at construction, the same way a delimiter does.
"""

from __future__ import annotations

from typing import Any

from rey_lib.files.data_file import data_file
from rey_lib.files.data_file.base import DataFile, DataFileStructureError
from rey_lib.logs import get_logger

__all__ = ["DelimitedNoHeaderFile"]

_logger = get_logger(__name__)


@data_file("DELIMITED_NO_HEADER")
class DelimitedNoHeaderFile(DataFile):
    """A delimited file whose fields are identified by position."""

    @property
    def file_type(self) -> str:
        """Its own token. It is NOT dispatched as CSV.

        ``DelimitedHeaderFile`` answers to CSV because the delimited reader
        serves it correctly. This format is the one that reader cannot serve,
        so sharing its token would be the confusion that hid the defect.
        """
        return "DELIMITED_NO_HEADER"

    @property
    def declared_columns(self) -> list[str]:
        """The names a caller supplied, or an empty list for none."""
        return [str(name) for name in (self.settings.get("columns") or [])]

    @property
    def declares_structure(self) -> bool:
        """Only when a caller named the columns.

        Declared names ARE a structural declaration -- they say what every
        field is called, in order, without reading anything. Positional
        fallbacks are not: col001..colNNN are derived from the first row's
        width, and a later row could be wider.
        """
        return bool(self.declared_columns)

    def _fields(self) -> list[list[str]]:
        """Every physical row's fields, with nothing taken as a header.

        ``include_all_rows`` is the whole point: it streams the preamble, the
        line the reader would have called a header, and everything after --
        each exactly once. Blank lines are dropped here rather than there,
        matching what the delimited reader has always done with them.

        Raises:
            DataFileStructureError: If the file cannot be read.
        """
        from rey_lib.files.csv import CsvReadError, open_csv

        delimiter = self.settings.get("delimiter")
        try:
            stream = open_csv(
                self.path,
                encoding=self.encoding,
                errors="replace",
                delimiter=delimiter,
                include_all_rows=True,
            )
            return [
                list(row.fields)
                for row in stream.rows
                if any((value or "").strip() for value in row.fields)
            ]
        except (CsvReadError, OSError) as exc:
            raise DataFileStructureError(
                f"Cannot read '{self.path.name}': {exc}"
            ) from exc

    def source_structure(self) -> list[str]:
        """What this file's fields are called.

        Declared names where a caller gave them; otherwise positional ones
        taken from the first row's width, because position is the only
        identity a headerless file has.

        Returns:
            The names, or an empty list for a file with no rows.
        """
        declared = self.declared_columns
        if declared:
            return declared
        rows = self._fields()
        if not rows:
            return []
        return [f"col{position:03d}" for position in range(1, len(rows[0]) + 1)]

    def read(self) -> list[dict[str, Any]]:
        """Every row, keyed by this file's own column names.

        EVERY row: the first one is data, and this is the format where that
        needed saying.
        """
        rows = self._fields()
        if not rows:
            return []
        names = self.source_structure()
        return [dict(zip(names, fields)) for fields in rows]

    def validate(self, expected_columns: list[str] | None = None) -> None:
        """Check the shape, against the destination or against itself.

        **The question is WIDTH, not names.** A file that names nothing cannot
        disagree with a destination by name, so what can be wrong is how many
        fields each row has -- and a row of the wrong width silently loses or
        gains a column when it is zipped to the names.

        With a destination: every row must be exactly as wide as the
        destination has columns. Where names were declared as well, they must
        match the destination in ORDER, since position is what the insert
        relies on.

        With no destination: every row must be as wide as the first. That is
        the only self-consistency a headerless file has.

        Raises:
            DataFileStructureError: Naming the first row that disagrees.
        """
        rows = self._fields()
        if not rows:
            return

        declared = self.declared_columns
        if expected_columns is not None and declared and declared != expected_columns:
            _logger.error(
                "Declared columns do not match the destination for '%s'\n"
                "Expected table columns:\n%s\n\n"
                "Declared columns:\n%s",
                self.path.name,
                ",".join(expected_columns),
                ",".join(declared),
            )
            raise DataFileStructureError(
                "Header mismatch", validation_name="load_header"
            )

        width = (
            len(expected_columns) if expected_columns is not None else len(rows[0])
        )
        for ordinal, fields in enumerate(rows, start=1):
            if len(fields) == width:
                continue
            raise DataFileStructureError(
                f"'{self.path.name}' row {ordinal} has {len(fields)} field(s) "
                f"where {width} were expected, so its values cannot be "
                f"matched to columns by position.",
                validation_name="load_header",
            )

    def read_validated(
        self,
        expected_columns: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Validate the shape, then read.

        Both read the file. A headerless file states its width nowhere but in
        its rows, so unlike a header file there is no cheap check to do first.
        """
        self.validate(expected_columns)
        return self.read()
