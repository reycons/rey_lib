"""Structured answers about one delimited source file.

A caller supplies a path and criteria — encoding, an optional delimiter,
required header text, a sample limit — and receives one structured result
describing the file. It never receives an algorithm to run, and it never needs
to know how a header is found, how a delimiter is chosen, or how a sample is
selected.

Questions this module answers
-----------------------------
Does the file have a header, and where is it? Does that header contain the
text the caller requires? Which delimiter is in use? Which physical lines are
data rows? How many rows are blank, and how many are ragged? What are the
parsed records? What sample satisfies the supplied limit?

Every answer carries the physical line number it came from, so a caller can
point at the source without re-reading it. One read serves every question:
the file is opened once and held in memory.

What this module does not decide
--------------------------------
What any field means, which columns are sensitive, or what the caller does
with the result. Redaction, profiling, and application artifacts belong to the
caller. This is not an authorization boundary: the caller resolves and
authorizes the path.
"""

from __future__ import annotations

import csv as _csv
import io
import random as _random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from rey_lib.encryption import sha256_text
from rey_lib.errors.error_utils import AppError
from rey_lib.files.file_utils import read_text_file

__all__ = [
    "CsvRead",
    "CsvStream",
    "CsvReadError",
    "CsvRow",
    "HeaderMatch",
    "looks_like_csv",
    "normalized_header",
    "open_csv",
    "parse_delimited_line",
    "read_csv",
    "read_csv_text",
    "render_delimited_line",
    "sample_indices",
]

# Header discovery is bounded to the opening rows. Every line is already in
# memory, so this bounds the comparison work rather than the read.
_HEADER_SEARCH_ROWS = 50

# A column needs this many values below a candidate before its agreement, or
# a candidate's break from it, means anything.
_MINIMUM_CONTRAST_ROWS = 2

# A streaming read buffers this many lines to determine the delimiter and
# locate the header. Header discovery is already bounded to the opening rows,
# so this holds everything that decision can consider and nothing more.
_PROLOGUE_LINES = _HEADER_SEARCH_ROWS * 2

# Tried in this order, so an ambiguous file resolves the same way every run.
_CANDIDATE_DELIMITERS: tuple[str, ...] = (",", "\t", ";", "|")


class CsvReadError(AppError):
    """Raised when a delimited source cannot be read or decoded."""


@dataclass(frozen=True)
class HeaderMatch:
    """Whether one piece of caller-required header text was found."""

    text: str
    found: bool
    field: str | None = None


@dataclass(frozen=True)
class CsvRow:
    """One parsed row, positioned in the source file."""

    physical_line_number: int
    text: str
    fields: tuple[str, ...]
    field_count: int
    is_ragged: bool
    is_blank: bool
    is_header_candidate: bool


@dataclass(frozen=True)
class CsvRead:
    """Everything one read can answer about a delimited source."""

    path: str
    encoding: str
    delimiter: str
    delimiter_supplied: bool
    has_header: bool
    header_line_number: int | None
    header_fields: tuple[str, ...]
    header_matches: tuple[HeaderMatch, ...]
    header_matched_all: bool
    all_rows: tuple[CsvRow, ...]
    rows: tuple[CsvRow, ...]
    data_line_numbers: tuple[int, ...]
    total_line_count: int
    blank_row_count: int
    ragged_row_count: int
    sample: tuple[CsvRow, ...]
    source_text_sha256: str


@dataclass(frozen=True)
class CsvStream:
    """A delimited file's structure, with its data rows still unread."""

    path: str
    encoding: str
    delimiter: str
    has_header: bool
    header_line_number: int | None
    header_fields: tuple[str, ...]
    header_matches: tuple[HeaderMatch, ...]
    header_matched_all: bool
    rows: Iterator[CsvRow]


