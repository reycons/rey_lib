"""Tests for numeric redaction behavior."""

from rey_lib.data.logical_type import LogicalType
from rey_lib.profiling.file_profiler import infer_logical_type
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
    """Decimal redaction keeps scale with a safe sentinel value."""
    masked = apply_mask("decimal", "-1,234.500", 7)

    assert masked == "-1,000.001"


def test_decimal_mask_preserves_largest_number_shape() -> None:
    """A wide decimal is redacted as same-width integer part and same scale."""
    assert apply_mask("decimal", "12567.09999", 1) == "10000.00001"


def test_integer_mask_preserves_width_and_sign() -> None:
    """Integer redaction keeps width and sign using a safe sentinel."""
    masked = apply_mask("integer", "-001234", 3)

    assert masked == "-100000"


def test_registry_keeps_numeric_replacements_stable() -> None:
    """The same original value maps to the same sentinel number."""
    registry = RedactionRegistry(["AMOUNT"], mask_types={"AMOUNT": "decimal"})

    first = registry.redact("AMOUNT", "99.00")
    second = registry.redact("AMOUNT", "99.00")

    assert first == second
    assert first == "10.01"
    assert len(first.rsplit(".", 1)[1]) == 2


def test_name_mask_preserves_field_width() -> None:
    """Delimited fields padded for fixed-width consumers keep their width."""
    assert apply_mask("name", "SMITH  ", 1).endswith("  ")
    assert len(apply_mask("name", "SMITH  ", 1)) == len("SMITH  ")


def test_infer_logical_type_integer_columns_return_integer() -> None:
    """Integer column values produce integer, not decimal or text."""
    assert infer_logical_type(["100", "200", "300"]) == LogicalType("integer")


def test_infer_logical_type_decimal_columns_use_observed_precision() -> None:
    """Decimal values produce decimal(p, s) with the observed scale, not a generic one."""
    result = infer_logical_type(["1200.50", "-42.123", "1000.00"])
    assert result is not None
    assert result.kind == "decimal"
    assert result.scale == 3  # max observed decimal places
    assert result.precision == 7  # four integer digits plus three


def test_infer_logical_type_date_columns_return_date() -> None:
    """Date column values produce date."""
    assert infer_logical_type(["2024-01-01", "2024-06-15", "2023-12-31"]) == LogicalType("date")


def test_infer_logical_type_text_columns_return_none() -> None:
    """Text columns return None so callers fall back to text."""
    assert infer_logical_type(["Alice", "Bob", "Charlie"]) is None


def test_infer_logical_type_empty_values_return_none() -> None:
    """Empty or blank-only values return None."""
    assert infer_logical_type([]) is None
    assert infer_logical_type(["", "  "]) is None
