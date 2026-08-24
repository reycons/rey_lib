"""The one entry point for configured file-manifest record selection.

This module introduces no search engine. It validates the declared
``manifest_selection`` configuration, reads the governed records, and evaluates
each declared record set against them:

    configured YAML matching
      -> select_manifest_records(...)
        -> read_records_from_control(...)  then match

A ``match`` whose fields are all top-level and whose values are all scalars is
compared directly. A ``match`` that addresses a nested field or lists accepted
values is evaluated through the same JMESPath expression the file-backed search
used, applied to the record itself -- so what a configured match means did not
change when the manifest moved into the database.

Selections are keyed by ``record_id``, the governed identity of the record. The
manifest is a table; there is no line to key a selection on.

Selection is deliberately narrow. It resolves no file path, joins no lifecycle
records, infers no lineage or file type, inspects no filesystem, and interprets
no workflow-specific meaning. Those are separate responsibilities built on top.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from rey_lib.logs.file_manifest import FileManifestError, resolve_file_manifest_path

__all__ = [
    "ManifestRecordSelection",
    "ManifestSelectionError",
    "SelectedManifestRecord",
    "normalize_manifest_selection",
    "select_manifest_records",
]

_RECORD_TYPE_FIELD = "record_type"


class ManifestSelectionError(Exception):
    """Raised when a manifest selection is malformed or cannot be performed."""


@dataclass(frozen=True)
class SelectedManifestRecord:
    """One manifest record selected by one declared record set."""

    record_set_index: int
    record_type: str
    record_id: int
    record: Mapping[str, Any]


@dataclass(frozen=True)
class ManifestRecordSelection:
    """Every record the declared selection matched, in manifest order."""

    manifest_path: str
    records: tuple[SelectedManifestRecord, ...]
    records_read: int
    records_selected: int

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self) -> Iterator[SelectedManifestRecord]:
        """Iterate selected records in manifest order."""
        return iter(self.records)


def normalize_manifest_selection(manifest_selection: Any) -> Any:
    """Return a declared selection as plain mappings and lists.

    Configuration reaches a consumer as Namespaces, so normalizing it here keeps
    every consumer storing, comparing, and recording the one same shape rather
    than each converting it privately.
    """
    if isinstance(manifest_selection, Mapping):
        return {
            str(key): normalize_manifest_selection(value)
            for key, value in manifest_selection.items()
        }
    if callable(getattr(manifest_selection, "keys", None)):
        return {
            str(key): normalize_manifest_selection(_get(manifest_selection, key))
            for key in manifest_selection.keys()
        }
    if isinstance(manifest_selection, (list, tuple)):
        return [normalize_manifest_selection(item) for item in manifest_selection]
    return manifest_selection


def select_manifest_records(
    ctx: Any,
    manifest_selection: Any,
) -> ManifestRecordSelection:
    """Select manifest records described by one ``manifest_selection`` section.

    Parameters
    ----------
    ctx : Any
        Resolved context carrying the installation path resolver.
    manifest_selection : Any
        The declared section: ``file_manifest`` plus an ordered ``record_sets``
        list, each entry carrying its own ``record_type`` and ``match``.

    Returns
    -------
    ManifestRecordSelection
        Matching records in manifest order, each attributed to the record set
        that selected it.

    Raises
    ------
    ManifestSelectionError
        If the configuration is malformed, or the governed manifest is missing,
        unreadable, or not valid JSONL.
    """
    # Imported lazily because rey_lib.files.jsonl imports get_logger from this
    # package; a module-level import would close a cycle.
    record_sets = _validated_record_sets(manifest_selection)
    manifest_path = _governed_manifest_path(ctx, manifest_selection)

    from rey_lib.logs.file_manifest import (
        FileManifestError, read_records_from_control,
    )

    try:
        # One read establishes the governed set and the read total. Matching is
        # evaluated here, over the records, rather than by a file reader: the
        # manifest is a table now and there is no line to key a selection on.
        all_records = read_records_from_control(ctx)
    except FileManifestError as exc:
        raise ManifestSelectionError(
            f"Manifest selection cannot read the governed file manifest: {exc}"
        ) from exc

    selected: dict[int, SelectedManifestRecord] = {}
    for index, (record_type, match) in enumerate(record_sets):
        for record_id, record in _matching_records(all_records, record_type, match):
            # A record matching more than one set is one selected record,
            # attributed to the first set that claimed it. Distinct records
            # are never collapsed.
            selected.setdefault(
                record_id,
                SelectedManifestRecord(
                    record_set_index=index,
                    record_type=record_type,
                    record_id=record_id,
                    record=record,
                ),
            )

    ordered = tuple(selected[key] for key in sorted(selected))
    return ManifestRecordSelection(
        manifest_path=str(manifest_path),
        records=ordered,
        records_read=len(all_records),
        records_selected=len(ordered),
    )


def _matching_records(
    records: list[dict[str, Any]],
    record_type: str,
    match: Mapping[str, Any],
) -> list[tuple[int, Mapping[str, Any]]]:
    """Return the records of one set, keyed by their governed identity.

    Plain top-level equality is compared directly. Anything with a dotted path
    or a list of accepted values goes through the same JMESPath expression the
    file-backed search used, evaluated against the record itself, so the match
    semantics are unchanged by the storage move.
    """
    if _is_plain_equality(match):
        wanted = {_RECORD_TYPE_FIELD: record_type, **dict(match)}
        return [
            (int(record["record_id"]), record)
            for record in records
            if all(record.get(field) == expected
                   for field, expected in wanted.items())
        ]

    import jmespath
    from jmespath.exceptions import JMESPathError

    expression = _search_expression(record_type, match)
    try:
        compiled = jmespath.compile(expression)
    except JMESPathError as exc:
        raise ManifestSelectionError(
            f"Manifest selection built an invalid match expression: {exc}"
        ) from exc
    matched = []
    for record in records:
        value = compiled.search(record)
        if isinstance(value, Mapping):
            matched.append((int(record["record_id"]), value))
    return matched


def _is_plain_equality(match: Mapping[str, Any]) -> bool:
    """Return whether top-level exact equality already covers this match."""
    return all(
        "." not in field and not isinstance(expected, (list, tuple))
        for field, expected in match.items()
    )


def _search_expression(record_type: str, match: Mapping[str, Any]) -> str:
    """Translate one record set into a JMESPath expression yielding the record.

    The expression evaluates to the record when every declared term holds and to
    null otherwise, so the shared search performs all comparison.
    """
    terms = [f"{_reference(_RECORD_TYPE_FIELD)} == {_literal(record_type)}"]
    for field, expected in match.items():
        reference = _reference(field)
        if isinstance(expected, (list, tuple)):
            terms.append(f"contains({_literal(list(expected))}, {reference})")
        else:
            terms.append(f"{reference} == {_literal(expected)}")
    return "[@] | [?" + " && ".join(terms) + "] | [0]"


def _reference(field: str) -> str:
    """Return one dotted field path as quoted JMESPath identifiers."""
    return ".".join(json.dumps(segment) for segment in field.split("."))


def _literal(value: Any) -> str:
    """Return one JMESPath JSON literal for any comparable value."""
    return "`" + json.dumps(value).replace("`", "\\`") + "`"


def _governed_manifest_path(ctx: Any, manifest_selection: Any) -> Path:
    """Resolve the governed manifest and require the declaration to name it."""
    try:
        governed = resolve_file_manifest_path(ctx).expanduser().resolve()
    except FileManifestError as exc:
        raise ManifestSelectionError(str(exc)) from exc

    declared = _get(manifest_selection, "file_manifest")
    if declared in (None, ""):
        raise ManifestSelectionError(
            "manifest_selection requires a 'file_manifest' value."
        )
    if not isinstance(declared, (str, Path)):
        raise ManifestSelectionError(
            "manifest_selection 'file_manifest' must be a path."
        )
    if Path(declared).expanduser().resolve() != governed:
        raise ManifestSelectionError(
            f"manifest_selection 'file_manifest' must name the installation's "
            f"governed manifest '{governed}'; found '{declared}'."
        )
    return governed


def _validated_record_sets(
    manifest_selection: Any,
) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    """Validate the declared record sets before any manifest is read."""
    if manifest_selection is None:
        raise ManifestSelectionError("manifest_selection is required.")

    raw = _get(manifest_selection, "record_sets")
    entries = list(raw) if isinstance(raw, (list, tuple)) else None
    if not entries:
        raise ManifestSelectionError(
            "manifest_selection requires a non-empty 'record_sets' list."
        )

    validated: list[tuple[str, Mapping[str, Any]]] = []
    for index, entry in enumerate(entries):
        label = f"manifest_selection record_sets[{index}]"
        if not _is_mapping_like(entry):
            raise ManifestSelectionError(f"{label} must be a mapping.")

        record_type = _get(entry, _RECORD_TYPE_FIELD)
        if not isinstance(record_type, str) or not record_type.strip():
            raise ManifestSelectionError(
                f"{label} requires a non-empty 'record_type'."
            )

        validated.append((record_type.strip(), _validated_match(entry, label)))
    return tuple(validated)


def _validated_match(entry: Any, label: str) -> Mapping[str, Any]:
    """Validate one record set's match object; an absent match matches all."""
    raw = _get(entry, "match")
    if raw is None:
        return {}
    if not _is_mapping_like(raw):
        raise ManifestSelectionError(f"{label} 'match' must be a mapping.")

    match: dict[str, Any] = {}
    for field in _keys(raw):
        name = str(field).strip()
        if not name:
            raise ManifestSelectionError(
                f"{label} 'match' declares an empty field name."
            )
        value = _get(raw, field)
        if isinstance(value, (list, tuple)) and not list(value):
            raise ManifestSelectionError(
                f"{label} 'match' field '{name}' declares an empty value list."
            )
        match[name] = list(value) if isinstance(value, (list, tuple)) else value
    return match


def _is_mapping_like(value: Any) -> bool:
    """Return whether a value is a mapping or a config Namespace."""
    return isinstance(value, Mapping) or callable(getattr(value, "keys", None))


def _keys(value: Any) -> Sequence[Any]:
    """Return the declared keys of a mapping or config Namespace."""
    if isinstance(value, Mapping):
        return list(value.keys())
    return list(value.keys())


def _get(value: Any, name: str, default: Any = None) -> Any:
    """Return one field from a mapping or config Namespace."""
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)