def open_csv(
    path: Path | str,
    *,
    encoding: str = "utf-8",
    errors: str = "strict",
    delimiter: str | None = None,
    required_header: Sequence[str] = (),
    skip_blank_lines: bool = False,
    include_all_rows: bool = False,
) -> CsvStream:
    """Answer a file's structure, then stream its rows without holding them.

    For a caller that processes rows one at a time and must not pay for the
    whole file in memory. The structural decisions are the same ones
    :func:`read_csv` makes, reached by the same functions: the delimiter is
    determined and the header located from a bounded opening window, which is
    all header discovery ever examines. Only the data rows stream.

    The returned ``rows`` iterator holds the file open until it is exhausted
    or closed, and each row carries its physical line number as it would from
    a whole-file read.

    Counts that require seeing every row — blank, ragged, total — are not
    available here; a caller needing those wants :func:`read_csv`.

    ``include_all_rows`` widens what streams without changing what is decided.
    By default the iterator yields data rows only, as it always has. Set it to
    stream every physical source row exactly once instead — the preamble above
    the header, blank lines, the located header row itself, and everything
    after. The structural decisions are untouched: the delimiter is detected the
    same way, the header is located in the same window, and ``header_fields``
    and ``header_line_number`` report the same values in both modes. Only the
    caller that must account for every line in the file needs it.

    ``skip_blank_lines`` remains independent and still applies, so a caller
    requiring one row per physical line leaves it unset.
    """
    source_path = Path(path)
    try:
        handle = source_path.open(encoding=encoding, errors=errors, newline="")
    except OSError as exc:
        raise CsvReadError(f"Cannot read '{source_path}': {exc}") from exc

    required = tuple(str(text) for text in required_header if str(text).strip())
    prologue: list[str] = []
    try:
        for line in handle:
            prologue.append(line.rstrip("\n").rstrip("\r"))
            if len(prologue) >= _PROLOGUE_LINES:
                break
    except (OSError, UnicodeError) as exc:
        handle.close()
        raise CsvReadError(f"Cannot read '{source_path}': {exc}") from exc

    resolved = delimiter if delimiter is not None else _detect_delimiter(prologue)
    parsed = [parse_delimited_line(line, resolved) for line in prologue]
    header_index = _locate_header(prologue, parsed, required)
    header_fields = () if header_index is None else tuple(parsed[header_index])
    matches = _evaluate_header(header_fields, required)

    return CsvStream(
        path=str(source_path),
        encoding=encoding,
        delimiter=resolved,
        has_header=header_index is not None,
        header_line_number=None if header_index is None else header_index + 1,
        header_fields=header_fields,
        header_matches=matches,
        header_matched_all=all(match.found for match in matches),
        rows=_stream_rows(
            handle,
            prologue,
            header_index,
            len(header_fields),
            resolved,
            skip_blank_lines,
            include_all_rows,
        ),
    )


def _stream_rows(
    handle: Any,
    prologue: list[str],
    header_index: int | None,
    expected_width: int,
    delimiter: str,
    skip_blank_lines: bool,
    include_all_rows: bool = False,
) -> Iterator[CsvRow]:
    """Yield rows, buffered prologue first, then the rest of the file.

    Data rows only by default. With ``include_all_rows`` the first yielded row
    is the file's first physical line, so nothing above the header is skipped.
    """
    first_data_index = (
        0 if include_all_rows or header_index is None else header_index + 1
    )
    try:
        index = 0
        for line in prologue:
            if index >= first_data_index:
                row = _row(line, index, expected_width, delimiter)
                if not (row.is_blank and skip_blank_lines):
                    yield row
            index += 1
        for raw in handle:
            line = raw.rstrip("\n").rstrip("\r")
            row = _row(line, index, expected_width, delimiter)
            if not (row.is_blank and skip_blank_lines):
                yield row
            index += 1
    finally:
        handle.close()


