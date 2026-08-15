"""Deterministic repository-map generation.

Contract: rey_repository_map_generator.sgc.yaml.

Python owns the factual repository inventory and graph; architectural
interpretation happens afterwards against the generated evidence. The engine is
application-neutral — every repository-specific name arrives as scan-rules data
from the repository being scanned.

The generated factual map is a JSONL fact stream: one complete JSON object per
line, every record carrying record_type and record_id.
"""

from __future__ import annotations

from rey_lib.repository_map.inventory import inventory_files, load_scan_rules
from rey_lib.repository_map.records import (
    ENTRY_POINT_LOAD_LOADED,
    ENTRY_POINT_LOAD_NOT_LOADED,
    ENTRY_POINT_LOAD_UNKNOWN,
    LANGUAGE_UNKNOWN,
    RECORD_TYPE_FILE,
    FileRecord,
    ScanRules,
)

__all__ = [
    "ENTRY_POINT_LOAD_LOADED",
    "ENTRY_POINT_LOAD_NOT_LOADED",
    "ENTRY_POINT_LOAD_UNKNOWN",
    "LANGUAGE_UNKNOWN",
    "RECORD_TYPE_FILE",
    "FileRecord",
    "ScanRules",
    "inventory_files",
    "load_scan_rules",
]
