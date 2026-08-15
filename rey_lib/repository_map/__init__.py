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

from rey_lib.repository_map.entry_points import extract_runtime_entry_points
from rey_lib.repository_map.extractors import (
    LANGUAGE_EXTRACTORS,
    LanguageExtractor,
    extract_executable_references,
    extract_symbols,
    supported_languages,
)
from rey_lib.repository_map.globals_scan import (
    GlobalReport,
    extract_global_publications_and_consumers,
)
from rey_lib.repository_map.inventory import inventory_files, load_scan_rules
from rey_lib.repository_map.registrations import extract_registrations
from rey_lib.repository_map.records import (
    EDGE_KIND_BACKEND_STRING_REFERENCE,
    EDGE_KIND_CALL,
    EDGE_KIND_GLOBAL_REFERENCE,
    EDGE_KIND_IMPORT,
    EDGE_KIND_PROPERTY_ACCESS,
    EDGE_KIND_REGISTRATION,
    EDGE_KIND_RE_EXPORT,
    EDGE_KIND_TEMPLATE_LOAD,
    ENTRY_POINT_LOAD_LOADED,
    ENTRY_POINT_LOAD_NOT_LOADED,
    ENTRY_POINT_LOAD_UNKNOWN,
    LANGUAGE_UNKNOWN,
    RECORD_TYPE_DEPENDENCY_EDGE,
    RECORD_TYPE_FILE,
    RECORD_TYPE_SYMBOL,
    SYMBOL_KIND_CLASS,
    SYMBOL_KIND_ENUM,
    SYMBOL_KIND_EXPORT,
    SYMBOL_KIND_FUNCTION,
    SYMBOL_KIND_GLOBAL_PUBLICATION,
    SYMBOL_KIND_INTERFACE,
    SYMBOL_KIND_RE_EXPORT,
    SYMBOL_KIND_TYPE_ALIAS,
    SYMBOL_KIND_VARIABLE,
    FileRecord,
    ReferenceEdge,
    ScanRules,
    SymbolInventory,
    SymbolRecord,
)

__all__ = [
    "EDGE_KIND_BACKEND_STRING_REFERENCE",
    "EDGE_KIND_CALL",
    "EDGE_KIND_GLOBAL_REFERENCE",
    "EDGE_KIND_IMPORT",
    "EDGE_KIND_PROPERTY_ACCESS",
    "EDGE_KIND_REGISTRATION",
    "EDGE_KIND_RE_EXPORT",
    "EDGE_KIND_TEMPLATE_LOAD",
    "ENTRY_POINT_LOAD_LOADED",
    "ENTRY_POINT_LOAD_NOT_LOADED",
    "ENTRY_POINT_LOAD_UNKNOWN",
    "LANGUAGE_EXTRACTORS",
    "LANGUAGE_UNKNOWN",
    "RECORD_TYPE_DEPENDENCY_EDGE",
    "RECORD_TYPE_FILE",
    "RECORD_TYPE_SYMBOL",
    "SYMBOL_KIND_CLASS",
    "SYMBOL_KIND_ENUM",
    "SYMBOL_KIND_EXPORT",
    "SYMBOL_KIND_FUNCTION",
    "SYMBOL_KIND_GLOBAL_PUBLICATION",
    "SYMBOL_KIND_INTERFACE",
    "SYMBOL_KIND_RE_EXPORT",
    "SYMBOL_KIND_TYPE_ALIAS",
    "SYMBOL_KIND_VARIABLE",
    "FileRecord",
    "GlobalReport",
    "LanguageExtractor",
    "ReferenceEdge",
    "ScanRules",
    "SymbolInventory",
    "SymbolRecord",
    "extract_executable_references",
    "extract_global_publications_and_consumers",
    "extract_registrations",
    "extract_runtime_entry_points",
    "extract_symbols",
    "inventory_files",
    "load_scan_rules",
    "supported_languages",
]