def _row(
    line: str,
    index: int,
    expected_width: int,
    delimiter: str,
) -> CsvRow:
    """Describe one physical line exactly as a whole-file read would."""
    is_blank = not line.strip()
    fields = tuple(parse_delimited_line(line, delimiter))
    return CsvRow(
        physical_line_number=index + 1,
        text=line,
        fields=fields,
        field_count=len(fields),
        is_ragged=bool(expected_width) and not is_blank and len(fields) != expected_width,
        is_blank=is_blank,
        is_header_candidate=_is_header_candidate(list(fields)),
    )


def read_csv(
    path: Path | str,
    *,
    encoding: str = "utf-8",
    errors: str = "strict",
    delimiter: str | None = None,
    required_header: Sequence[str] = (),
    sample_size: int = 0,
    skip_blank_lines: bool = False,
) -> CsvRead:
    """Read one delimited file and answer every structural question about it.

    Opening and decoding happen here; every structural answer comes from
    :func:`read_csv_text`, so a file and a string in hand are analysed by one
    code path and cannot disagree.

    Parameters
    ----------
    path : Path | str
        Source file, already resolved and authorized by the caller.
    encoding : str
        Character encoding, for example ``'utf-8-sig'``.
    errors : str
        Decoding error policy, as :func:`open`. ``'replace'`` for a caller that
        must not fail on undecodable bytes.
    delimiter : str | None
        The delimiter to use. ``None`` asks this module to determine it.
    required_header : Sequence[str]
        Header text the caller requires. Each entry is reported in
        ``header_matches``; a line containing them is preferred when locating
        the header. Supplying criteria never obliges the caller to search.
    sample_size : int
        Maximum records to return in ``sample``. ``0`` returns no sample;
        a limit at or above the row count returns every row.
    skip_blank_lines : bool
        Exclude blank lines from ``rows`` and ``data_line_numbers``. They are
        counted in ``blank_row_count`` either way.

    Returns
    -------
    CsvRead
        Header identity and position, delimiter, parsed data rows with their
        physical line numbers, blank and ragged counts, and the sample.

    Raises
    ------
    CsvReadError
        If the file cannot be read or decoded. A missing or ambiguous header
        is an answer, not an error: ``has_header`` is False.
    """
    source_path = Path(path)
    try:
        source_text = read_text_file(source_path, encoding=encoding, errors=errors)
    except OSError as exc:
        raise CsvReadError(f"Cannot read '{source_path}': {exc}") from exc

    return read_csv_text(
        source_text,
        delimiter=delimiter,
        required_header=required_header,
        sample_size=sample_size,
        skip_blank_lines=skip_blank_lines,
        source=str(source_path),
        encoding=encoding,
    )


