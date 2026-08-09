"""Focused tests for profile-only representative-value redaction."""

from __future__ import annotations

import json

from rey_lib.profiling import redact_profile


def _profile() -> dict:
    return {
        "source": "customers.csv",
        "profile_version": "csv_v1",
        "llm_hints": {"recommended_source_name": "customers"},
        "redacted_columns": [],
        "columns": [
            {
                "name": "customer_name",
                "raw_name": "customer_name",
                "ordinal": 1,
                "type": "text",
                "distinct_sample": ["ACME", "BETA"],
                "sample_values": ["ACME", "BETA"],
                "constant_value": "ACME",
                "null_like_values": ["UNKNOWN"],
                "min_length": 4,
            },
            {
                "name": "vendor_name",
                "raw_name": "vendor_name",
                "ordinal": 2,
                "type": "text",
                "distinct_sample": ["ACME"],
                "sample_values": ["ACME"],
                "min_numeric": 1.0,
                "max_numeric": 9.0,
                "min_date": "2026-01-01",
                "max_date": "2026-12-31",
                "max_length": 4,
            },
        ],
    }


def test_profile_redaction_is_column_scoped_and_consistent() -> None:
    original = _profile()
    redacted = redact_profile(original)

    first = redacted["columns"][0]
    second = redacted["columns"][1]
    assert first["distinct_sample"][0] == first["sample_values"][0]
    assert first["distinct_sample"][0] == first["constant_value"]
    assert first["distinct_sample"][0] != second["distinct_sample"][0]
    assert "ACME" not in json.dumps(redacted)
    assert "BETA" not in json.dumps(redacted)
    assert "UNKNOWN" not in json.dumps(redacted)
    assert original == _profile()


def test_only_enumerated_column_value_fields_are_transformed() -> None:
    redacted = redact_profile(_profile())

    assert redacted["source"] == "customers.csv"
    assert redacted["llm_hints"] == {"recommended_source_name": "customers"}
    assert redacted["columns"][0]["name"] == "customer_name"
    assert redacted["columns"][0]["type"] == "text"
    assert redacted["columns"][0]["min_length"] == 4
    assert redacted["columns"][1]["max_length"] == 4


def test_exact_numeric_and_date_ranges_are_removed_but_shape_facts_remain() -> None:
    redacted = redact_profile(_profile())
    column = redacted["columns"][1]

    assert "min_numeric" not in column
    assert "max_numeric" not in column
    assert "min_date" not in column
    assert "max_date" not in column
    assert column["type"] == "text"
    assert column["max_length"] == 4
