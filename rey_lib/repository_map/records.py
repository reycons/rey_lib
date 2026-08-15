"""Typed records for the deterministic repository map.

Contract: rey_repository_map_generator.sgc.yaml (INC-001).

Records are frozen so a completed scan cannot be mutated afterwards, and every
record serializes through ``to_dict`` into one JSONL record with a fixed key
order, so the generated fact stream stays byte-stable across runs from the same
commit. The generated factual map is JSONL, never a YAML document.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "ENTRY_POINT_LOAD_LOADED",
    "ENTRY_POINT_LOAD_NOT_LOADED",
    "ENTRY_POINT_LOAD_UNKNOWN",
    "LANGUAGE_UNKNOWN",
    "RECORD_TYPE_FILE",
    "FileRecord",
    "ScanRules",
]

# Tri-state for REQ-011's "loaded by a known runtime entry point".
# Entry-point discovery arrives in INC-003. Until then every file records
# UNKNOWN rather than a fabricated answer, per the conservative-evidence rule.
ENTRY_POINT_LOAD_LOADED = "loaded"
ENTRY_POINT_LOAD_NOT_LOADED = "not_loaded"
ENTRY_POINT_LOAD_UNKNOWN = "unknown"

# Language recorded when no configured extension mapping matches the file.
LANGUAGE_UNKNOWN = "unknown"

# JSONL record_type values. The generated factual map is a JSONL fact stream,
# never a YAML document, and every record carries record_type and record_id.
RECORD_TYPE_FILE = "file"


@dataclass(frozen=True)
class FileRecord:
    """One inventoried source file and its deterministic classification.

    Attributes:
        path: Repository-relative POSIX path. The record identity and sort key.
        language: Configured language name, or ``LANGUAGE_UNKNOWN``.
        size_bytes: File size in bytes as reported by the filesystem.
        is_generated: True when the path matches a configured generated glob.
        is_vendor: True when the path matches a configured vendor glob.
        is_test: True when the path matches a configured test glob.
        entry_point_load_state: One of the ``ENTRY_POINT_LOAD_*`` constants.
    """

    path: str
    language: str
    size_bytes: int
    is_generated: bool
    is_vendor: bool
    is_test: bool
    entry_point_load_state: str = ENTRY_POINT_LOAD_UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        """Return this file as a JSONL 'file' record.

        Key order is fixed so the serialized stream is byte-stable across runs.

        Returns:
            The record, conforming to jsonl_record_contract.record_types.file.
        """
        return {
            "record_type": RECORD_TYPE_FILE,
            "record_id": f"{RECORD_TYPE_FILE}:{self.path}",
            "path": self.path,
            "language": self.language,
            "size_bytes": self.size_bytes,
            "classification": {
                "generated": self.is_generated,
                "vendor": self.is_vendor,
                "test": self.is_test,
            },
            "entry_point_load_state": self.entry_point_load_state,
        }


@dataclass(frozen=True)
class ScanRules:
    """Per-repository scan configuration supplied as data, never as code.

    The engine stays application-neutral: every repository-specific name lives
    in the scanned repository's own rules file (ARCH-005).

    Glob semantics — these are NOT shell globs:
        Every glob is matched with ``fnmatch.fnmatchcase`` against the
        repository-relative POSIX path. Consequences worth stating outright,
        because shell and pathlib habits predict otherwise:

        - ``*`` crosses directory separators. ``static/*`` matches
          ``static/js/react/bundle.js``, not only ``static/bundle.js``.
        - There is no ``**``. Writing it adds nothing that ``*`` lacks.
        - Matching is case-sensitive on every platform, so a scan cannot vary
          with the case sensitivity of the filesystem underneath it.
        - Patterns are anchored at the repository root. ``tests/*`` matches only
          a top-level tests directory; use ``*/tests/*`` for nested ones.

        Hidden paths never reach glob matching at all. Excluding any path
        segment beginning with '.' is repository policy applied by the scanner
        before stat, not a rule expressible here.

    Attributes:
        ignored_directory_names: Directory names pruned during the walk.
        ignored_path_globs: Globs excluding files by repository-relative path.
        language_by_extension: Lowercase file suffix to language name.
        generated_path_globs: Globs marking build output.
        vendor_path_globs: Globs marking third-party code.
        test_path_globs: Globs marking test code.
    """

    ignored_directory_names: frozenset[str]
    ignored_path_globs: tuple[str, ...]
    language_by_extension: dict[str, str]
    generated_path_globs: tuple[str, ...]
    vendor_path_globs: tuple[str, ...]
    test_path_globs: tuple[str, ...]

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "ScanRules":
        """Build rules from a parsed rules mapping.

        Args:
            data: Parsed contents of a repository's rules file.

        Returns:
            The typed rules.

        Raises:
            ValueError: If the mapping or any recognized section has the wrong
                shape. Missing sections are permitted and yield empty rules.
        """
        if not isinstance(data, dict):
            raise ValueError(f"Scan rules must be a mapping, got {type(data).__name__}.")

        extensions = _require_str_mapping(data, "language_by_extension")
        return cls(
            ignored_directory_names=frozenset(_require_str_list(data, "ignored_directory_names")),
            ignored_path_globs=tuple(_require_str_list(data, "ignored_path_globs")),
            # Suffixes are compared lowercase so casing in the rules file is not
            # a source of nondeterminism between platforms.
            language_by_extension={key.lower(): value for key, value in extensions.items()},
            generated_path_globs=tuple(_require_str_list(data, "generated_path_globs")),
            vendor_path_globs=tuple(_require_str_list(data, "vendor_path_globs")),
            test_path_globs=tuple(_require_str_list(data, "test_path_globs")),
        )


def _require_str_list(data: dict[str, Any], key: str) -> list[str]:
    """Return a list-of-strings section, defaulting to empty when absent.

    Args:
        data: Parsed rules mapping.
        key: Section name to read.

    Returns:
        The section values.

    Raises:
        ValueError: If the section is present but is not a list of strings.
    """
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Scan rules section '{key}' must be a list of strings.")
    return value


def _require_str_mapping(data: dict[str, Any], key: str) -> dict[str, str]:
    """Return a string-to-string section, defaulting to empty when absent.

    Args:
        data: Parsed rules mapping.
        key: Section name to read.

    Returns:
        The section mapping.

    Raises:
        ValueError: If the section is present but is not a string mapping.
    """
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(item, str) for pair in value.items() for item in pair
    ):
        raise ValueError(f"Scan rules section '{key}' must map strings to strings.")
    return value