def read_csv_text(
    text: str,
    *,
    delimiter: str | None = None,
    required_header: Sequence[str] = (),
    sample_size: int = 0,
    skip_blank_lines: bool = False,
    source: str = "",
    encoding: str = "utf-8",
) -> CsvRead:
    """Answer every structural question about delimited text already in hand.

    The whole analysis lives here: delimiter determination, header location,
    header matching, row positions, blank and ragged counting, and sampling.
    :func:`read_csv` is this function plus opening a file, so nothing can be
    true of a file that is not true of its content.

    ``source`` and ``encoding`` are recorded on the result for a caller that
    has them; neither affects the analysis.
    """
    lines = text.splitlines()
    source_text_sha256 = sha256_text(text)
    required = tuple(str(text) for text in required_header if str(text).strip())

    if not lines:
        return CsvRead(
            path=source,
            encoding=encoding,
            delimiter=delimiter or _CANDIDATE_DELIMITERS[0],
            delimiter_supplied=delimiter is not None,
            has_header=False,
            header_line_number=None,
            header_fields=(),
            header_matches=tuple(
                HeaderMatch(text=text, found=False) for text in required
            ),
            header_matched_all=not required,
            all_rows=(),
            rows=(),
            data_line_numbers=(),
            total_line_count=0,
            blank_row_count=0,
            ragged_row_count=0,
            sample=(),
            source_text_sha256=source_text_sha256,
        )

    resolved_delimiter = (
        delimiter if delimiter is not None else _detect_delimiter(lines)
    )
    parsed = [parse_delimited_line(line, resolved_delimiter) for line in lines]
    header_index = _locate_header(lines, parsed, required)

    header_fields: tuple[str, ...] = ()
    if header_index is not None:
        header_fields = tuple(parsed[header_index])
    matches = _evaluate_header(header_fields, required)

    expected_width = len(header_fields)
    first_data_index = 0 if header_index is None else header_index + 1

    # Every physical line is described once. The data region is a projection of
    # that, so a caller reporting on the whole file and a caller processing
    # only data rows read the same facts.
    all_rows: list[CsvRow] = []
    rows: list[CsvRow] = []
    data_line_numbers: list[int] = []
    blank_row_count = 0
    ragged_row_count = 0
    for index, line in enumerate(lines):
        is_blank = not line.strip()
        fields = tuple(parsed[index])
        in_data_region = index >= first_data_index
        is_ragged = (
            in_data_region
            and bool(expected_width)
            and not is_blank
            and len(fields) != expected_width
        )
        row = CsvRow(
            physical_line_number=index + 1,
            text=line,
            fields=fields,
            field_count=len(fields),
            is_ragged=is_ragged,
            is_blank=is_blank,
            is_header_candidate=_is_header_candidate(list(fields)),
        )
        all_rows.append(row)
        if not in_data_region:
            continue
        # A blank line above the header is structural noise, not a blank data
        # row, so only the data region is counted.
        if is_blank:
            blank_row_count += 1
            if skip_blank_lines:
                continue
        if is_ragged:
            ragged_row_count += 1
        rows.append(row)
        data_line_numbers.append(index + 1)

    return CsvRead(
        path=source,
        encoding=encoding,
        delimiter=resolved_delimiter,
        delimiter_supplied=delimiter is not None,
        has_header=header_index is not None,
        header_line_number=None if header_index is None else header_index + 1,
        header_fields=header_fields,
        header_matches=matches,
        header_matched_all=all(match.found for match in matches),
        all_rows=tuple(all_rows),
        rows=tuple(rows),
        data_line_numbers=tuple(data_line_numbers),
        total_line_count=len(lines),
        blank_row_count=blank_row_count,
        ragged_row_count=ragged_row_count,
        sample=_sample(tuple(rows), sample_size),
        source_text_sha256=source_text_sha256,
    )


def _detect_delimiter(lines: list[str]) -> str:
    """Return the delimiter whose parse is most consistent across the file.

    Each candidate is scored by how many non-blank rows agree on a field count
    greater than one. Ties resolve by candidate order, so the same file always
    yields the same delimiter.
    """
    sample = [line for line in lines if line.strip()][:_HEADER_SEARCH_ROWS]
    if not sample:
        return _CANDIDATE_DELIMITERS[0]

    best_delimiter = _CANDIDATE_DELIMITERS[0]
    best_score = (0, 0)
    for candidate in _CANDIDATE_DELIMITERS:
        widths = [len(parse_delimited_line(line, candidate)) for line in sample]
        multi = [width for width in widths if width > 1]
        if not multi:
            continue
        agreement = max(multi.count(width) for width in set(multi))
        score = (agreement, max(multi))
        if score > best_score:
            best_score = score
            best_delimiter = candidate
    return best_delimiter


