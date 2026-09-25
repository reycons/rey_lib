"""Keyed files: every record names its own columns.

JSONL with its alias NDJSON, and JSON. The difference from a delimited file is
not the syntax, it is WHERE the column names live -- and that changes when the
shape can be checked at all. A header is one line and is readable before the
rows; record keys are only knowable once the records exist.

So these validate AFTER reading, and read exactly once. A second parse would be
invisible to every test except the one that counts reads, and it would cost a
full pass over every file on every load.

**JSON IS NOT A KIND OF JSONL.** They share the rule above and nothing else:
JSONL is one object per line, JSON is one document. A table exported as JSON
comes out as an array of rows, usually under a key naming the table; the same
table as JSONL comes out as lines. So both are keyed files, and neither is
defined in terms of the other.
"""

from __future__ import annotations

from typing import Any

from rey_lib.files import file_utils
from rey_lib.files.data_file import data_file
from rey_lib.data.errors import DataStructureError
from rey_lib.files.data_file.base import DataFile, RecordShape
from rey_lib.logs import get_logger

__all__ = ["JsonFile", "JsonlFile", "KeyedFile"]

_logger = get_logger(__name__)


def _record_shape(top_is_list: bool, keys: dict[str, bool]) -> RecordShape:
    """Where the rows are, from a SUMMARY of one JSON document.

    THE POSITIVE RULE, stated once. Two shapes are tables, and they are the
    two a table export produces:

        [{...}, {...}]              the rows on their own
        {"asset": [{...}, {...}]}   the rows under a key naming the table

    REFUSED RATHER THAN SEARCHED. A document with several keys is not a table:
    picking whichever value happens to be a list would make the answer depend
    on key order, and a file holding two exported tables would silently yield
    one of them.

    **IT TAKES A SUMMARY, NOT A DOCUMENT**, and that is what lets one rule
    serve two very different callers. ``_rows`` holds the decoded document and
    summarises it; ``record_shape`` obtains the same summary while decoding,
    without retaining the records. A cheap path that re-implemented the rule
    could drift from the one ``read`` uses -- calling a document a table that
    reading then refuses -- and the summary is the narrowest thing both can
    honestly produce.

    THE SUMMARY IS EVERYTHING THE RULE USES. Nothing about a top-level
    array's CONTENTS appears here, because the rule inspects none: an array
    is records whatever is in it. Whether each element is an object is
    ``_records``'s question, asked only by a caller that wants the rows.

    Args:
        top_is_list: Whether the document's top level is an array.
        keys: For a top-level object, its keys mapped to whether each one's
            value is an array. DUPLICATES MUST ALREADY BE COLLAPSED the way
            ``json.loads`` collapses them -- last occurrence wins -- or
            ``{"asset": [], "asset": []}`` reads as two keys here and as one
            everywhere else.

    Returns:
        The shape. Never raises -- a document that is not a table is an
        answer, and only a caller that needed one explains why.
    """
    if top_is_list:
        return RecordShape(holds_records=True)

    if len(keys) == 1:
        name, value_is_list = next(iter(keys.items()))
        if value_is_list:
            return RecordShape(holds_records=True, record_key=str(name))

    return RecordShape(holds_records=False)


def _summarise(document: Any) -> tuple[bool, dict[str, bool]]:
    """Summarise an already-decoded document for ``_record_shape``.

    The adapter for a caller that already holds the document. Mirrors what
    the decoding path produces, so both reach the rule with the same facts.
    """
    if isinstance(document, dict):
        return False, {
            str(name): isinstance(value, list)
            for name, value in document.items()
        }
    return isinstance(document, list), {}


