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
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from rey_lib.errors.error_utils import AppError
from rey_lib.files.file_utils import read_text_file

__all__ = [
    "CsvRead",
    "CsvReadError",
    "CsvRow",
    "HeaderMatch",
    "parse_delimited_line",
    "read_csv",
    "sample_indices",
]

# Header discovery is bounded to the opening rows. Every line is already in
# memory, so this bounds the comparison work rather than the read.
_HEADER_SEARCH_ROWS = 50

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


def read_csv(
    path: Path | str,
    *,
    encoding: str = "utf-8",
    delimiter: str | None = None,
    required_header: Sequence[str] = (),
    sample_size: int = 0,
    skip_blank_lines: bool = False,
) -> CsvRead:
    """Read one delimited file and answer every structural question about it.

    Parameters
    ----------
    path : Path | str
        Source file, already resolved and authorized by the caller.
    encoding : str
        Character encoding, for example ``'utf-8-sig'``.
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
        source_text = read_text_file(source_path, encoding=encoding)
    except OSError as exc:
        raise CsvReadError(f"Cannot read '{source_path}': {exc}") from exc

    lines = source_text.splitlines()
    source_text_sha256 = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    required = tuple(str(text) for text in required_header if str(text).strip())

    if not lines:
        return CsvRead(
            path=str(source_path),
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
        path=str(source_path),
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

    candidates: list[tuple[tuple[int, int, int], int]] = []
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
        candidates.append(((len(matching_width), consistency, lexical), index))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    best_score, best_index = candidates[0]
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
    """Return the indices a representative sample selects, in file order.

    Deterministic rather than random: the opening rows, the middle of the
    file, and the closing rows, so a consumer sees every part of the source.
    Exported so a caller can apply one selection across parallel sequences and
    keep them aligned.
    """
    if total <= size or size <= 0:
        return list(range(total))

    third = max(1, size // 3)
    extra = size - (third * 3)  # the remainder goes to the opening segment
    first_n = third + extra
    last_n = third
    mid_n = third
    mid_start = max(first_n, (total - mid_n) // 2)
    mid_end = min(total - last_n, mid_start + mid_n)

    seen: set[int] = set()
    selected: list[int] = []
    for start, end in ((0, first_n), (mid_start, mid_end), (total - last_n, total)):
        for index in range(start, end):
            if index not in seen:
                seen.add(index)
                selected.append(index)
    return selected


def parse_delimited_line(line: str, delimiter: str) -> list[str]:
    """Parse one in-memory source line with the given delimiter."""
    return list(next(_csv.reader([line], delimiter=delimiter), []))


def _is_header_candidate(fields: list[str]) -> bool:
    """Return whether ``fields`` are structurally plausible column names."""
    stripped = [field.strip() for field in fields]
    if len(stripped) < 2 or any(not field for field in stripped):
        return False
    normalized = [
        re.sub(r"[^a-z0-9]+", "_", field.lower()).strip("_") for field in stripped
    ]
    if any(not field for field in normalized) or len(set(normalized)) != len(normalized):
        return False
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
