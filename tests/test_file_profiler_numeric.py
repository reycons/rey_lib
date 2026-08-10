"""Tests for numeric metadata in file profiles."""

from rey_lib.profiling.csv_profile import enrich_csv_profile
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


def test_representative_samples_rank_frequency_then_first_seen() -> None:
    rows = [
        {"TYPE": value}
        for value in ["BETA", "ALPHA", "BETA", "ALPHA", "GAMMA", "BETA"]
    ]

    base = profile_rows(rows=rows, source_name="sample.csv", layout="delimited")
    assert "distinct_sample" not in base["columns"][0]

    enriched = enrich_csv_profile(
        base,
        rows,
        rows,
        source_file="sample.csv",
        encoding="utf-8",
        delimiter=",",
        max_sample_values=3,
    )
    assert enriched["columns"][0]["sample_values"] == [
        {"value": "BETA", "count": 3},
        {"value": "ALPHA", "count": 2},
        {"value": "GAMMA", "count": 1},
    ]


def test_equal_frequency_samples_keep_existing_first_seen_order() -> None:
    rows = [
        {"TYPE": value}
        for value in ["SECOND", "FIRST", "FIRST", "SECOND", "THIRD"]
    ]

    base = profile_rows(rows=rows, source_name="sample.csv", layout="delimited")
    profile = enrich_csv_profile(
        base,
        rows,
        rows,
        source_file="sample.csv",
        encoding="utf-8",
        delimiter=",",
    )

    assert profile["columns"][0]["sample_values"] == [
        {"value": "SECOND", "count": 2},
        {"value": "FIRST", "count": 2},
        {"value": "THIRD", "count": 1},
    ]


def test_source_line_number_is_never_profiled() -> None:
    rows = [
        {"source_line_number": "2", "Amount": "10.00"},
        {"source_line_number": "3", "Amount": "20.00"},
    ]

    base = profile_rows(rows=rows, source_name="sample.csv", layout="delimited")
    enriched = enrich_csv_profile(
        base,
        rows,
        rows,
        source_file="sample.csv",
        encoding="utf-8",
        delimiter=",",
    )

    assert base["column_count"] == 1
    assert [column["name"] for column in enriched["columns"]] == ["Amount"]
    assert "source_line_number" not in str(enriched)