def _locate_header(
    lines: list[str],
    parsed: list[list[str]],
    required: tuple[str, ...],
) -> int | None:
    """Return the header's index, or None when the file has no header.

    Caller-required text is a locator criterion: the first line containing all
    of it is the header. Otherwise the header is the row whose width the
    following rows agree with most consistently, and which reads like column
    names. An absent or ambiguous header returns None — the caller asked
    whether there is one, so that is an answer rather than a failure.
    """
    if required:
        for index, line in enumerate(lines):
            if all(text in line for text in required):
                return index

    candidates: list[tuple[tuple[int, ...], int, int]] = []
    non_blank = [index for index, line in enumerate(lines) if line.strip()]
    for index in non_blank[:_HEADER_SEARCH_ROWS]:
        fields = parsed[index]
        if not _is_header_candidate(fields):
            continue
        following = [parsed[later] for later in non_blank if later > index]
        if not following:
            continue
        matching_width = [row for row in following if len(row) == len(fields)]
        if not matching_width:
            continue
        consistency = (len(matching_width) * 1000) // len(following)
        lexical = sum(_looks_like_header_name(field) for field in fields)
        # A row of plausible words is not yet a header. What separates one from
        # a title of the same width is that a header does not read like the
        # values beneath it: labels over dates are not dates, labels over
        # account codes do not share their shape. A title padded to the table's
        # width has more rows agreeing with it than the real header does — the
        # header is one of them — so width alone picks the title.
        contrast = _breaks_from_columns(fields, matching_width)
        distinct = _distinct_name_count(fields)
        named = _usable_name_count(fields)
        # A header may be repeated, but it cannot sit above a *different*
        # header. A title padded to the table's width has the real header among
        # the rows beneath it, and outscores it on width for that very reason.
        leads_another = _leads_a_different_header(fields, matching_width)
        # Position carries weight in its own right: a file's first usable row
        # is its header until something disproves it. Only the disqualifiers
        # outrank it — a row that leads a different header, or that reads like
        # the values beneath it, has been disproved and loses to a later row.
        # Everything after position only separates rows that are otherwise
        # equal, which no two rows are, so a candidate is never abandoned for
        # being indistinguishable.
        candidates.append((
            (
                1 - leads_another,
                contrast,
                -index,
                named,
                distinct,
                len(matching_width),
                consistency,
                lexical,
            ),
            index,
            len(fields),
        ))

    if not candidates:
        return None
    # A header names every column of its table, so a candidate narrower than
    # the widest one on offer is not naming this table. Prose carrying two
    # commas parses as three fields and reads like column names; it loses here
    # on width rather than on any judgement about its wording.
    widest = max(width for _, _, width in candidates)
    candidates = [item for item in candidates if item[2] == widest]
    candidates.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    best_score, best_index, _ = candidates[0]
    if len(candidates) > 1 and candidates[1][0] == best_score:
        # Two rows are equally plausible; claiming either would be a guess.
        return None
    return best_index


def _evaluate_header(
    header_fields: tuple[str, ...],
    required: tuple[str, ...],
) -> tuple[HeaderMatch, ...]:
    """Report whether each required text appears in the located header."""
    matches: list[HeaderMatch] = []
    for text in required:
        exact = next(
            (field for field in header_fields if field.strip() == text), None
        )
        if exact is not None:
            matches.append(HeaderMatch(text=text, found=True, field=exact))
            continue
        contains = next((field for field in header_fields if text in field), None)
        matches.append(
            HeaderMatch(
                text=text, found=contains is not None, field=contains
            )
        )
    return tuple(matches)


def _sample(rows: tuple[CsvRow, ...], size: int) -> tuple[CsvRow, ...]:
    """Return a bounded, structurally representative sample in file order."""
    if size <= 0:
        return ()
    return tuple(rows[index] for index in sample_indices(len(rows), size))


def sample_indices(total: int, size: int) -> list[int]:
    """Randomly select distinct row indices in randomized evidence order.

    The caller first supplies the eligible row count.  Selection is then made
    without replacement across that complete population. The randomized order
    is retained because downstream column evidence is bounded: restoring file
    order would make those bounded values favor the beginning of the file even
    though the selected population itself was uniform.
    """
    population_size = max(total, 0)
    if population_size == 0:
        return []
    sample_size = population_size if size <= 0 else min(population_size, size)
    return _random.sample(range(population_size), sample_size)