class KeyedFile(DataFile):
    """A file whose records each name their own fields.

    Everything below the read is shared, because the rule is about the records
    rather than the syntax that produced them. A subtype supplies `read` and
    its own `file_type`, and inherits the rest.
    """

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
            expected = set(expected_columns)

            for position, row in enumerate(records, start=1):
                actual = set(row)
                if actual == expected:
                    continue

                # Column names, never values -- these files carry data.
                _logger.error(
                    "Load key validation failed at record %d of %d\n"
                    "Missing from the record: %s\n"
                    "Not in the destination:  %s",
                    position,
                    len(records),
                    ", ".join(sorted(expected - actual)) or "(none)",
                    ", ".join(sorted(actual - expected)) or "(none)",
                )
                # SHORT, because this is what the run log records. The
                # diagnosis above goes to the logger; putting it here instead
                # would change recorded evidence.
                raise DataStructureError(
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
            # Column names, never values -- these files carry data.
            _logger.error(
                "Record consistency failed at record %d of %d\n"
                "Missing from the record: %s\n"
                "Not in the first record: %s",
                ordinal,
                len(records),
                ", ".join(missing) or "(none)",
                ", ".join(extra) or "(none)",
            )
            detail = ", ".join(
                part for part in (
                    f"missing {missing}" if missing else "",
                    f"unexpected {extra}" if extra else "",
                ) if part
            )
            raise DataStructureError(
                f"'{self.path.name}' record {ordinal} does not match the "
                f"first record's fields: {detail}.",
                validation_name="load_record_consistency",
            )


@data_file("JSONL", "NDJSON")
class JsonlFile(KeyedFile):
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

    def write(self, records: list[dict[str, Any]]) -> int:
        """Write one record per line, through this format's own writer.

        ``write_jsonl_file`` takes the path and the records and renders each
        through the same line renderer every other JSONL writer uses, so a
        record holding the same content is the same bytes whichever produced
        it. Nothing here states a column set: a keyed record carries its own
        names, which is what made this a keyed file on the way in.
        """
        from rey_lib.files.primitive_file_io import write_jsonl_file

        write_jsonl_file(self.path, records)
        _logger.debug("Wrote %d record(s) to %r", len(records), self)
        return len(records)


@data_file("JSON")
class JsonFile(KeyedFile):
    """One JSON document holding a table's rows.

    What a table exported as JSON looks like: an array of flat objects, one
    per row, usually under a single key naming the table --

        {"asset": [{"asset_id": 1, ...}, {"asset_id": 2, ...}]}

    -- and sometimes the array on its own. Both are read; anything else is
    refused by name rather than guessed at.
    """

    @property
    def file_type(self) -> str:
        """JSON. A document, not the line-per-record format."""
        return "JSON"

    def read(self) -> list[dict[str, Any]]:
        """The document's rows, with their JSON types intact.

        The whole document is parsed at once, which is what a document means:
        there is no line to read independently, and half of one is not valid
        JSON.
        """
        from rey_lib.files.json import read_json_file

        return self._rows(read_json_file(self.path, encoding=self.encoding))

    def write(self, records: list[dict[str, Any]]) -> int:
        """Write the rows as one document, through this format's own writer.

        **A BARE ARRAY, which is one of the two shapes ``_rows`` reads.** The
        other is a single key naming the table, and there is nothing here that
        knows what to call it -- a document written under an invented key
        would name a table that may not exist. So the shape that needs no name
        is the one written, and what is written reads back.
        """
        from rey_lib.files.json import write_json_file

        write_json_file(self.path, records)
        _logger.debug("Wrote %d row(s) to %r", len(records), self)
        return len(records)

    def record_shape(self) -> RecordShape:
        """Whether this file holds a table, and where its rows are.

        **The one format that has to look.** Every other DataFile is records
        by construction; a JSON file may equally be a configuration document,
        so the base's answer would be wrong and this reads to find out.

        **It answers about the file; it is not how the rows travel.** A
        consumer that can read the file itself -- DuckDB over a path, say --
        takes this answer and then reads the file, rather than taking the
        records from here. Materialising the rows in Python to hand them on
        would defeat the point of a consumer that reads files directly.

        Returns:
            The shape. A document that is not a table is an ANSWER, not a
            refusal: only a caller that wanted the rows is owed an
            explanation, and ``read`` is what gives one.

        Raises:
            JsonReadError: The file will not parse, or cannot be read.
                DELIBERATELY NOT SOFTENED. Being unparseable is not a shape,
                and reporting it as "no records" would take a real error away
                from every direct caller. A consumer that only wants routing
                information out of this catches it itself -- knowing it is
                choosing to ignore an error, rather than never seeing one.
        """
        import json

        from rey_lib.files.json import JsonReadError

        keys: dict[str, bool] = {}

        def _note(pairs: list[tuple[str, Any]]) -> None:
            """Keep each object's key summary; keep none of the object.

            DUPLICATES COLLAPSE AS ``json.loads`` COLLAPSES THEM -- last
            occurrence wins. Keeping the raw pair sequence would make
            ``{"asset": [...], "asset": [...]}`` read as TWO keys, and so not
            a table, while ``read`` sees the decoder's one-key dict and
            accepts it. That divergence is the one thing this must not
            introduce.

            Every object overwrites this, and the OUTERMOST completes LAST --
            not a decoder implementation detail but the grammar: a parent
            cannot be built before its values are. So what survives the
            decode is the top-level object's keys.
            """
            nonlocal keys
            collapsed: dict[str, bool] = {}
            for name, value in pairs:
                collapsed[str(name)] = isinstance(value, list)
            keys = collapsed
            return None                 # this object is not retained

        try:
            text = self.path.read_text(encoding=self.encoding)
            top = json.loads(text, object_pairs_hook=_note)
        except OSError as exc:
            raise JsonReadError(
                f"Cannot read '{self.path.name}': {exc}"
            ) from exc
        except ValueError as exc:
            raise JsonReadError(
                f"Invalid JSON in '{self.path}': {exc}"
            ) from exc

        return _record_shape(isinstance(top, list), keys)

    def _rows(self, document: Any) -> list[dict[str, Any]]:
        """The rows inside one parsed document.

        Which shapes are tables is ``_record_shape``'s, and is asked here
        rather than restated -- ``record_shape`` answers the same question
        for a caller that only needs the answer, and two copies of that rule
        would eventually disagree about what a table is.

        What stays here is the REFUSAL, because only a caller that wanted the
        rows has to explain why there are none, and the explanation depends on
        exactly how the document failed.

        Raises:
            DataStructureError: When the document is not a table.
        """
        shape = _record_shape(*_summarise(document))
        if not shape.holds_records:
            raise DataStructureError(
                self._why_not_a_table(document),
                validation_name="load_json_shape",
            )

        if shape.record_key is None:
            return self._records(document, "the document")
        return self._records(document[shape.record_key], f"'{shape.record_key}'")

    def _why_not_a_table(self, document: Any) -> str:
        """Say what was found instead of rows, in the reader's terms.

        Reached only once ``json_record_shape`` has answered no, so every
        branch here describes a document that is genuinely not a table. Each
        names the thing that was actually wrong: a reader sent to look at the
        wrong part of a file is worse served than one told nothing.
        """
        if isinstance(document, dict):
            if len(document) == 1:
                # The key is right and its value is not an array. Said as
                # such: "keys are: asset" would describe a document whose key
                # was wrong, and send the reader to look at the wrong thing.
                name, value = next(iter(document.items()))
                return (
                    f"'{self.path.name}' holds one key, '{name}', but it "
                    f"holds a JSON {type(value).__name__} rather than an "
                    "array of rows."
                )
            keys = ", ".join(sorted(str(one) for one in document)) or "(none)"
            return (
                f"'{self.path.name}' holds no single array of rows. A table "
                f"exported as JSON is an array, or one key holding an array; "
                f"this document's keys are: {keys}."
            )

        return (
            f"'{self.path.name}' is a JSON {type(document).__name__}, not a "
            "table: there are no rows in it."
        )

    def _records(self, rows: list[Any], where: str) -> list[dict[str, Any]]:
        """The array's entries, once every one is an object.

        A row that is not an object has no field names, so the rule every
        keyed file is held to cannot be applied to it. Refused here, where the
        position can be named, rather than at the insert.

        Raises:
            DataStructureError: Naming the first entry that is not an
                object, because one is enough to stop the load.
        """
        for ordinal, row in enumerate(rows, start=1):
            if isinstance(row, dict):
                continue
            raise DataStructureError(
                f"'{self.path.name}': entry {ordinal} of {where} is a "
                f"{type(row).__name__}, not an object, so it names no columns.",
                validation_name="load_json_shape",
            )
        return list(rows)
