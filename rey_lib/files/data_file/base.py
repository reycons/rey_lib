"""What a source file IS, and what it contains.

One object per format, because a format is BEHAVIOUR and not just a reader
function. Adding JSONL to the loader needed three separate procedural changes
-- a reader branch, a different structural rule, and a different point in the
sequence -- because nothing owned what the format means.

**A DataFile knows nothing about databases.** It does not load, and it does
not describe the records a transform will produce. Both belong to the objects
downstream of it, which is what lets a consumer that only wants to READ a file
-- the analysis framework, the Console's viewer -- use this without inheriting
a database contract.

Two questions this object answers, deliberately kept apart
--------------------------------------------------------
``source_structure()``
    What this file physically contains.
``validate(expected_columns)``
    Whether it is sound -- against a destination, or against itself.

A third, ``logical_schema()``, is NOT here: what the produced records contain
is a transform's answer, not a file's. One configured feed in this estate
declares 56 output columns over a 47-field file, and 9 of those columns have
no physical source at all -- constants, context values, a hash. A file cannot
describe them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["DataFile", "RecordShape"]


@dataclass(frozen=True)
class RecordShape:
    """Whether a file holds records, and where inside it they are.

    THREE STATES, and two would lose one of them:

        not records   holds_records=False  record_key=None
        records       holds_records=True   record_key=None
        records       holds_records=True   record_key=<a key>

    ``record_key=""`` CANNOT mean "no key". ``{"": [...]}`` is legal JSON -- a
    document with one key that happens to be empty -- so a caller testing the
    key for truthiness could not tell it from ``[...]`` and would unwrap
    neither. The two are different documents and this says so.

    **This is not "should be opened in a query surface."** It says what the
    file object can represent, and nothing about which viewer should own it.
    A JSONL file holds records and still belongs to its own inspector; that
    is viewer policy, decided elsewhere and free to outrank this.

    Attributes:
        holds_records: Whether this file can present itself as rows.
        record_key: Where they sit inside it, where that is a question the
            format has -- None when the file simply IS its records.
            Meaningless when ``holds_records`` is False.
    """

    holds_records: bool
    record_key: str | None = None

#: What the loader already defaults to, kept so nothing changes by moving.
DEFAULT_ENCODING = "utf-8-sig"


class DataFile(ABC):
    """One source file, and what its format means.

    Construction is a subtype's own; nothing is discovered afterwards.
    """

    def __init__(
        self,
        path: Path,
        *,
        encoding: str = DEFAULT_ENCODING,
        **settings: Any,
    ) -> None:
        """Hold the file and its format settings.

        Args:
            path: The file.
            encoding: How to decode it. The loader's existing default.
            settings: Format-specific settings a subtype declares -- delimiter,
                sheet, field widths. Held rather than interpreted here.
        """
        self.path = Path(path)
        self.encoding = encoding
        self.settings = settings

    @property
    @abstractmethod
    def file_type(self) -> str:
        """The token ``get_reader`` dispatches on for this format."""

    @abstractmethod
    def source_structure(self) -> list[str]:
        """The fields this file physically contains.

        NOT the destination's columns, and not the transform's output. For a
        delimited file this is its header; for a keyed file, the key set its
        records carry.
        """

    @abstractmethod
    def read(self) -> list[dict[str, Any]]:
        """The records, knowing nothing of any destination.

        What a consumer that only wants the data calls. No validation: a
        caller with nothing to validate against should not have to pass
        something meaningless to say so.
        """

    @property
    def declares_structure(self) -> bool:
        """Whether this file STATES its structure, or only exhibits it.

        A header names every column in order before a row is read; a declared
        fixed-width layout does the same. Those files DECLARE. A keyed file
        carries its names on each record and a positional file carries them
        nowhere, so their structure is inferred from whatever was read -- and
        the first record proves nothing about the second.

        A source-specific primitive, not a profile question. What a profile
        does with it -- whether a structural definition counts as complete --
        is the resolver's, which is why this says only what this file knows
        about itself.

        Defaults to False: inferred is the weaker claim, and a format that
        genuinely declares says so.
        """
        return False

    def record_shape(self) -> RecordShape:
        """Whether this file holds records, and where they are.

        THE SEMANTIC QUESTION, asked of the file object rather than guessed
        from its name. A consumer deciding what a path is good for asks this;
        a consumer that can read the file itself then reads the FILE. See
        ``RecordShape`` -- the answer describes the file, not a surface.

        Records by default, because that is already this class's contract:
        a DataFile produces ``list[dict]``, so every format that is one holds
        records. Formats whose record representation is CONDITIONAL override
        this -- ``JsonFile`` is the case, since a JSON file may equally be a
        configuration document with no rows in it.

        Concrete by design, not abstract: a new format that genuinely is a
        DataFile should not have to restate the thing that made it one.

        Returns:
            Records, with nothing to say about where. Never reads the file --
            an override that must look may raise whatever reading raises.
        """
        return RecordShape(holds_records=True)

    @abstractmethod
    def validate(self, expected_columns: list[str] | None = None) -> None:
        """Refuse a file that is not structurally sound.

        **TWO DIFFERENT QUESTIONS, and the argument selects which.**

        ``expected_columns`` given -- DESTINATION COMPATIBILITY.
            Does this file match the table it is going into?

        ``expected_columns`` is None -- INTERNAL CONSISTENCY.
            Is this file coherent with ITSELF? There is no destination, and
            none is pretended.

        **``None`` is not ``[]``.** An empty list is a destination that has no
        columns, which nothing matches. Conflating the two is exactly the
        defect where an absent table answered no columns, a real header was
        compared against them, and the file was rejected as a header mismatch
        against nothing.

        Raises:
            DataStructureError: When the file does not satisfy whichever
                question was asked.
        """

    @abstractmethod
    def read_validated(
        self,
        expected_columns: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """``read()`` and ``validate()`` composed IN THIS FORMAT'S OWN ORDER.

        The only reason this exists. A header is one line and is checked
        BEFORE the rows, so a bad file costs nothing to reject; a keyed source
        names its columns on every record and can only be checked AFTER. A
        caller that sequenced the two itself would be branching on format
        again, which is what this hierarchy exists to remove.

        Implementations must read the source **once**.

        Raises:
            DataStructureError: As ``validate``.
        """

    def sample(self, limit: int) -> list[dict[str, Any]]:
        """Some of the records, for LOOKING AT rather than loading.

        The same role question a ``QuerySource`` answers, and deliberately not
        ``read()``: that one is unbounded because a load must have all of its
        source, and this one is "show me what this would carry".

        CONCRETE AND HONEST ABOUT ITS COST. A file reader is not bounded --
        every format here parses the whole file -- so this reads and then
        takes what was asked for. That is correct rather than cheap, and it is
        stated rather than hidden: a format that can stop early overrides this
        and nothing else changes. A bounded reader invented per format would
        be the same reading implemented twice.

        Args:
            limit: How many records at most.

        Returns:
            Up to ``limit`` records, in the order this file holds them.
        """
        if limit <= 0:
            return []
        return self.read()[:limit]

    def write(self, rows: list[dict[str, Any]]) -> int:
        """Write these records to this file, and say how many.

        **THE TARGET ROLE.** Source and target are ROLES rather than classes,
        so the object that answers "what is this file" answers it whichever
        end of a movement the file is on. A separate destination hierarchy
        would be the same format knowledge written twice, disagreeing the
        first time a format changed.

        Delegation and nothing else. Every format that can be written already
        has a writer -- ``write_delimited_rows``, ``write_json_file``,
        ``write_jsonl_file`` -- and each takes the path and the records. The
        column names and their order come from the records, because that is
        what those writers already read them from; nothing here states a
        schema, and no destination structure is asked for.

        Concrete rather than abstract, and the default is a REFUSAL BY NAME.
        Not every format can be written: a headerless delimited file states
        its width nowhere, and the unmigrated spreadsheet path is not a
        DataFile at all. A format that can be written says so by implementing
        this; one that cannot refuses saying which, in the same shape
        ``data_file_for`` refuses a format it does not know. Making it
        abstract would instead force every reader to answer a question about
        writing.

        Args:
            rows: The records to write. Their first record's keys fix the
                column names and their order.

        Returns:
            Records written.

        Raises:
            ValueError: When this format has no writer.
        """
        raise ValueError(
            f"{type(self).__name__} cannot be written: format "
            f"'{self.file_type}' has no writer, so it can be a load's source "
            f"and not its destination."
        )

    def __repr__(self) -> str:
        """Name the format and the file, for a failure that has to be read."""
        return f"{type(self).__name__}({self.path.name!r})"
