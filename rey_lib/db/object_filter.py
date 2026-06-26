"""
Object selection filter for DDL export.

Implements P1 of SGC_Postgres_DDL_Comment_Enrichment_Prerequisites: select which
already-enumerated database objects to export, by schema / object type / object
name, with optional exclude rules and object-scoped ``force``.

Pure and dependency-free — no I/O and no database access. Operates on the
normalised object dicts produced by the exporter (each has ``schema``,
``object_type``, ``name``, ``key``). When no filter is supplied the exporter
keeps its current whole-database behaviour; this module is only invoked when a
filter is explicitly configured.

Public API
----------
validate_object_filter   Return config errors for an object_filter (empty=valid).
apply_object_filter      Return the subset of objects matching the filter.
"""

from __future__ import annotations

from fnmatch import fnmatch
from typing import Any

__all__ = ["validate_object_filter", "apply_object_filter"]

# Programmable object types this SGC applies to.
_VALID_OBJECT_TYPES = frozenset({"procedure", "function", "view", "trigger"})

# A rule field value meaning "match any".
_ANY = (None, "", "*")


def validate_object_filter(cfg: Any) -> list[str]:
    """Return validation errors for an ``object_filter`` config (empty = valid).

    Parameters
    ----------
    cfg : Any
        The ``object_filter`` mapping (``include`` / optional ``exclude``).

    Returns
    -------
    list[str]
        Human-readable errors; empty when valid.
    """
    if not isinstance(cfg, dict):
        return ["object_filter must be a mapping with an 'include' list."]

    errors: list[str] = []
    include = cfg.get("include")
    if not isinstance(include, list) or not include:
        errors.append("object_filter.include must list at least one rule.")

    for section in ("include", "exclude"):
        rules = cfg.get(section)
        if rules is None:
            continue
        if not isinstance(rules, list):
            errors.append(f"object_filter.{section} must be a list of rules.")
            continue
        for index, rule in enumerate(rules):
            label = f"object_filter.{section}[{index}]"
            if not isinstance(rule, dict):
                errors.append(f"{label} must be a mapping.")
                continue
            obj_type = rule.get("object_type")
            if obj_type not in _ANY and str(obj_type) not in _VALID_OBJECT_TYPES:
                errors.append(
                    f"{label}: invalid object_type '{obj_type}' "
                    f"(allowed: {sorted(_VALID_OBJECT_TYPES)})."
                )
            if rule.get("schema") in _ANY and obj_type in _ANY and rule.get("object_name") in _ANY:
                errors.append(
                    f"{label}: a rule must specify at least one of "
                    "schema, object_type, or object_name."
                )
    return errors


def apply_object_filter(
    objects: list[dict[str, Any]],
    cfg: dict[str, Any],
    *,
    default_force: bool = False,
) -> list[dict[str, Any]]:
    """Return the subset of ``objects`` selected by the filter.

    An object is kept when it matches at least one ``include`` rule and no
    ``exclude`` rule. Each kept object gains a ``force`` flag (the matching
    include rule's ``force``, else ``default_force``) for downstream use; this
    metadata does not affect export itself.

    Parameters
    ----------
    objects : list[dict[str, Any]]
        Normalised objects (each with ``schema``, ``object_type``, ``name``).
    cfg : dict[str, Any]
        The ``object_filter`` mapping (``include`` / optional ``exclude``).
    default_force : bool
        Global force default applied when an include rule omits ``force``.

    Returns
    -------
    list[dict[str, Any]]
        Selected objects, in their original order.
    """
    include = cfg.get("include") or []
    exclude = cfg.get("exclude") or []

    kept: list[dict[str, Any]] = []
    for obj in objects:
        matched_rule = next((r for r in include if _rule_matches(obj, r)), None)
        if matched_rule is None:
            continue
        if any(_rule_matches(obj, r) for r in exclude):
            continue
        selected = dict(obj)
        selected["force"] = bool(matched_rule.get("force", default_force))
        kept.append(selected)
    return kept


def _rule_matches(obj: dict[str, Any], rule: dict[str, Any]) -> bool:
    """Return True when ``obj`` matches a single include/exclude rule."""
    schema = rule.get("schema")
    if schema not in _ANY and str(schema) != str(obj.get("schema", "")):
        return False
    obj_type = rule.get("object_type")
    if obj_type not in _ANY and str(obj_type) != str(obj.get("object_type", "")):
        return False
    name_pattern = rule.get("object_name")
    pattern = "*" if name_pattern in _ANY else str(name_pattern)
    return fnmatch(str(obj.get("name", "")), pattern)
