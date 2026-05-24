"""Tests for numeric metadata in file profiles."""

from rey_lib.profiling.file_profiler import profile_rows


def test_profile_records_decimal_precision_metadata() -> None:
    """Decimal columns expose the largest scale present in source values."""
    profile = profile_rows(
        rows=[
            {"AMOUNT": "100.0", "COUNT": "12"},
            {"AMOUNT": "200.125", "COUNT": "300"},
        ],
        source_name="sample.csv",
        layout="delimited",
    )

    cols = {col["name"]: col for col in profile["columns"]}

    assert cols["AMOUNT"]["type"] == "decimal"
    assert cols["AMOUNT"]["min_decimal_places"] == 1
    assert cols["AMOUNT"]["max_decimal_places"] == 3
    assert cols["AMOUNT"]["max_integer_digits"] == 3
    assert cols["COUNT"]["type"] == "integer"
    assert cols["COUNT"]["max_decimal_places"] == 0
    assert cols["COUNT"]["max_integer_digits"] == 3
