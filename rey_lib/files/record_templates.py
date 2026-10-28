"""Destination templates resolved from a record's own values.

A configured destination may carry ``<dotted.field>`` placeholders, each naming
a field of the record the destination is being resolved for. This module looks
those fields up and renders the path.

It names no field itself. Every field path comes from the configured template,
so installation vocabulary -- whatever a particular installation calls its
feeds, clients, record types or dates -- stays in YAML and never appears here.
A template referencing fields this module has never heard of resolves exactly
as well as one referencing familiar ones.

It resolves and reports. It creates nothing, writes nothing, and does not touch
the filesystem, so a caller can resolve a destination and then decide whether to
use it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "MISSING",
    "TemplateResolution",
    "record_field",
    "resolve_record_template",
]

_TEMPLATE_FIELD = re.compile(r"<([^<>]*)>")

#: Returned when an address names no field. Public because a caller testing for
#: a missing field must compare against this exact object: a second sentinel
#: that looks the same never matches, and the check silently passes.
MISSING = object()


@dataclass(frozen=True)
class TemplateResolution:
    """A resolved destination, or the field that prevented resolution."""

    path: Path | None
    missing_field: str | None

    @property
    def resolved(self) -> bool:
        """Whether every referenced field was present and usable."""
        return self.path is not None


def record_field(record: Mapping[str, Any], field: str) -> Any:
    """Return one exact dotted field value, or a missing sentinel.

    Exact rather than lenient: an absent field is missing, not empty, so a
    caller can tell a template referencing the wrong field from one referencing
    a field that happens to be blank.
    """
    current: Any = record
    for part in field.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return MISSING
        current = current[part]
    return current


def resolve_record_template(
    template: str,
    record: Mapping[str, Any],
) -> TemplateResolution:
    """Resolve every ``<field>`` in ``template`` from ``record``.

    Parameters
    ----------
    template : str
        A destination carrying zero or more ``<dotted.field>`` placeholders.
        Installation tokens such as ``{data}`` are already resolved by the time
        a template reaches here; only record placeholders remain.
    record : Mapping[str, Any]
        The record whose values the placeholders name.

    Returns
    -------
    TemplateResolution
        The expanded path, or the name of the first field that was absent,
        not a string, or blank. The field is reported by its actual name so a
        configuration error says which placeholder to fix rather than that
        something was missing.
    """
    expanded = template
    for field in _TEMPLATE_FIELD.findall(template):
        value = record_field(record, field)
        if value is MISSING or not isinstance(value, str) or not value.strip():
            return TemplateResolution(path=None, missing_field=field)
        expanded = expanded.replace(f"<{field}>", value.strip())
    return TemplateResolution(
        path=Path(expanded).expanduser().resolve(), missing_field=None
    )
