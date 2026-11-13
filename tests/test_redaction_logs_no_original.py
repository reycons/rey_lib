"""The redaction registry never writes an original value to a log record (#1356).

A replacement whose length differs from its source is intended output for the
email, text, ssn, date and zip masks. It used to log a warning carrying the
original value, once per distinct value, into the run log. Whatever path a
value takes through the registry, no log record may carry it.
"""

from __future__ import annotations

import logging

import pytest

from rey_lib.redaction.registry import RedactionRegistry

#: Source values that are recognisable in any log text they reach.
ORIGINALS = [
    "jane.original@private-domain.org",
    "SECRET ORIGINAL TEXT VALUE",
    "123-45-6789",
    "2024-02-29",
    "90210-1234",
]


def _record_text(record: logging.LogRecord) -> str:
    """Return everything a log record can carry into the run log.

    Args:
        record: A captured log record.

    Returns:
        The formatted message, the raw arguments and any exception text.
    """
    exc_text = logging.Formatter().formatException(record.exc_info) if record.exc_info else ""
    return f"{record.getMessage()} {record.args!r} {exc_text}"


@pytest.mark.parametrize("mask_type", ["email", "text", "ssn", "date", "zip", None])
def test_no_original_value_reaches_a_log_record(
    mask_type: str | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every original value is redacted and none appears in any log record.

    Args:
        mask_type: The column's mask; ``None`` takes the generic path.
        caplog: Captures every record emitted while redacting.
    """
    mask_types = {"COL": mask_type} if mask_type else None
    registry = RedactionRegistry(["COL"], mask_types=mask_types)

    with caplog.at_level(logging.DEBUG):
        replacements = [registry.redact("COL", value) for value in ORIGINALS]

    assert all(r != v for r, v in zip(replacements, ORIGINALS))
    logged = " ".join(_record_text(record) for record in caplog.records)
    for value in ORIGINALS:
        assert value not in logged


def test_the_length_changing_path_is_exercised() -> None:
    """The email mask changes length, so the removed warning's trigger is covered."""
    registry = RedactionRegistry(["COL"], mask_types={"COL": "email"})

    assert len(registry.redact("COL", ORIGINALS[0])) != len(ORIGINALS[0])
