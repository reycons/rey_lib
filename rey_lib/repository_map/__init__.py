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

from rey_lib.repository_map.architecture_policy import (
    ArchitectureRuleSource,
    CompiledArchitecturePolicy,
    compile_architecture_policy,
)
from rey_lib.repository_map.boundaries import check_architecture_boundaries
from rey_lib.repository_map.increment_gate import (
    AgentHandoff,
    ArchitectureDiff,
    build_agent_handoff,
    diff_architecture_violations,
)
from rey_lib.repository_map.migration import (
    MigrationManifest,
    MigrationRow,
    RetirementReport,
    load_migration_manifest,
    validate_migration_manifest,
    verify_retirement_ready,
)
from rey_lib.repository_map.dispatchers import inventory_dispatchers_and_switches
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
from rey_lib.repository_map.graph import (
    RepositoryGraph,
    build_dependency_graph,
    compute_reachability,
)
from rey_lib.repository_map.inventory import inventory_files, load_scan_rules
from rey_lib.repository_map.registrations import extract_registrations
from rey_lib.repository_map.review import (
    ReviewDecision,
    ReviewDocument,
    load_review,
    validate_review,
    verify_generated_map_unedited,
)
from rey_lib.repository_map.system_index import (
    SystemIndex,
    build_system_index,
    load_repository_baselines,
    validate_system_index,
    write_system_index,
)
from rey_lib.repository_map.writer import (
    GENERATOR_VERSION,
    MapDiff,
    RepositoryMap,
    compare_repository_maps,
    generate_repository_map,
    write_repository_map,
)
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
    "GENERATOR_VERSION",
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
    "MapDiff",
    "ReferenceEdge",
    "RepositoryGraph",
    "RepositoryMap",
    "ReviewDecision",
    "ReviewDocument",
    "ScanRules",
    "SymbolInventory",
    "SymbolRecord",
    "SystemIndex",
    "build_dependency_graph",
    "build_system_index",
    "ArchitectureRuleSource",
    "CompiledArchitecturePolicy",
    "AgentHandoff",
    "ArchitectureDiff",
    "MigrationManifest",
    "MigrationRow",
    "RetirementReport",
    "build_agent_handoff",
    "check_architecture_boundaries",
    "diff_architecture_violations",
    "load_migration_manifest",
    "validate_migration_manifest",
    "verify_retirement_ready",
    "compile_architecture_policy",
    "compare_repository_maps",
    "compute_reachability",
    "extract_executable_references",
    "extract_global_publications_and_consumers",
    "extract_registrations",
    "extract_runtime_entry_points",
    "extract_symbols",
    "generate_repository_map",
    "inventory_dispatchers_and_switches",
    "inventory_files",
    "load_repository_baselines",
    "load_review",
    "load_scan_rules",
    "supported_languages",
    "validate_review",
    "validate_system_index",
    "verify_generated_map_unedited",
    "write_repository_map",
    "write_system_index",
]
