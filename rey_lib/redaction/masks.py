"""
Type-aware masking functions for file_redactor column redaction.

Each mask function receives the original value and a per-column counter and
returns a fixed or counter-derived replacement appropriate for that data type.
The counter is deterministic within a column namespace so the same original
value always produces the same replacement.

Public API
----------
KNOWN_MASKS     Set of supported mask type strings.
apply_mask      Dispatch to the appropriate mask function by type name.
"""

from __future__ import annotations

import random

__all__: list[str] = ["KNOWN_MASKS", "apply_mask"]

# Words used for text masking — short, neutral, lorem-ipsum style.
_TEXT_WORDS: list[str] = [
    "lorem", "ipsum", "dolor", "sit", "amet", "consectetur",
    "adipiscing", "elit", "sed", "eiusmod", "tempor", "incididunt",
]

KNOWN_MASKS: frozenset[str] = frozenset({
    "ssn", "phone", "email", "name", "text", "date", "zip", "account",
    "integer", "decimal",
})


def apply_mask(mask_type: str, value: str, counter: int) -> str:
    """Return a type-aware replacement for ``value``.

    Dispatches to the handler registered for ``mask_type``.  Unknown types
    are passed through unchanged.

    Parameters
    ----------
    mask_type : str
        One of the strings in ``KNOWN_MASKS``.
    value : str
        Original field value.
    counter : int
        Per-column unique-value counter — used to produce distinct
        replacements when the mask type varies by record.

    Returns
    -------
    str
        Masked replacement, or ``value`` unchanged if ``mask_type`` is
        not recognised.
    """
    handler = _HANDLERS.get(mask_type)
    if handler is None:
        return value
    return handler(value, counter)


# ---------------------------------------------------------------------------
# Private — mask handlers
# ---------------------------------------------------------------------------

def _mask_ssn(value: str, counter: int) -> str:
    """Replace with a fixed invalid SSN."""
    return "111-11-1111"


def _mask_phone(value: str, counter: int) -> str:
    """Replace with zeroed-out digits preserving common separators."""
    result: list[str] = []
    for ch in value:
        result.append("0" if ch.isdigit() else ch)
    return "".join(result)


def _mask_email(value: str, counter: int) -> str:
    """Replace with a sequentially numbered example-domain address."""
    return f"user{counter}@example.com"


def _mask_name(value: str, counter: int) -> str:
    """Replace with a sequentially numbered placeholder name."""
    return _fit_width(f"Name{counter}", value)


def _mask_text(value: str, counter: int) -> str:
    """Replace with deterministic random placeholder text.

    Word count matches the original so spacing patterns are preserved.
    The counter seeds the RNG so the same original always yields the same
    replacement within a column.
    """
    rng = random.Random(counter)
    word_count = max(1, len(value.split()))
    return " ".join(rng.choices(_TEXT_WORDS, k=word_count))


def _mask_date(value: str, counter: int) -> str:
    """Replace with a fixed obviously-invalid sentinel date."""
    return "1900-01-01"


def _mask_zip(value: str, counter: int) -> str:
    """Replace with all-zero ZIP, preserving 5- or 9-digit length."""
    digits_only = value.replace("-", "").replace(" ", "")
    if len(digits_only) == 9:
        return "00000-0000"
    return "00000"


def _mask_account(value: str, counter: int) -> str:
    """Replace digits with zeros, preserving length and non-digit characters."""
    result: list[str] = []
    for ch in value:
        result.append("0" if ch.isdigit() else ch)
    return "".join(result)


def _mask_integer(value: str, counter: int) -> str:
    """Replace integer digits with a safe same-scale numeric sentinel."""
    return _sentinel_integer(value)


def _mask_decimal(value: str, counter: int) -> str:
    """Replace decimal digits while preserving integer width and decimal scale."""
    return _sentinel_decimal(value)


def _sentinel_integer(value: str) -> str:
    """Return an integer like 12567 -> 10000, preserving sign and separators."""
    return _format_sentinel_number(value, fractional_digits=0)


def _sentinel_decimal(value: str) -> str:
    """Return a decimal like 12567.09999 -> 10000.00001."""
    scale = _fractional_width(value)
    return _format_sentinel_number(value, fractional_digits=scale)


def _fractional_width(value: str) -> int:
    """Return the count of digits after the decimal point."""
    if "." not in value:
        return 0
    return sum(1 for ch in value.rsplit(".", 1)[1] if ch.isdigit())


def _format_sentinel_number(value: str, fractional_digits: int) -> str:
    """Build a same-shape numeric sentinel from a source value."""
    stripped = value.strip()
    sign = "-" if stripped.startswith("-") else "+" if stripped.startswith("+") else ""
    unsigned = stripped[1:] if sign else stripped
    whole = unsigned.split(".", 1)[0]
    integer_digit_count = sum(1 for ch in whole if ch.isdigit())
    integer_digits = _sentinel_digits(integer_digit_count)
    integer_text = _apply_integer_grouping(whole, integer_digits)

    if fractional_digits <= 0:
        return f"{sign}{integer_text}"

    fractional_text = "0" * (fractional_digits - 1) + "1"
    return f"{sign}{integer_text}.{fractional_text}"


def _sentinel_digits(width: int) -> str:
    """Return same-width sentinel digits with a non-zero leading digit."""
    if width <= 0:
        return "0"
    return "1" + ("0" * (width - 1))


def _apply_integer_grouping(template: str, digits: str) -> str:
    """Place sentinel digits into the original integer punctuation shape."""
    result: list[str] = []
    digit_iter = iter(digits)
    for ch in template:
        if ch.isdigit():
            result.append(next(digit_iter))
        else:
            result.append(ch)

    if any(ch.isdigit() for ch in template):
        return "".join(result)

    return digits


def _fit_width(replacement: str, value: str) -> str:
    """Pad or trim a replacement to match the original field width."""
    width = len(value)
    if len(replacement) > width:
        return replacement[:width]
    return replacement.ljust(width)


_HANDLERS: dict[str, object] = {
    "ssn":     _mask_ssn,
    "phone":   _mask_phone,
    "email":   _mask_email,
    "name":    _mask_name,
    "text":    _mask_text,
    "date":    _mask_date,
    "zip":     _mask_zip,
    "account": _mask_account,
    "integer": _mask_integer,
    "decimal": _mask_decimal,
}
