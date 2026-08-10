"""Focused tests for canonical profile-sample redaction."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from rey_lib.profiling import redact_profile
from rey_lib.profiling import profile_redaction


def _samples() -> list[dict]:
    return [
        {
            "column": "Customer Name",
            "sample_values": [
                {"value": "ACME", "count": 7},
                {"value": "BETA", "count": 3},
            ],
            "constant_value": "ACME",
            "null_like_values": ["UNKNOWN"],
        },
        {
            "column": "Trade Date",
            "sample_values": [{"value": "2026-03-04", "count": 4}],
            "min_numeric": 1.0,
            "max_numeric": 9.0,
            "min_date": "2026-01-01",
            "max_date": "2026-12-31",
        },
    ]


def test_profile_redaction_preserves_shape_and_does_not_mutate_input(
    monkeypatch,
) -> None:
    original = _samples()
    before = deepcopy(original)
    monkeypatch.setattr(profile_redaction.secrets, "choice", lambda values: values[-1])
    monkeypatch.setattr(profile_redaction.secrets, "randbelow", lambda _limit: 0)

    redacted = redact_profile(original)

    assert original == before
    assert [set(item) for item in redacted] == [set(item) for item in original]
    assert [item["column"] for item in redacted] == [
        "Customer Name",
        "Trade Date",
    ]
    assert redacted[0]["sample_values"] == [
        {"value": "ZZZZ", "count": 7},
        {"value": "ZZZZ", "count": 3},
    ]
    assert redacted[0]["constant_value"] == "ZZZZ"
    assert redacted[0]["null_like_values"] == ["ZZZZZZZ"]


def test_numeric_and_date_values_are_randomized_in_their_existing_fields(
    monkeypatch,
) -> None:
    monkeypatch.setattr(profile_redaction.secrets, "choice", lambda values: values[-1])
    monkeypatch.setattr(profile_redaction.secrets, "randbelow", lambda _limit: 0)

    redacted = redact_profile(_samples())[1]

    assert redacted["sample_values"] == [
        {"value": "1900-01-01", "count": 4}
    ]
    assert redacted["min_numeric"] == 9.9
    assert redacted["max_numeric"] == 9.9
    assert redacted["min_date"] == "1900-01-01"
    assert redacted["max_date"] == "1900-01-01"


def test_identical_clear_values_are_randomized_independently(monkeypatch) -> None:
    replacements = iter([*"AAAA", *"BBBB"])
    monkeypatch.setattr(
        profile_redaction.secrets,
        "choice",
        lambda _values: next(replacements),
    )

    redacted = redact_profile([
        {
            "column": "Name",
            "sample_values": [
                {"value": "ACME", "count": 2},
                {"value": "ACME", "count": 1},
            ],
        },
    ])

    assert redacted[0]["sample_values"] == [
        {"value": "AAAA", "count": 2},
        {"value": "BBBB", "count": 1},
    ]


def test_numeric_text_preserves_format_and_digit_bias_favors_zero_and_one(
    monkeypatch,
) -> None:
    monkeypatch.setattr(profile_redaction.secrets, "choice", lambda values: values[0])

    redacted = redact_profile([
        {
            "column": "Amount",
            "sample_values": [{"value": "-$1,234.50%", "count": 6}],
        },
    ])[0]["sample_values"][0]["value"]

    assert redacted == "-$0,000.00%"
    digits = profile_redaction._DIGITS
    assert digits.count("0") == digits.count("1") == 2
    assert all(digits.count(value) == 1 for value in "23456789")


def test_supported_date_representations_remain_valid(monkeypatch) -> None:
    monkeypatch.setattr(profile_redaction.secrets, "randbelow", lambda _limit: 0)
    source_dates = ["2026-04-30", "04/30/2026", "04-30-2026", "20260430"]

    values = redact_profile([
        {
            "column": "Date",
            "sample_values": [
                {"value": value, "count": 1} for value in source_dates
            ],
        },
    ])[0]["sample_values"]

    formats = ["%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%Y%m%d"]
    for entry, expected_format in zip(values, formats, strict=True):
        datetime.strptime(entry["value"], expected_format)
        assert entry["count"] == 1
