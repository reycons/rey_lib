"""
Read what an implementer authored, and refuse what cannot be believed.

Generation fails on a malformed or contradictory record rather than skipping
it. A record that cannot be parsed is not a record with no capabilities -- it
is an object whose migration nobody can check, and treating the two alike is
how an inventory becomes wrong quietly.

An authored ``computed_status`` is ignored rather than read. The field exists in
the authored file only so a human writing one can see that the verdict is not
theirs; whatever it says has no effect here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rey_lib.config.config_loader import parse_yaml

from rey_lib.migration_reconciliation.records import (
    DISPOSITIONS,
    DISPOSITION_RETIRED,
    EVIDENCE_REQUIRED,
    CapabilityRow,
    ObjectRecord,
)


class MigrationRecordError(ValueError):
    """An authored record cannot be believed, and generation must stop."""


def load_object_record(path: Path) -> ObjectRecord:
    """Parse and validate one authored capability file.

    Args:
        path: The authored ``<object>.capabilities.yaml``.

    Returns:
        The parsed record.

    Raises:
        MigrationRecordError: If a required field is missing, a disposition is
            not one of the known values, a preserved capability carries no
            evidence, or a retirement names no decision.
    """
    try:
        document = parse_yaml(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 -- any parse failure is a refusal
        raise MigrationRecordError(f"{path.name}: cannot be parsed: {exc}") from exc
    if not isinstance(document, dict):
        raise MigrationRecordError(f"{path.name}: is not a mapping.")

    object_name = _required_text(document, "object", path)
    rows = document.get("capabilities")
    if not isinstance(rows, list) or not rows:
        raise MigrationRecordError(f"{path.name}: declares no capabilities.")

    capabilities = tuple(_capability(row, index, path) for index, row in enumerate(rows))
    _refuse_duplicate_capabilities(capabilities, path)

    return ObjectRecord(
        object_name=object_name,
        path=path,
        increment=_optional_text(document, "increment"),
        capabilities=capabilities,
        predecessors=_string_tuple(document.get("predecessors"), "predecessors", path),
        source_objects=_string_tuple(
            (document.get("provenance") or {}).get("source_objects")
            if isinstance(document.get("provenance"), dict) else None,
            "provenance.source_objects", path,
        ),
    )


def load_all(directory: Path) -> list[ObjectRecord]:
    """Parse every authored record in a directory, in a deterministic order.

    Args:
        directory: Where the authored files live.

    Returns:
        The parsed records, ordered by object name.

    Raises:
        MigrationRecordError: If two files declare the same object.
    """
    records = [load_object_record(path) for path in sorted(directory.glob("*.capabilities.yaml"))]
    seen: dict[str, Path] = {}
    for record in records:
        if record.object_name in seen:
            raise MigrationRecordError(
                f"{record.path.name}: {record.object_name} is already declared by "
                f"{seen[record.object_name].name}.",
            )
        seen[record.object_name] = record.path
    return sorted(records, key=lambda record: record.object_name)


def _capability(row: Any, index: int, path: Path) -> CapabilityRow:
    """Validate one capability row.

    Args:
        row: The authored mapping.
        index: Its position, for an error a reader can locate.
        path: The file it came from.

    Returns:
        The validated row.

    Raises:
        MigrationRecordError: On any violation of the row's own rules.
    """
    where = f"{path.name}: capability {index + 1}"
    if not isinstance(row, dict):
        raise MigrationRecordError(f"{where}: is not a mapping.")

    capability = _required_text(row, "source_capability", path, where)
    disposition = _required_text(row, "disposition", path, where)
    if disposition not in DISPOSITIONS:
        raise MigrationRecordError(
            f"{where} ({capability}): '{disposition}' is not a disposition. "
            f"Known: {', '.join(sorted(DISPOSITIONS))}.",
        )

    evidence = _optional_text(row, "evidence")
    decision = _optional_text(row, "decision")
    target_owner = _optional_text(row, "target_owner")

    if disposition in EVIDENCE_REQUIRED and not evidence:
        raise MigrationRecordError(
            f"{where} ({capability}): {disposition} claims the behaviour survived "
            "and must name evidence.",
        )
    if disposition in EVIDENCE_REQUIRED and not target_owner:
        raise MigrationRecordError(
            f"{where} ({capability}): {disposition} must name the object that answers for it.",
        )
    if disposition == DISPOSITION_RETIRED and not decision:
        raise MigrationRecordError(
            f"{where} ({capability}): a retirement must name the architecture decision "
            "that permitted it.",
        )
    if disposition == DISPOSITION_RETIRED and target_owner:
        raise MigrationRecordError(
            f"{where} ({capability}): a retired capability cannot also name an owner.",
        )

    return CapabilityRow(
        source_capability=capability,
        disposition=disposition,
        target_owner=target_owner,
        evidence=evidence,
        decision=decision,
        shape_change=_optional_text(row, "shape_change"),
    )


def _refuse_duplicate_capabilities(
    capabilities: tuple[CapabilityRow, ...], path: Path,
) -> None:
    """Refuse one capability declared twice, which would let one row hide another."""
    seen: set[str] = set()
    for capability in capabilities:
        key = capability.source_capability.strip().lower()
        if key in seen:
            raise MigrationRecordError(
                f"{path.name}: '{capability.source_capability}' is declared twice.",
            )
        seen.add(key)


def _required_text(source: dict[str, Any], key: str, path: Path, where: str = "") -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MigrationRecordError(f"{where or path.name}: '{key}' is required.")
    return value.strip()


def _optional_text(source: dict[str, Any], key: str) -> str | None:
    value = source.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _string_tuple(value: Any, key: str, path: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise MigrationRecordError(f"{path.name}: '{key}' must be a list of strings.")
    return tuple(item.strip() for item in value if item.strip())
