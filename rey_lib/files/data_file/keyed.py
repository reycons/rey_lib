"""Keyed files: every record names its own columns.

JSONL and its alias NDJSON. The difference from a delimited file is not the
syntax, it is WHERE the column names live -- and that changes when the shape
can be checked at all. A header is one line and is readable before the rows;
record keys are only knowable once the records exist.

So this validates AFTER reading, and reads exactly once. A second parse would
be invisible to every test except the one that counts reads, and it would cost
a full pass over every file on every load.
"""

from __future__ import annotations

from typing import Any

from rey_lib.files.data_file import data_file
from rey_lib.files.data_file.base import DataFile, DataFileStructureError
from rey_lib.files import file_utils

__all__ = ["JsonlFile"]


@data_file("JSONL", "NDJSON")
class JsonlFile(DataFile):
    """One JSON object per line, each naming its own fields."""

    @property
    def file_type(self) -> str:
        """JSONL; NDJSON is the same format under another name."""
        return "JSONL"

    def read(self) -> list[dict[str, Any]]:
        """Every record, with its JSON types intact.

        Nothing coerces values to strings and nothing needs to: this format
        carries int, float, bool and null, and discarding that here would be
        a loss the destination cannot undo.
        """
        return list(
            file_utils.get_reader(
                self.path,
                file_type=self.file_type,
                encoding=self.encoding,
            )
        )

    def source_structure(self) -> list[str]:
        """The field names the records carry, taken from the first.

        Requires reading; a keyed file states its shape nowhere else. The
        first record is the file's claim about itself, and ``validate`` is
        what holds the rest to it.
        """
        records = self.read()
        return list(records[0].keys()) if records else []

    def validate(self, expected_columns: list[str] | None = None) -> None:
        """Check every record's key set.

        Reads to do it, because there is nothing else to read. Prefer
        ``read_validated`` where the records are wanted anyway -- it does the
        same checking over one read instead of two.
        """
        self._validate_records(self.read(), expected_columns)

    def read_validated(
        self,
        expected_columns: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Read ONCE, then validate the records that were read."""
        records = self.read()
        self._validate_records(records, expected_columns)
        return records

    def _validate_records(
        self,
        records: list[dict[str, Any]],
        expected_columns: list[str] | None,
    ) -> None:
        """The rule, over records already in hand.

        **EQUALITY, not coverage**, when a destination is given. Nothing
        projects records onto the destination's columns: the column list is
        taken from the first record, so an extra key BECOMES a column against
        a table that has none. A superset is as wrong as a subset.

        **Every record, not the first.** The insert requires every row to
        carry every column, so the first proves nothing about the second, and
        the reader omits an absent field rather than nulling it -- so
        disagreeing records really do arrive.

        **Set, not sequence.** A JSON object's key order is incidental;
        rejecting a file for it would reject one differing only in how its
        producer serialised it.
        """
        if not records:
            return

        if expected_columns is not None:
            # MIGRATION STEP: delegates to the shipped validator rather than
            # reimplementing it, so this revision provably runs the OLD check
            # through the NEW object. The body moves in here next; doing both
            # at once would leave no point at which "no behaviour change" is
            # checkable rather than asserted.
            #
            # Imported here because file_loader imports this package; a
            # module-level import would close the cycle.
            from rey_lib.files.file_loader import _validate_keyed_records

            if not _validate_keyed_records(records, expected_columns):
                raise DataFileStructureError(
                    "Record keys do not match the destination columns",
                    validation_name="load_record_keys",
                )
            return

        # No destination: the FIRST record is the file's claim about itself,
        # and every later record is held to it.
        required = set(records[0])

        for ordinal, record in enumerate(records, start=1):
            keys = set(record)
            if keys == required:
                continue

            missing = sorted(required - keys)
            extra = sorted(keys - required)
            detail = ", ".join(
                part for part in (
                    f"missing {missing}" if missing else "",
                    f"unexpected {extra}" if extra else "",
                ) if part
            )
            raise DataFileStructureError(
                f"'{self.path.name}' record {ordinal} does not match the "
                f"first record's fields: {detail}."
            )
