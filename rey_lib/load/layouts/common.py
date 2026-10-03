"""
Shared helpers for the Loader's delimited layout functions (row 589, step 5).

Copied from the legacy file_operator ``layouts.common`` module, which stays
untouched as the reference. Only the profile path's dependency closure is
copied: effective-setting resolution and the redaction plan. The output writer
and the per-file summary join this module when a step's production path
reaches them.

Public API
----------
effective_max_sample_rows Resolve the governed row limit from args.
effective_encoding    Resolve encoding from args, else the code default.
effective_delimiter   Resolve delimiter from args, else the code default.
prepare_redaction     Build the (redact_columns, mask_types) plan.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rey_lib.logs import get_logger

from rey_lib.load.input_utils import parse_column_list
from rey_lib.load.profile_errors import ProfilingError

__all__: list[str] = [
    "effective_sample_size",
    "effective_max_sample_rows",
    "effective_encoding",
    "effective_delimiter",
    "effective_max_sample_values",
    "prepare_redaction",
    "DEFAULT_MAX_SAMPLE_ROWS",
    "DEFAULT_ENCODING",
    "DEFAULT_DELIMITER",
    "DEFAULT_MAX_SAMPLE_VALUES",
]

_log = get_logger(__name__)

# Code-level fallbacks used only when neither the YAML nor CLI specifies a value.
DEFAULT_MAX_SAMPLE_ROWS: int = 500
DEFAULT_ENCODING:    str = "utf-8-sig"
DEFAULT_DELIMITER:   str = ","
DEFAULT_MAX_SAMPLE_VALUES: int = 10


def effective_max_sample_rows(args: Any) -> int:
    """Return the one governed sample-row limit from canonical or legacy args."""
    canonical = getattr(args, "max_sample_rows", None)
    legacy = getattr(args, "sample_size", None)
    if canonical is not None and legacy is not None:
        raise ProfilingError(
            "max_sample_rows and legacy sample_size cannot both be supplied."
        )
    value = canonical if canonical is not None else legacy
    return int(value) if value else DEFAULT_MAX_SAMPLE_ROWS


def effective_sample_size(args: Any) -> int:
    """Compatibility wrapper for the canonical sample-row limit resolver."""
    return effective_max_sample_rows(args)


def effective_encoding(args: Any) -> str:
    """Return the file encoding from args, falling back to the code default."""
    return getattr(args, "encoding", None) or DEFAULT_ENCODING


def effective_delimiter(args: Any) -> str:
    """Return the field delimiter from args, falling back to the code default."""
    return getattr(args, "delimiter", None) or DEFAULT_DELIMITER


def effective_max_sample_values(args: Any) -> int:
    """Return the per-column sample-value cap from args, else the code default.

    Resolved from the ``profiling.max_sample_values_per_column`` feed setting
    (surfaced on ``args`` by feed config), falling back to the code default.
    """
    val = getattr(args, "max_sample_values_per_column", None)
    return int(val) if val else DEFAULT_MAX_SAMPLE_VALUES


def prepare_redaction(
    args:              Any,
    *,
    available_columns: list[str],
    detect_masks:      Callable[[list[str]], dict[str, str]],
    source_name:       str,
) -> tuple[list[str], dict[str, str]]:
    """Return the single redaction plan used by tabular layouts.

    Parameters
    ----------
    args : Any
        Parsed CLI/feed arguments (redact_columns, redact_all, redact_masks).
    available_columns : list[str]
        Column names present in the file.
    detect_masks : Callable[[list[str]], dict[str, str]]
        Layout-specific mask detector for the chosen redaction columns.
    source_name : str
        Source file name (for logging).

    Returns
    -------
    tuple[list[str], dict[str, str]]
        ``(redact_columns, mask_types)``.
    """
    requested_columns = parse_column_list(args.redact_columns)
    if getattr(args, "redact_all", False):
        redact_columns = list(available_columns)
    else:
        available = set(available_columns)
        redact_columns = [col for col in requested_columns if col in available]

    mask_types = detect_masks(redact_columns)
    explicit_masks = getattr(args, "redact_masks", None) or {}
    mask_types = {**mask_types, **explicit_masks}

    _log.info(
        "%s: redaction plan — columns=%s masks=%s",
        source_name, redact_columns, mask_types,
    )
    return redact_columns, mask_types
