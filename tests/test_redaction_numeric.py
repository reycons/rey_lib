"""Tests for numeric redaction behavior."""

from rey_lib.redaction.detector import detect_mask_type
from rey_lib.redaction.masks import apply_mask
from rey_lib.redaction.registry import RedactionRegistry


def test_detector_recognizes_decimal_values() -> None:
    """Decimal columns should not fall back to text redaction."""
    assert detect_mask_type(["1200.50", "-42.123", "1,005.00"]) == "decimal"


def test_detector_recognizes_integer_values() -> None:
    """Integer columns should receive numeric redaction by default."""
    assert detect_mask_type(["1200", "-42", "1,005"]) == "integer"


def test_decimal_mask_preserves_scale_and_separators() -> None:
    """Decimal redaction preserves format but changes the digits."""
    masked = apply_mask("decimal", "-1,234.500", 7)

    assert masked != "-1,234.500"
    assert masked[0] == "-"
    assert masked[2] == ","
    assert masked[-4] == "."
    assert len(masked.rsplit(".", 1)[1]) == 3


def test_integer_mask_preserves_width_and_sign() -> None:
    """Integer redaction keeps numeric shape without zero-filling values."""
    masked = apply_mask("integer", "-001234", 3)

    assert masked != "-001234"
    assert masked.startswith("-")
    assert len(masked) == len("-001234")
    assert masked[1:].isdigit()


def test_registry_keeps_numeric_replacements_stable() -> None:
    """The same original value maps to the same random-looking number."""
    registry = RedactionRegistry(["AMOUNT"], mask_types={"AMOUNT": "decimal"})

    first = registry.redact("AMOUNT", "99.00")
    second = registry.redact("AMOUNT", "99.00")

    assert first == second
    assert first != "99.00"
    assert len(first.rsplit(".", 1)[1]) == 2


def test_name_mask_preserves_field_width() -> None:
    """Delimited fields padded for fixed-width consumers keep their width."""
    assert apply_mask("name", "SMITH  ", 1).endswith("  ")
    assert len(apply_mask("name", "SMITH  ", 1)) == len("SMITH  ")
