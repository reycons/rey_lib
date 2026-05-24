"""Tests for numeric metadata in file profiles."""

from rey_lib.profiling.file_profiler import profile_rows


def test_profile_records_decimal_precision_metadata() -> None:
    """Numeric columns expose precision, sign, and digit distribution metadata."""
    profile = profile_rows(
        rows=[
            {"AMOUNT": "100.0", "COUNT": "12"},
            {"AMOUNT": "200.125", "COUNT": "300"},
            {"AMOUNT": "-010.50", "COUNT": "004"},
        ],
        source_name="sample.csv",
        layout="delimited",
    )

    cols = {col["name"]: col for col in profile["columns"]}

    assert cols["AMOUNT"]["type"] == "decimal"
    assert cols["AMOUNT"]["min_decimal_places"] == 1
    assert cols["AMOUNT"]["max_decimal_places"] == 3
    assert cols["AMOUNT"]["min_integer_digits"] == 3
    assert cols["AMOUNT"]["max_integer_digits"] == 3
    assert cols["AMOUNT"]["common_integer_digits"] == 3
    assert cols["AMOUNT"]["integer_digit_counts"] == {"3": 3}
    assert cols["AMOUNT"]["has_leading_zero"] is True
    assert cols["AMOUNT"]["has_negative"] is True
    assert cols["COUNT"]["type"] == "integer"
    assert cols["COUNT"]["max_decimal_places"] == 0
    assert cols["COUNT"]["max_integer_digits"] == 3
    assert cols["COUNT"]["common_integer_digits"] == 3
    assert cols["COUNT"]["integer_digit_counts"] == {"2": 1, "3": 2}
    assert cols["COUNT"]["has_leading_zero"] is True
    assert cols["COUNT"]["has_negative"] is False
