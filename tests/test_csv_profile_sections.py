"""The two sections of an enriched CSV profile, and the line between them.

Distribution facts say what the dataset looks like. ``loader_hints`` says how to
read it. A fact stated in both places is a fact that can disagree with itself,
which is what the retired ``csv`` subsection made possible: it restated the row
and column counts, the delimiter and the encoding, all of which already had a
home.
"""

from rey_lib.profiling.csv_profile import enrich_csv_profile
from rey_lib.profiling.file_profiler import profile_rows
from rey_lib.profiling.profile_validation import validate_csv_profile

ROWS = [
    {"NAME": "Ada", "AMOUNT": "10.5"},
    {"NAME": "Bob", "AMOUNT": "20.0"},
]


def _enriched(**overrides) -> dict:
    base = profile_rows(rows=ROWS, source_name="sample.csv", layout="delimited")
    options = {
        "source_file": "sample.csv",
        "encoding": "utf-8",
        "delimiter": ",",
        "quote_char": '"',
        "has_header": True,
        "blank_line_count": 2,
        "ragged_row_count": 1,
        "max_sample_values": 3,
    }
    options.update(overrides)
    return enrich_csv_profile(base, ROWS, ROWS, **options)


def test_the_retired_csv_subsection_is_gone() -> None:
    """Nothing restates the dataset facts under a format-named section."""
    assert "csv" not in _enriched()


def test_read_instructions_live_only_in_loader_hints() -> None:
    """Delimiter, encoding, quoting and the header flag are stated once."""
    profile = _enriched()

    assert profile["loader_hints"] == {
        "file_type": "CSV",
        "delimiter": ",",
        "encoding": "utf-8",
        "quote_char": '"',
        "header": True,
    }
    # The same instructions must not also appear among the dataset facts.
    for instruction in ("delimiter", "encoding", "quote_char", "has_header"):
        assert instruction not in profile, instruction


def test_dataset_facts_are_stated_once_beside_the_counts() -> None:
    """The promoted counts sit with row_count and column_count, not under csv."""
    profile = _enriched()

    assert profile["row_count"] == 2
    assert profile["column_count"] == 2
    assert profile["blank_line_count"] == 2
    assert profile["ragged_row_count"] == 1
    # The retired subsection's duplicates of the two counts are not reinstated
    # under new names.
    assert "profiled_row_count" not in profile


def test_a_combined_row_count_is_a_dataset_fact() -> None:
    """The combined count joins the other counts rather than a nested section."""
    profile = _enriched(combined_row_count=9)

    assert profile["combined_profiled_row_count"] == 9
    assert "csv" not in profile


def test_a_single_file_profile_states_no_combined_count() -> None:
    """Nothing is invented for a profile that combined nothing."""
    assert "combined_profiled_row_count" not in _enriched()


def test_validation_requires_the_promoted_counts() -> None:
    """Promoting the counts must not let them go missing unnoticed."""
    assert validate_csv_profile(_enriched()) == []

    without_counts = _enriched()
    del without_counts["blank_line_count"]
    errors = validate_csv_profile(without_counts)
    assert any("blank_line_count" in error for error in errors)


def test_validation_no_longer_demands_a_csv_section() -> None:
    """A profile is complete without the section that was retired."""
    profile = _enriched()
    assert "csv" not in profile
    assert validate_csv_profile(profile) == []
