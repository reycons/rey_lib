"""
Character pattern analysis and characteristic-preserving replacement generation.

A replacement value must match the original value's:
  - width     — same character length
  - character type — digit stays digit, upper stays upper, lower stays lower
  - separators     — non-alphanumeric characters preserved in position

Design
------
Each value is analysed into a per-character class sequence (the "pattern").
A counter integer is then encoded into that pattern so the replacement has
the same structural fingerprint as the original.

Digit positions   → zero-padded decimal counter, right-to-left fill.
Uppercase alpha   → counter encoded as uppercase letters (A=0, B=1 … per digit).
Lowercase alpha   → same encoding, lowercase.
Other (separator) → character preserved verbatim.

Example
-------
Original : ``123-45-6789``   (9 digit positions, 2 separators)
Counter  : 1
Pattern  : D D D S D D S D D D D
Digit fill (9 slots, counter=1): ``000000001``
Result   : ``000-00-0001``

Original : ``SMITH``   (5 uppercase alpha positions)
Counter  : 1
Alpha fill: digit 1 → letter 'B', padded with 'A' → ``BAAAA``
Result   : ``BAAAA``

Original : ``ABC123``  (3 upper + 3 digit)
Counter  : 1
Upper fill (3 slots): ``BAA``
Digit fill (3 slots): ``001``
Result   : ``BAA001``

Public API
----------
analyze_pattern        Return per-character class list for a value string.
generate_replacement   Generate a replacement string from a pattern + counter.
"""

from __future__ import annotations

__all__ = ["analyze_pattern", "generate_replacement"]

# ---------------------------------------------------------------------------
# Character class tags
# ---------------------------------------------------------------------------
_DIGIT: str = "D"
_UPPER: str = "U"
_LOWER: str = "L"
_SEP:   str = "S"   # separator — preserved verbatim


def analyze_pattern(value: str) -> list[tuple[str, str]]:
    """Return a per-character (class, char) list for ``value``.

    Parameters
    ----------
    value : str
        Original field value.

    Returns
    -------
    list[tuple[str, str]]
        Each element is ``(class_tag, original_char)`` where class_tag is
        one of ``'D'`` (digit), ``'U'`` (uppercase alpha), ``'L'`` (lowercase
        alpha), or ``'S'`` (separator — preserved verbatim).
    """
    result: list[tuple[str, str]] = []
    for ch in value:
        if ch.isdigit():
            result.append((_DIGIT, ch))
        elif ch.isupper():
            result.append((_UPPER, ch))
        elif ch.islower():
            result.append((_LOWER, ch))
        else:
            result.append((_SEP, ch))
    return result


def generate_replacement(pattern: list[tuple[str, str]], counter: int) -> str:
    """Generate a replacement string that preserves the structural pattern.

    Digit positions are filled with a zero-padded decimal representation of
    ``counter``, right-to-left.  Alpha positions (upper or lower) are filled
    using a letter encoding of ``counter`` where each decimal digit maps to a
    letter (0→A … 9→J), right-to-left, padded with 'A'/'a'.  Separator
    positions are reproduced verbatim.

    The result is always the same length as the original value.

    Parameters
    ----------
    pattern : list[tuple[str, str]]
        Output of :func:`analyze_pattern`.
    counter : int
        Positive integer identifying this unique value within its column.

    Returns
    -------
    str
        Replacement string with the same width and character structure.
    """
    digit_slots  = [i for i, (cls, _) in enumerate(pattern) if cls == _DIGIT]
    upper_slots  = [i for i, (cls, _) in enumerate(pattern) if cls == _UPPER]
    lower_slots  = [i for i, (cls, _) in enumerate(pattern) if cls == _LOWER]

    result = list(ch for _, ch in pattern)   # start with separators in place

    if digit_slots:
        digit_fill = _encode_digits(counter, len(digit_slots))
        for pos, ch in zip(digit_slots, digit_fill):
            result[pos] = ch

    if upper_slots:
        upper_fill = _encode_alpha(counter, len(upper_slots), upper=True)
        for pos, ch in zip(upper_slots, upper_fill):
            result[pos] = ch

    if lower_slots:
        lower_fill = _encode_alpha(counter, len(lower_slots), upper=False)
        for pos, ch in zip(lower_slots, lower_fill):
            result[pos] = ch

    return "".join(result)


# ---------------------------------------------------------------------------
# Private — encoding helpers
# ---------------------------------------------------------------------------

def _encode_digits(counter: int, width: int) -> str:
    """Return a zero-padded decimal string of ``counter`` truncated to ``width``."""
    encoded = str(counter)
    if len(encoded) > width:
        encoded = encoded[-width:]          # keep least-significant digits
    return encoded.zfill(width)


def _encode_alpha(counter: int, width: int, *, upper: bool) -> str:
    """Return a letter-encoded string of ``counter`` padded to ``width``.

    Each decimal digit of ``counter`` is mapped to a letter (0→A, 1→B … 9→J).
    The encoded string is right-aligned and left-padded with 'A' / 'a'.
    """
    pad   = "A" if upper else "a"
    digit_map = {str(d): chr(ord(pad) + d) for d in range(10)}
    encoded   = "".join(digit_map[d] for d in str(counter))
    if len(encoded) > width:
        encoded = encoded[-width:]
    return encoded.rjust(width, pad)
