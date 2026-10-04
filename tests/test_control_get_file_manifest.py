"""Control.get_file_manifest returns the control.file_manifest row, from the flat projection.

f_file_manifest_get returns control.transform_flat_vw rows (backlog 618). The
manifest's own columns repeat on every row, six of them under manifest_* names,
because the projection gives the plain names to the WORKING MUTATION. The
reduction must return exactly the manifest row callers have always received.
No database is opened: the routine call is recorded, not made.
"""

from __future__ import annotations

from typing import Any

from rey_lib.control.control import Control

#: The manifest as control.file_manifest holds it, in the table's column order.
_MANIFEST_ROW: dict[str, Any] = {
    "file_manifest_id": 11,
    "path": "/data/feed/source/inbox/a.csv",
    "file_name": "a.csv",
    "base_name": "a",
    "file_extension": "csv",
    "checksum_sha256": "c" * 64,
    "size_bytes": 1234,
    "source_name": "feed",
    "evidence": {"found_by": "inventory"},
    "producer": {"operation": "inventory"},
    "created_ts": "2026-10-01T09:00:00+00:00",
    "last_updated_ts": "2026-10-02T09:00:00+00:00",
    "data_profile_key": "manifest-profile-key",
    "installation_id": 3,
    "file_type_id": 5,
    "batch_step_id": 900,
}


def _flat_row(field_ordinal: int) -> dict[str, Any]:
    """One flat row of that manifest at a working mutation that has MOVED it.

    Every plain name that means the working mutation carries a value different
    from the manifest's, so reading a plain name instead of its manifest_*
    counterpart fails the comparison rather than passing by coincidence.
    """
    m = _MANIFEST_ROW
    return {
        "file_manifest_id": m["file_manifest_id"],
        "file_mutation_id": 77,
        "installation_id": m["installation_id"],
        "file_type_id": m["file_type_id"],
        "path": "/data/feed/work/sanitized_csv/a.csv",
        "file_name": m["file_name"],
        "base_name": m["base_name"],
        "file_extension": m["file_extension"],
        "checksum_sha256": m["checksum_sha256"],
        "size_bytes": m["size_bytes"],
        "source_name": m["source_name"],
        "evidence": m["evidence"],
        "data_profile_key": "the-file-types-profile-key",
        "mutation_batch_step_id": 901,
        "mutation_producer": {"operation": "file_sanitization"},
        "manifest_path": m["path"],
        "manifest_producer": m["producer"],
        "inventoried_ts": m["created_ts"],
        "manifest_updated_ts": m["last_updated_ts"],
        "manifest_data_profile_key": m["data_profile_key"],
        "manifest_batch_step_id": m["batch_step_id"],
        "field_ordinal": field_ordinal,
    }


def _answering(rows: list[dict[str, Any]]) -> tuple[Control, list[tuple[str, dict[str, Any]]]]:
    """A Control whose routine calls are recorded and answered with ``rows``."""
    calls: list[tuple[str, dict[str, Any]]] = []
    control = Control.__new__(Control)

    def _call_rows(action: str, variables: dict[str, Any],
                   required: bool = False) -> list[dict[str, Any]]:
        calls.append((action, dict(variables)))
        return [dict(row) for row in rows]

    control._call_rows = _call_rows  # type: ignore[method-assign]
    return control, calls


def test_the_flat_rows_reduce_to_the_manifest_row() -> None:
    control, _ = _answering([_flat_row(1), _flat_row(2), _flat_row(3)])

    assert control.get_file_manifest(11) == _MANIFEST_ROW


def test_the_columns_arrive_in_the_tables_order() -> None:
    control, _ = _answering([_flat_row(1)])

    assert list(control.get_file_manifest(11)) == list(_MANIFEST_ROW)


def test_the_working_mutations_facts_are_not_the_manifests() -> None:
    """The silent case backlog 618 found: the plain names are the mutation's."""
    control, _ = _answering([_flat_row(1)])

    manifest = control.get_file_manifest(11)

    assert manifest["path"] == "/data/feed/source/inbox/a.csv"
    assert manifest["data_profile_key"] == "manifest-profile-key"
    assert manifest["batch_step_id"] == 900


def test_no_rows_is_none() -> None:
    control, _ = _answering([])

    assert control.get_file_manifest(11) is None


def test_only_the_manifest_id_is_sent() -> None:
    """The installation is read off Control.installation_id by the binding."""
    control, calls = _answering([_flat_row(1)])

    control.get_file_manifest(11)

    assert calls == [("get_file_manifest", {"file_manifest_id": 11})]
