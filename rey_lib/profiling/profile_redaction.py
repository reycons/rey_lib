"""Independent, structure-preserving redaction for canonical profile samples."""

from __future__ import annotations

import calendar
import re
import secrets
import string
from copy import deepcopy
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

__all__ = ["redact_profile"]

_VALUE_FIELDS = (
    "sample_values",
    "null_like_values",
    "constant_value",
    "min_numeric",
    "max_numeric",
    "min_date",
    "max_date",
)
_LIST_FIELDS = frozenset({"null_like_values"})
_DATE_FIELDS = frozenset({"min_date", "max_date"})
_DIGITS = "001123456789"
_NONZERO_DIGITS = "1123456789"
_DATE_RANGE_DAYS = (date(2099, 12, 31) - date(1900, 1, 1)).days + 1


def redact_profile(samples: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return independently randomized samples with the identical field shape.

    Only the explicitly governed value-bearing sample fields are transformed.
    Column names and punctuation are retained. No seed, substitution map, or
    relationship between equal clear values is created or persisted.
    """
    redacted: list[dict[str, Any]] = []
    for supplied in samples:
        item = deepcopy(dict(supplied))
        for field in _VALUE_FIELDS:
            if field not in item:
                continue
            value = item[field]
            if field == "sample_values" and isinstance(value, list):
                item[field] = [
                    {
                        "value": _redact_value(entry["value"], force_date=False),
                        "count": entry["count"],
                    }
                    for entry in value
                ]
            elif field in _LIST_FIELDS and isinstance(value, list):
                item[field] = [
                    _redact_value(entry, force_date=False) for entry in value
                ]
            else:
                item[field] = _redact_value(
                    value,
                    force_date=field in _DATE_FIELDS,
                )
        redacted.append(item)
    return redacted


def _redact_value(value: Any, *, force_date: bool) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return _random_integer(value)
    if isinstance(value, float):
        rendered = str(value)
        for _ in range(16):
            candidate = float(_shape_numeric_text(rendered))
            if candidate != value:
                return candidate
        return float(_force_different_digit(rendered))

    text = str(value)
    replacement = _random_date(text) if force_date or _date_shape(text) else None
    if replacement is None:
        replacement = _shape_characters(text)
    if replacement == text and text:
        replacement = _retry_different(text, force_date=force_date)
    return replacement


def _retry_different(text: str, *, force_date: bool) -> str:
    for _ in range(8):
        candidate = _random_date(text) if force_date or _date_shape(text) else None
        candidate = candidate if candidate is not None else _shape_characters(text)
        if candidate != text:
            return candidate
    if force_date or _date_shape(text):
        candidate = _render_date(text, date(2000, 1, 1))
        if candidate == text:
            candidate = _render_date(text, date(2000, 1, 2))
        return candidate
    return _force_different_text(text)


def _random_integer(value: int) -> int:
    rendered = str(value)
    negative = rendered.startswith("-")
    width = len(rendered.lstrip("+-"))
    for _ in range(16):
        if width <= 1:
            digits = _random_digit()
        else:
            digits = secrets.choice(_NONZERO_DIGITS) + "".join(
                _random_digit() for _ in range(width - 1)
            )
        randomized = int(digits)
        candidate = -randomized if negative else randomized
        if candidate != value:
            return candidate
    fallback = int("1" * width)
    if fallback == abs(value):
        fallback = int("2" * width)
    return -fallback if negative else fallback


def _shape_characters(text: str) -> str:
    output: list[str] = []
    for character in text:
        if character.isupper():
            output.append(secrets.choice(string.ascii_uppercase))
        elif character.islower():
            output.append(secrets.choice(string.ascii_lowercase))
        elif character.isdigit():
            output.append(_random_digit())
        else:
            output.append(character)
    return "".join(output)


def _shape_numeric_text(text: str) -> str:
    return "".join(
        _random_digit() if character.isdigit() else character for character in text
    )


def _force_different_text(text: str) -> str:
    output = list(text)
    for index, character in enumerate(output):
        if character.isupper():
            output[index] = "A" if character != "A" else "B"
            break
        if character.islower():
            output[index] = "a" if character != "a" else "b"
            break
        if character.isdigit():
            output[index] = "0" if character != "0" else "1"
            break
    return "".join(output)


def _force_different_digit(text: str) -> str:
    output = list(text)
    for index, character in enumerate(output):
        if character.isdigit():
            output[index] = "0" if character != "0" else "1"
            break
    return "".join(output)


def _random_digit() -> str:
    return secrets.choice(_DIGITS)


def _date_shape(text: str) -> str | None:
    stripped = text.strip()
    for pattern in (
        r"\d{4}-\d{2}-\d{2}",
        r"\d{2}/\d{2}/\d{4}",
        r"\d{2}-\d{2}-\d{4}",
        r"\d{2}/\d{2}/\d{2}",
        r"\d{8}",
        r"\d{1,2}-[A-Za-z]{3}-\d{4}",
        r"\d{1,2} [A-Za-z]{3} \d{4}",
    ):
        if re.fullmatch(pattern, stripped):
            return pattern
    return None


def _random_date(text: str) -> str | None:
    shape = _date_shape(text)
    if shape is None:
        return None
    generated = date(1900, 1, 1) + timedelta(days=secrets.randbelow(_DATE_RANGE_DAYS))
    return _render_date(text, generated)


def _render_date(text: str, generated: date) -> str:
    shape = _date_shape(text)
    if shape is None:
        return text
    stripped = text.strip()
    if shape == r"\d{4}-\d{2}-\d{2}":
        rendered = generated.strftime("%Y-%m-%d")
    elif shape == r"\d{2}/\d{2}/\d{4}":
        rendered = generated.strftime("%m/%d/%Y")
    elif shape == r"\d{2}-\d{2}-\d{4}":
        rendered = generated.strftime("%m-%d-%Y")
    elif shape == r"\d{2}/\d{2}/\d{2}":
        rendered = generated.strftime("%m/%d/%y")
    elif shape == r"\d{8}":
        rendered = generated.strftime("%Y%m%d")
    else:
        separator = "-" if "-" in stripped else " "
        day_width = len(stripped.split(separator, 1)[0])
        day = str(generated.day) if day_width == 1 else f"{generated.day:02d}"
        source_month = stripped.split(separator)[1]
        month = calendar.month_abbr[generated.month]
        if source_month.isupper():
            month = month.upper()
        elif source_month.islower():
            month = month.lower()
        rendered = separator.join((day, month, str(generated.year)))
    prefix = text[: len(text) - len(text.lstrip())]
    suffix = text[len(text.rstrip()):]
    return prefix + rendered + suffix
