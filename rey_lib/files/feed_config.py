"""
Feed configuration loader for file_operator.

Reads ``redact.yaml`` from an inbox directory and returns a validated
``FeedConfig``.  The YAML file travels with the data files and makes the
inbox self-describing — redaction columns, layout, encoding, and optional
path overrides are declared once and applied to every file dropped into
the inbox over time.

Path resolution uses a three-tier priority:
  1. ``paths`` block in ``redact.yaml`` (feed-level override, exact path)
  2. ``paths`` block in ``config.{env}.yaml`` (env-level, supports ``{source_name}`` token)
  3. Derived from ``ctx.paths.output_root / source_name / {subdir}``

Public API
----------
FEED_CONFIG_FILENAME    Standard YAML file name looked for in every inbox.
FeedConfig              Dataclass holding all feed-level redaction config.
load_feed_config        Load and validate redact.yaml from an inbox directory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from rey_lib.config import config_utils
from rey_lib.errors.error_utils import ConfigError
from rey_lib.files.file_utils import read_text_file

from rey_lib.files.error_utils import RedactorError

__all__: list[str] = [
    "FEED_CONFIG_FILENAME",
    "FeedConfig",
    "FeedGroup",
    "load_feed_config",
    "validate_feed_groups",
    "VALID_PROFILE_MODES",
    "VALID_COMBINE_STRATEGIES",
    "VALID_UNMATCHED_ACTIONS",
]

# Standard file name — one per inbox directory.
FEED_CONFIG_FILENAME: str = "redact.yaml"

# Allowed enum values for the optional ``feeds`` grouping block.
VALID_PROFILE_MODES: frozenset[str] = frozenset({"single_file", "combined_files"})
VALID_COMBINE_STRATEGIES: frozenset[str] = frozenset({"same_header"})
VALID_UNMATCHED_ACTIONS: frozenset[str] = frozenset({"warn", "skip", "error", "reject"})


@dataclass
class FeedGroup:
    """One explicit feed-grouping rule from the ``feeds`` block.

    Attributes
    ----------
    name : str
        Feed name — used as the combined profile/artifact stem.
    file_pattern : str
        Shell-style glob matched against inbox file names.
    profile_mode : str
        ``single_file`` (one profile per file) or ``combined_files`` (one
        combined profile per feed).
    combine_strategy : str | None
        Required when ``profile_mode`` is ``combined_files``; currently only
        ``same_header``.
    """

    name:             str
    file_pattern:     str
    profile_mode:     str
    combine_strategy: str | None = None


@dataclass
class FeedConfig:
    """Feed-level redaction configuration loaded from ``redact.yaml``.

    Attributes
    ----------
    layout : str
        File layout — ``delimited``, ``fixed_width``, ``excel``, or ``auto``.
    file_pattern : str | list[str]
        Glob pattern or patterns applied to the inbox (default ``*``).
    delimiter : str
        Field delimiter for delimited layouts (default ``,``).
    encoding : str
        File encoding (default ``utf-8-sig``).
    max_sample_rows : int | None
        Maximum sampled rows. ``None`` defers to the code-level default.
        ``sample_size`` remains a temporary read-only compatibility alias.
    redact_all : bool
        When ``True`` every column is redacted and the app infers the mask
        type from each column's values automatically.  Explicit
        ``redact_masks`` entries override the auto-detected type for
        specific columns.
    redact_columns : list[str]
        Column names to redact (delimited and Excel layouts).
    redact_ranges : list[str]
        Range strings in ``start-end`` form (fixed-width layout).
    redact_masks : dict[str, str]
        Optional mapping of column name → mask type string.  Supported
        types: ``ssn``, ``phone``, ``email``, ``name``, ``text``,
        ``date``, ``zip``, ``account``, ``integer``, ``decimal``.
        Columns not listed here use auto-detected type-aware redaction
        when available.
    output_path : str | None
        Absolute path for redacted output files.  Overrides env config and
        the derived default when set.
    processed_path : str | None
        Absolute path for successfully processed source files.
    rejected_path : str | None
        Absolute path for source files that failed processing.
    """

    layout:         str             = "auto"
    file_pattern:   str | list[str] = "*"
    delimiter:      str             = ","
    encoding:       str             = "utf-8-sig"
    max_sample_rows: int | None     = None
    redact_all:     bool            = False
    redact_columns: list[str]       = field(default_factory=list)
    redact_ranges:  list[str]       = field(default_factory=list)
    redact_masks:   dict[str, str]  = field(default_factory=dict)
    output_path:    str | None      = None
    processed_path: str | None      = None
    rejected_path:  str | None      = None

    # Delimited structure hints — tell the redactor about messy exports.
    skip_blank_lines: bool          = False
    header_contains:  str | None    = None
    drop_ragged_rows: bool          = False

    # Optional source-filename classification applied during file-type updates.
    classification_filename_regex: str | None = None

    # Optional explicit feed grouping. When empty, the inbox is processed with
    # the single ``file_pattern`` above (legacy behavior).
    feeds:             list[FeedGroup] = field(default_factory=list)
    on_unmatched_file: str            = "warn"

    @property
    def sample_size(self) -> int | None:
        """Return the legacy name for compatibility with existing callers."""
        return self.max_sample_rows


def load_feed_config(inbox: Path) -> FeedConfig | None:
    """Load and validate ``redact.yaml`` from an inbox directory.

    Returns ``None`` when no ``redact.yaml`` is present so the caller can
    fall back to CLI arguments.  Raises when the file exists but cannot be
    parsed.

    Parameters
    ----------
    inbox : Path
        Directory to search for ``redact.yaml``.

    Returns
    -------
    FeedConfig | None
        Parsed config, or ``None`` if the file is absent.

    Raises
    ------
    RedactorError
        When ``redact.yaml`` exists but cannot be parsed as a YAML mapping.
    """
    config_path = inbox / FEED_CONFIG_FILENAME
    if not config_path.exists():
        return None

    # File read via rey_lib.files; YAML parsed via config_utils (the only
    # module allowed to import yaml). The Namespace is the folder's redact ctx,
    # from which the typed FeedConfig below is built.
    try:
        data = config_utils.parse_yaml(read_text_file(config_path, encoding="utf-8"))
    except ConfigError as exc:
        raise RedactorError(f"Cannot parse '{config_path}': {exc}") from exc

    if not isinstance(data, dict):
        raise RedactorError(f"'{config_path}' must be a YAML mapping.")

    raw = config_utils.Namespace(data)
    paths = raw.get("paths") or {}

    raw_masks = raw.get("redact_masks") or {}
    file_pattern = _coerce_file_pattern(raw.get("file_pattern", "*"))
    filename_regex = _classification_filename_regex(raw.get("classification"))
    canonical_sample_rows = raw.get("max_sample_rows")
    legacy_sample_rows = raw.get("sample_size")
    if canonical_sample_rows is not None and legacy_sample_rows is not None:
        raise RedactorError(
            "'max_sample_rows' and legacy 'sample_size' cannot both be configured."
        )
    configured_sample_rows = (
        canonical_sample_rows
        if canonical_sample_rows is not None
        else legacy_sample_rows
    )

    return FeedConfig(
        layout         = str(raw.get("layout", "auto")),
        file_pattern   = file_pattern,
        delimiter      = str(raw.get("delimiter", ",")),
        encoding       = str(raw.get("encoding", "utf-8-sig")),
        max_sample_rows = (
            int(configured_sample_rows) if configured_sample_rows else None
        ),
        redact_all     = bool(raw.get("redact_all", False)),
        redact_columns = [str(c) for c in raw.get("redact_columns", [])],
        redact_ranges  = [str(r) for r in raw.get("redact_ranges",  [])],
        redact_masks   = {str(k): str(v) for k, v in raw_masks.items()},
        output_path    = str(paths["output"])    if "output"    in paths else None,
        processed_path = str(paths["processed"]) if "processed" in paths else None,
        rejected_path  = str(paths["rejected"])  if "rejected"  in paths else None,
        skip_blank_lines = bool(raw.get("skip_blank_lines", False)),
        header_contains  = str(raw["header_contains"]) if raw.get("header_contains") else None,
        drop_ragged_rows = bool(raw.get("drop_ragged_rows", False)),
        classification_filename_regex = filename_regex,
        feeds            = _coerce_feeds(raw.get("feeds")),
        on_unmatched_file = str(raw.get("on_unmatched_file", "warn")),
    )


def _classification_filename_regex(raw: object) -> str | None:
    """Return a validated optional ``classification.filename_regex`` value."""
    if raw is None:
        return None
    if not isinstance(raw, (dict, config_utils.Namespace)):
        raise RedactorError("'classification' must be a mapping.")

    value = raw.get("filename_regex")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RedactorError("'classification.filename_regex' must be a non-empty string.")

    try:
        compiled = re.compile(value)
    except re.error as exc:
        raise RedactorError(
            f"'classification.filename_regex' is not a valid regular expression: {exc}"
        ) from exc
    if not compiled.groupindex:
        raise RedactorError(
            "'classification.filename_regex' must contain at least one named capture group."
        )
    return value


def _coerce_feeds(raw: object) -> list[FeedGroup]:
    """Return a list of FeedGroup parsed from the optional ``feeds`` block."""
    if not raw:
        return []
    if not isinstance(raw, list):
        raise RedactorError("'feeds' must be a list of feed mappings.")

    feeds: list[FeedGroup] = []
    for entry in raw:
        # config_utils wraps each mapping in a list as a Namespace; both dict
        # and Namespace expose ``.get``.
        if not isinstance(entry, (dict, config_utils.Namespace)):
            raise RedactorError("each 'feeds' entry must be a mapping.")
        strategy = entry.get("combine_strategy")
        feeds.append(FeedGroup(
            name             = str(entry.get("name", "") or ""),
            file_pattern     = str(entry.get("file_pattern", "") or ""),
            profile_mode     = str(entry.get("profile_mode", "") or ""),
            combine_strategy = str(strategy) if strategy else None,
        ))
    return feeds


def validate_feed_groups(feeds: list[FeedGroup]) -> list[str]:
    """Return validation errors for a list of feed groups (empty when valid).

    Catches missing name/file_pattern/profile_mode, invalid profile_mode,
    ``combined_files`` without a combine_strategy, invalid combine_strategy,
    and duplicate feed names. Overlapping patterns are file-specific and are
    detected when resolving inbox files, not here.

    Parameters
    ----------
    feeds : list[FeedGroup]
        Parsed feed groups.

    Returns
    -------
    list[str]
        Human-readable error messages.
    """
    errors: list[str] = []
    seen_names: set[str] = set()

    for index, feed in enumerate(feeds):
        label = feed.name or f"feed[{index}]"
        if not feed.name:
            errors.append(f"{label}: missing required field 'name'.")
        elif feed.name in seen_names:
            errors.append(f"{label}: duplicate feed name.")
        else:
            seen_names.add(feed.name)

        if not feed.file_pattern:
            errors.append(f"{label}: missing required field 'file_pattern'.")

        if not feed.profile_mode:
            errors.append(f"{label}: missing required field 'profile_mode'.")
        elif feed.profile_mode not in VALID_PROFILE_MODES:
            errors.append(
                f"{label}: invalid profile_mode '{feed.profile_mode}' "
                f"(allowed: {sorted(VALID_PROFILE_MODES)})."
            )

        if feed.profile_mode == "combined_files":
            if not feed.combine_strategy:
                errors.append(f"{label}: combined_files requires 'combine_strategy'.")
            elif feed.combine_strategy not in VALID_COMBINE_STRATEGIES:
                errors.append(
                    f"{label}: invalid combine_strategy '{feed.combine_strategy}' "
                    f"(allowed: {sorted(VALID_COMBINE_STRATEGIES)})."
                )

    return errors


def _coerce_file_pattern(raw: object) -> str | list[str]:
    """Return one file pattern string or a list of file pattern strings."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, Iterable):
        return [str(item) for item in raw]
    return str(raw)
