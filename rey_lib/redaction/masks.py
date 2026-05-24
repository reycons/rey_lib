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
    """Replace integer digits with deterministic random digits."""
    return _randomize_digits(value, counter)


def _mask_decimal(value: str, counter: int) -> str:
    """Replace decimal digits while preserving decimal scale and separators."""
    return _randomize_digits(value, counter)


def _randomize_digits(value: str, counter: int) -> str:
    """Return ``value`` with every digit replaced by deterministic randomness."""
    rng = random.Random(f"{counter}:{value.count('.')}")
    result: list[str] = []
    first_digit = True

    for ch in value:
        if not ch.isdigit():
            result.append(ch)
            continue

        if first_digit:
            result.append(str(rng.randint(1, 9)))
            first_digit = False
        else:
            result.append(str(rng.randint(0, 9)))

    return "".join(result)


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
