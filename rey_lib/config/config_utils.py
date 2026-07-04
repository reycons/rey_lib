"""
Compatibility facade for rey_lib config utilities.

The config loader was split by responsibility
(SGC_Rey_Lib_Config_Utils_Responsibility_Split) into focused modules:

  config_namespace.py  — Namespace attribute-access wrapper
  config_paths.py      — path token resolution and path-key handling
  config_loader.py     — YAML load/parse/dump/validate and config merge
  config_context.py    — ctx assembly and effective-config construction
  provenance.py        — config value provenance metadata

This module preserves the public import surface. Existing callers such as
``from rey_lib.config.config_utils import build_ctx_from_path`` continue to work
unchanged. New code may import from the focused modules directly.
"""

from __future__ import annotations

from rey_lib.config.config_context import (
    build_ctx_from_path,
    inject_secrets,
    print_ctx,
)
from rey_lib.config.config_loader import (
    dump_yaml,
    parse_yaml,
    parse_yaml_namespace,
    validate_yaml_file,
    validate_yaml_folder,
)
from rey_lib.config.config_namespace import Namespace
from rey_lib.config.config_paths import (
    PathResolver,
    _apply_path_resolver,
    _is_path_key,
    _resolve_paths,
)
from rey_lib.config.provenance import (
    explain_config_value,
    get_config_metadata,
    get_config_source_files,
    get_config_source_map,
)

__all__ = [
    "build_ctx_from_path",
    "inject_secrets",
    "print_ctx",
    "validate_yaml_file",
    "validate_yaml_folder",
    "parse_yaml",
    "parse_yaml_namespace",
    "dump_yaml",
    "Namespace",
    "PathResolver",
    "get_config_metadata",
    "get_config_source_files",
    "get_config_source_map",
    "explain_config_value",
]
