"""Tests for numeric redaction behavior."""

from rey_lib.profiling.file_profiler import infer_sql_type
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


def test_infer_sql_type_integer_columns_return_integer() -> None:
    """Integer column values produce INTEGER, not DECIMAL or VARCHAR."""
    assert infer_sql_type(["100", "200", "300"]) == "INTEGER"


def test_infer_sql_type_decimal_columns_use_observed_precision() -> None:
    """Decimal column values produce DECIMAL(p, s) with observed scale, not generic DECIMAL(20, 8)."""
    result = infer_sql_type(["1200.50", "-42.123", "1000.00"])
    assert result is not None
    assert result.startswith("DECIMAL(")
    _, scale_part = result.rstrip(")").split(", ")
    assert int(scale_part) == 3  # max observed decimal places


def test_infer_sql_type_date_columns_return_date() -> None:
    """Date column values produce DATE."""
    assert infer_sql_type(["2024-01-01", "2024-06-15", "2023-12-31"]) == "DATE"


def test_infer_sql_type_text_columns_return_none() -> None:
    """Text columns return None so callers fall back to VARCHAR."""
    assert infer_sql_type(["Alice", "Bob", "Charlie"]) is None


def test_infer_sql_type_empty_values_return_none() -> None:
    """Empty or blank-only values return None."""
    assert infer_sql_type([]) is None
    assert infer_sql_type(["", "  "]) is None