def parse_delimited_line(
    line: str,
    delimiter: str,
    *,
    strict: bool = False,
) -> list[str]:
    """Parse one in-memory source line with the given delimiter.

    ``strict`` rejects malformed quoting instead of recovering from it. The
    error carries only what this module knows — the parse failure itself — so
    the caller adds the path and line number it is reporting against, and
    decides whether that is fatal or collected.

    Fields are returned exactly as the source holds them. Cleaning control
    characters is a governed workflow step, not something a reader does on its
    own: a caller that has not asked for it must see what the file actually
    contains, and the step that has asked applies
    :func:`rey_lib.files.text.clean_text_value` itself.
    """
    try:
        return list(next(_csv.reader([line], delimiter=delimiter, strict=strict), []))
    except _csv.Error as exc:
        raise CsvReadError(str(exc)) from exc


def looks_like_csv(text: str, delimiter: str | None = None) -> bool:
    """Return whether ``text`` reads as delimited records.

    Not a separate heuristic: the text is analysed by :func:`read_csv_text`,
    and the answer is whether that analysis found a header of more than one
    field. Content the reader handles is therefore recognised here — a title
    line above the header included — because it is the same analysis.

    Name the ``delimiter`` to ask about exactly that separator, or leave it
    unset to have it determined from the common candidates: comma, tab,
    semicolon, pipe.
    """
    try:
        read = read_csv_text(text, delimiter=delimiter)
    except CsvReadError:
        return False
    return read.has_header and len(read.header_fields) > 1


def normalized_header(header_fields: Sequence[str]) -> list[str]:
    """Return the normalised snake_case column names, preserving order.

    Header names are format, not meaning: this says what a column is called
    once punctuation and case stop mattering, never what its values are. The
    normalisation rule itself belongs to profiling, so it is borrowed rather
    than restated.
    """
    # Imported here because rey_lib.profiling re-exports this function; a
    # module-level import would close the cycle.
    from rey_lib.profiling.value_patterns import normalize_name

    return [normalize_name(name) for name in header_fields]


def render_delimited_line(fields: Sequence[str], delimiter: str) -> str:
    """Render one row as a delimited line, quoting exactly as parsing expects.

    The inverse of :func:`parse_delimited_line`, so a value written here reads
    back as the same value. No line terminator is appended.
    """
    output = io.StringIO(newline="")
    _csv.writer(output, delimiter=delimiter, lineterminator="").writerow(list(fields))
    return output.getvalue()


def _usable_name_count(fields: list[str]) -> int:
    """Return how many of a candidate's columns carry a usable name.

    An unnamed column does not disqualify a header, but a header naming all of
    its columns is the better one where both are on offer.
    """
    return sum(
        1
        for field in fields
        if re.sub(r"[^a-z0-9]+", "_", field.strip().lower()).strip("_")
    )


def _distinct_name_count(fields: list[str]) -> int:
    """Return how many of a candidate's column names are distinct.

    Repeated names do not disqualify a header, but a header whose names are all
    distinct is the better one where both are on offer.
    """
    return len({
        re.sub(r"[^a-z0-9]+", "_", field.strip().lower()).strip("_")
        for field in fields
    })


def _leads_a_different_header(
    fields: list[str],
    following: list[list[str]],
) -> int:
    """Whether a different header row sits among the rows below a candidate.

    The repeated header of a report is the same header and is not counted.
    Descriptive rows under a header also read like column names, so a differing
    row only disqualifies the candidate when the candidate reads like it too: a
    title above the real header shares its words and case, while a header above
    its own descriptions does not.
    """
    identity = tuple(field.strip() for field in fields)
    for row in following:
        if not _is_header_candidate(row):
            continue
        if tuple(field.strip() for field in row) == identity:
            continue
        if not _breaks_from_row(fields, row):
            return 1
    return 0


def _breaks_from_row(fields: list[str], other: list[str]) -> bool:
    """Whether a candidate reads differently from one other row."""
    compared = 0
    breaks = 0
    for column, label in enumerate(fields):
        token = label.strip()
        if column >= len(other):
            continue
        value = other[column].strip()
        if not token or not value:
            continue
        compared += 1
        if _breaks_from_values(token, [value, value]):
            breaks += 1
    return bool(compared) and breaks * 2 >= compared


