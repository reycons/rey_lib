"""
Validation for enriched CSV profile JSON.

Confirms a CSV-enriched profile carries the sections and per-column fields that
downstream LLM steps rely on, so an invalid profile is never treated as a
successfully processed file. Lint-only: returns a list of human-readable error
strings (empty when valid); it does not mutate the profile.

Public API
----------
validate_csv_profile   Return a list of validation errors (empty when valid).
"""

from __future__ import annotations

from typing import Any

from rey_lib.logs import get_logger

__all__ = ["validate_csv_profile"]

_logger = get_logger(__name__)

_REQUIRED_TOP_LEVEL = (
    "source",
    "csv",
    "columns",
    "loader_hints",
    "llm_hints",
    "profile_scope",
    "source_files",
    "file_count",
)
_REQUIRED_COLUMN_FIELDS = (
    "ordinal",
    "raw_name",
    "normalized_name",
    "safe_sql_name",
    "blank_count",
    "non_blank_count",
    "max_length",
    "sample_values",
    "type_hint",
)


def validate_csv_profile(profile: dict[str, Any]) -> list[str]:
    """Return validation errors for an enriched CSV profile.

    Parameters
    ----------
    profile : dict[str, Any]
        The enriched profile dict.

    Returns
    -------
    list[str]
        Human-readable error messages. Empty when the profile is valid.
    """
    errors: list[str] = []

    if not isinstance(profile, dict):
        return ["profile is not a JSON object."]

    for section in _REQUIRED_TOP_LEVEL:
        if section not in profile:
            errors.append(f"missing required profile section: '{section}'.")

    columns = profile.get("columns")
    if not isinstance(columns, list) or not columns:
        errors.append("profile 'columns' must be a non-empty list.")
        return errors

    for index, column in enumerate(columns):
        if not isinstance(column, dict):
            errors.append(f"column at index {index} is not an object.")
            continue
        label = column.get("raw_name", f"index {index}")
        for field in _REQUIRED_COLUMN_FIELDS:
            if field not in column:
                errors.append(f"column '{label}' missing required field: '{field}'.")

    return errors
