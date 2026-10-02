"""The authored architecture, as rows the code schema can publish.

``code.concept`` and ``code.realization`` are the authored half of the code
store, and until now nothing filled them: ``CodeIndexDatabaseWriter`` promotes
the scan and says so in its own docstring. This module is the missing producer.

**It serializes; it does not resolve.** A ``realized_by`` string is staged
exactly as authored. Deciding which file, directory or symbol it names is
``code.f_realization_resolution``'s, and ``code.p_publish`` refuses a promotion
where any reference resolves to other than exactly one place. Resolving here
first would be a second implementation of a rule the schema already states, and
two implementations of one rule are two answers waiting to disagree.

**The concept tree is read once, here, from the same authority the JSONL
projection reads.** ``concept_key`` is the dotted path of concept keys, which is
what ``code.concept`` uses as its stable identity -- its surrogate id is
regenerated on every publication and must never be leaned on. Declared order is
display order throughout, so ``sort_order`` is position among siblings and
nothing is sorted.

Nothing here writes to a database. It returns rows; the caller stages them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rey_lib.logs.logging_setup import get_logger
from rey_lib.repository_map.architecture_projection import (
    ArchitectureProjectionError,
    CONCEPTS_KEY,
    CONCEPTS_SECTION,
    _parsed_architecture,
)

__all__ = ["ArchitectureRows", "architecture_rows"]

logger = get_logger(__name__)


class ArchitectureRows:
    """The authored architecture as three sets of staged rows.

    Attributes:
        concepts: One row per concept, in declared order, each naming its
            parent by ``concept_key`` rather than by a surrogate id.
        realizations: One row per ``realized_by`` reference, naming its concept
            the same way. The reference is the authored string, unresolved.
        canonical_objects: One row per ``canonical`` entry: the object that is
            the authority for one responsibility within its concept, its public
            seam and what must not be bypassed. Authored strings, unresolved.
    """

    __slots__ = ("concepts", "realizations", "canonical_objects")

    def __init__(
        self,
        concepts: list[dict[str, Any]],
        realizations: list[dict[str, Any]],
        canonical_objects: list[dict[str, Any]],
    ) -> None:
        self.concepts = concepts
        self.realizations = realizations
        self.canonical_objects = canonical_objects


def architecture_rows(architecture_path: Path) -> ArchitectureRows:
    """Read the authored concept tree as rows for the code schema's staging.

    Args:
        architecture_path: Path to the architecture context document.

    Returns:
        The concept and realization rows, in declared order.

    Raises:
        ArchitectureProjectionError: If the document declares no concepts, or a
            concept declares no key.
    """
    document = _parsed_architecture(architecture_path)
    declared = document.get(CONCEPTS_SECTION) or {}
    concepts = declared.get(CONCEPTS_KEY) or []
    if not concepts:
        raise ArchitectureProjectionError(
            f"{architecture_path} declares no concepts under "
            f"{CONCEPTS_SECTION}.{CONCEPTS_KEY}."
        )

    rows = ArchitectureRows([], [], [])
    _walk(concepts, parent_key="", path=(), rows=rows)
    logger.info(
        "Read %d concepts, %d realizations and %d canonical objects from %s",
        len(rows.concepts), len(rows.realizations), len(rows.canonical_objects),
        architecture_path.name,
    )
    return rows


def _walk(
    concepts: list[Any],
    parent_key: str,
    path: tuple[str, ...],
    rows: ArchitectureRows,
) -> None:
    """Emit one level of the concept tree, then the levels beneath it.

    Declared order is display order, so ``sort_order`` is position among
    siblings and nothing is sorted.

    A concept that names nothing simply contributes no realization rows. The
    implementation container the tree draws between a concept and its code is a
    presentation decision and is not stored: it is derivable from whether a
    concept has realizations at all.

    Args:
        concepts: The concept declarations at this level.
        parent_key: The dotted key of the concept above, empty at the top.
        path: The keys of the concepts above, for the dotted key.
        rows: Accumulated rows, appended to.

    Raises:
        ArchitectureProjectionError: If a concept declares no key.
    """
    for ordinal, declared in enumerate(concepts):
        if not isinstance(declared, dict) or not str(declared.get("key") or ""):
            raise ArchitectureProjectionError(
                f"A concept under {'.'.join(path) or CONCEPTS_SECTION} declares no key."
            )
        here = (*path, str(declared["key"]))
        concept_key = ".".join(here)
        rows.concepts.append(
            {
                "concept_key": concept_key,
                # Empty, not None: the schema's own publication check compares
                # a staged parent against '' to mean "this is a domain".
                "parent_concept_key": parent_key,
                "label": str(declared.get("label") or here[-1]),
                # Empty, never None: code.concept.statement is NOT NULL, and a
                # concept with no authored prose is a real, storable state.
                "statement": str(declared.get("statement") or ""),
                "sort_order": ordinal,
            }
        )
        _walk(declared.get(CONCEPTS_KEY) or [], concept_key, here, rows)

        for position, reference in enumerate(declared.get("realized_by") or []):
            rows.realizations.append(
                {
                    "concept_key": concept_key,
                    # Authored verbatim. The schema resolves it; we do not.
                    "reference": str(reference),
                    "sort_order": position,
                }
            )
        for entry in declared.get("canonical") or []:
            rows.canonical_objects.append(_canonical_row(entry, concept_key))


#: What a ``canonical`` entry must state, and the staged column each becomes.
_CANONICAL_REQUIRED: tuple[tuple[str, str], ...] = (
    ("key", "object_key"),
    ("responsibility", "responsibility"),
    ("object", "implementation_reference"),
    ("authority", "authority"),
    ("public_seam", "public_seam"),
    ("must_not_bypass", "must_not_bypass"),
    ("evidence", "evidence"),
    ("status", "status"),
)


def _canonical_row(entry: Any, concept_key: str) -> dict[str, Any]:
    """One authored ``canonical`` entry as a staged row, verbatim.

    Nothing is resolved or judged here: which symbol ``object`` names, whether a
    status is valid and whether a responsibility already has an active owner are
    the schema's (``code.p_publish``). This only refuses an entry that is not
    there to stage -- a missing or non-text field.

    Args:
        entry: One item of a concept's ``canonical`` list.
        concept_key: The dotted key of the concept it is authored on.

    Returns:
        The row for ``code.canonical_object_stage``.

    Raises:
        ArchitectureProjectionError: If a required field is missing or is not text.
    """
    if not isinstance(entry, dict):
        raise ArchitectureProjectionError(
            f"A canonical entry under {concept_key} is not a mapping."
        )
    row: dict[str, Any] = {"concept_key": concept_key}
    for field, column in _CANONICAL_REQUIRED:
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ArchitectureProjectionError(
                f"Canonical entry {entry.get('key') or '?'} under {concept_key} "
                f"states no {field} as text."
            )
        row[column] = value
    for field, column in (("superseded_by", "superseded_by_key"), ("notes", "notes")):
        value = entry.get(field) or ""
        if not isinstance(value, str):
            raise ArchitectureProjectionError(
                f"Canonical entry {row['object_key']} under {concept_key} "
                f"states {field} as something other than text."
            )
        row[column] = value
    return row