def _breaks_from_columns(
    fields: list[str],
    following: list[list[str]],
) -> int:
    """Return whether a candidate reads as labels over the rows beneath it.

    Each populated field is compared against what its own column holds below:
    by datatype, by the shape of its characters, and by case. Any one of those
    differing is a break, and a candidate that breaks in most of its columns is
    behaving as a header rather than as another record.

    Returns an int so it can lead the candidate's score tuple directly.
    """
    if not following:
        return 0
    compared = 0
    breaks = 0
    for column, label in enumerate(fields):
        token = label.strip()
        if not token:
            continue
        values = [
            row[column].strip()
            for row in following
            if column < len(row) and row[column].strip()
        ]
        if len(values) < _MINIMUM_CONTRAST_ROWS:
            continue
        compared += 1
        if _breaks_from_values(token, values):
            breaks += 1
    if not compared:
        return 0
    return int(breaks * 2 >= compared)


def _breaks_from_values(label: str, values: list[str]) -> bool:
    """Whether one label differs from the values of its own column."""
    from rey_lib.profiling.file_profiler import detect_datatype

    if detect_datatype(label) != _dominant(detect_datatype(v) for v in values):
        return True
    if _symbol_sequence(label) != _dominant(_symbol_sequence(v) for v in values):
        return True
    return _case_style(label) != _dominant(_case_style(v) for v in values)


def _dominant(observations: Iterator[str]) -> str:
    """Return the most common observation, or "" when the column agrees on none.

    A column that agrees on nothing cannot be broken from, so it yields no
    evidence rather than false contrast.
    """
    from collections import Counter

    counts = Counter(observations)
    if not counts:
        return ""
    value, hits = counts.most_common(1)[0]
    return value if hits > 1 or len(counts) == 1 else ""


def _symbol_sequence(value: str) -> str:
    """Return the value's character-class shape, carrying no source values.

    Consecutive characters of one class collapse to one symbol, so "2026-08-05"
    and "1999-01-02" share a shape while "Post_Date" does not.
    """
    token = value.strip()
    if not token:
        return "E"
    classes = []
    for character in token:
        if character.isalpha():
            symbol = "A"
        elif character.isdigit():
            symbol = "D"
        elif character.isspace():
            symbol = "S"
        else:
            symbol = "P"
        if not classes or symbol != classes[-1]:
            classes.append(symbol)
    return "-".join(classes)


def _case_style(value: str) -> str:
    """Return the value's case style."""
    token = value.strip()
    if not token:
        return "none"
    if not any(character.isalpha() for character in token):
        return "numeric"
    if "_" in token:
        return "snake"
    if token.isupper():
        return "upper"
    if token.islower():
        return "lower"
    if token.istitle():
        return "title"
    return "mixed"


def _is_header_candidate(fields: list[str]) -> bool:
    """Return whether ``fields`` are structurally plausible column names."""
    stripped = [field.strip() for field in fields]
    if len(stripped) < 2:
        return False
    # One unusable name does not make a row stop being the header. A column
    # left unnamed, or named with a stray character that normalizes to
    # nothing, is ordinary in an exported report; rejecting the row for it
    # leaves the file with no header at all, and every consumer then behaves as
    # though the file has no columns. Both are scored instead, so a fully named
    # header still wins wherever one exists.
    #
    # What still gates is the majority: most of the row must read like column
    # names. That is what keeps a padded title or a row of punctuation out,
    # without depending on any single field.
    lexical = sum(_looks_like_header_name(field) for field in stripped)
    return lexical * 2 >= len(stripped)


def _looks_like_header_name(value: str) -> bool:
    """Return whether a value resembles a textual column identifier."""
    token = value.strip()
    if not token or not re.search(r"[A-Za-z]", token):
        return False
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)", token):
        return False
    if re.fullmatch(r"\d{1,4}[-/]\d{1,2}[-/]\d{1,4}", token):
        return False
    return True
