"""Line-preserving inspection rows (row 589, step 5).

Copied from the legacy file_operator ``inspection`` module, which stays
untouched as the reference. Only what the profile path reaches is copied: the
``InspectionRow`` a delimited read builds. Persisting inspection evidence and
the per-file-type analysis artifacts join this module when a step's production
path reaches them.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "InspectionRow",
]


@dataclass(frozen=True)
class InspectionRow:
    """One safe persisted row with stable source position."""

    source_file: str
    physical_line_number: int
    logical_row_number: int
    text: str
    parsed_fields: tuple[str, ...]
    field_count: int
    line_length: int
    delimiter_count: int
    parse_status: str
    is_blank: bool
    is_detected_header: bool
    is_header_candidate: bool
    header_candidate_identity_sha256: str | None
    region: str
