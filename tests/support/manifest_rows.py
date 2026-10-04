"""Flat manifest rows, as control.f_file_manifest_get returns them (backlog 619).

A Loader step no longer reads a selector row: it asks
``ManifestSource.get_for_operation`` for the operation's files and reads every
fact off the objects. A step test therefore primes its Control double with the
rows the routine returns -- one per selected file here, untyped and unprofiled,
which is one row -- and lets a real ManifestSource be built from them.

The keys are the ones ManifestSource materialises; anything a test does not
state is the unconfigured default.
"""

from __future__ import annotations

from typing import Any

__all__ = ["manifest_row"]


def manifest_row(file_manifest_id: int, file_mutation_id: int, path: Any,
                 **facts: Any) -> dict[str, Any]:
    """One governed file at one mutation, as one flat row.

    ``facts`` sets or overrides any column -- classification, base_path,
    conversion, record_type, manifest_data_profile_key, layout and so on.
    """
    return {
        "file_manifest_id": file_manifest_id,
        "file_mutation_id": file_mutation_id,
        "installation_id": 1,
        "file_type_id": None,
        "path": None if path is None else str(path),
        "layout": None,
        "data_profile_id": None,
        "data_profile_field_id": None,
        "profile_header_definition": None,
        "transform_id": None,
        "transform_column_id": None,
        "transform_row_filter": None,
        "transform_is_default": None,
        **facts,
    }
