"""Parity tests for the config_utils responsibility split.

Guards SGC_Rey_Lib_Config_Utils_Responsibility_Split: the compatibility facade
must keep re-exporting the public surface, and the re-exported objects must be
the same objects defined in the focused modules (no divergent copies).
"""

from __future__ import annotations

from rey_lib.config import (
    config_context,
    config_loader,
    config_namespace,
    config_paths,
    config_utils,
    provenance,
)

# Public name -> module that now owns the implementation.
_PUBLIC_SURFACE = {
    "Namespace": config_namespace,
    "PathResolver": config_paths,
    "build_ctx_from_path": config_context,
    "inject_secrets": config_context,
    "print_ctx": config_context,
    "parse_yaml": config_loader,
    "parse_yaml_namespace": config_loader,
    "dump_yaml": config_loader,
    "validate_yaml_file": config_loader,
    "validate_yaml_folder": config_loader,
    "get_config_metadata": provenance,
    "get_config_source_files": provenance,
    "explain_config_value": provenance,
}


def test_public_surface_reexported_and_identical() -> None:
    """Every public name resolves via config_utils to the owning module's object."""
    for name, owner in _PUBLIC_SURFACE.items():
        assert hasattr(config_utils, name), f"config_utils lost public name {name!r}"
        assert getattr(config_utils, name) is getattr(owner, name), (
            f"config_utils.{name} is not the object from {owner.__name__}"
        )


def test_all_matches_reexports() -> None:
    """__all__ lists exactly the public names, all importable."""
    assert set(config_utils.__all__) == set(_PUBLIC_SURFACE)


def test_private_helpers_still_importable() -> None:
    """Private helpers some tests import from config_utils remain available."""
    assert config_utils._apply_path_resolver is config_paths._apply_path_resolver
    assert config_utils._is_path_key is config_paths._is_path_key
    assert config_utils._resolve_paths is config_paths._resolve_paths


def test_path_key_allowlist_moved_unchanged() -> None:
    """The moved path-key allowlist keeps its membership (constant parity)."""
    assert "contract_file" in config_paths._PATH_KEYS
    assert "log_path" in config_paths._LOG_PATH_KEYS
    # A non-path key must never have been added.
    assert "name" not in config_paths._PATH_KEYS


def test_yaml_import_only_in_loader() -> None:
    """`import yaml` is centralised in config_loader; the facade has none."""
    assert getattr(config_loader, "yaml", None) is not None
    assert getattr(config_utils, "yaml", None) is None
    assert getattr(config_context, "yaml", None) is None
    assert getattr(config_paths, "yaml", None) is None
