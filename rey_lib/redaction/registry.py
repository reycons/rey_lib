"""
Deterministic per-column redaction registry.

Maps original values to characteristic-preserving replacements within a
named column namespace.  The same original value always produces the same
replacement within a registry instance.  Different columns use independent
counters so replacements never collide across columns.

Public API
----------
RedactionRegistry   Stateful registry mapping originals to replacements.
"""

from __future__ import annotations

from typing import Optional

from rey_lib.logs import get_logger
from rey_lib.redaction.char_utils import analyze_pattern, generate_replacement
from rey_lib.redaction.masks import apply_mask

__all__ = ["RedactionRegistry"]

_logger = get_logger(__name__)


class RedactionRegistry:
    """Stateful, per-column deterministic redaction registry.

    Each column namespace holds its own mapping and counter so that
    ``ACCOUNT_NUMBER`` counter 1 and ``MASTER_ACCOUNT`` counter 1 produce
    distinct but structurally consistent replacements.

    Parameters
    ----------
    columns : list[str]
        Column names that will be redacted.  A namespace is created for each.

    Examples
    --------
    >>> reg = RedactionRegistry(["ACCOUNT", "NAME"])
    >>> reg.redact("ACCOUNT", "12345")
    '00001'
    >>> reg.redact("ACCOUNT", "98765")
    '00002'
    >>> reg.redact("ACCOUNT", "12345")   # same → same
    '00001'
    >>> reg.redact("NAME", "SMITH")
    'BAAAA'
    """

    def __init__(
        self,
        columns:    list[str],
        mask_types: dict[str, str] | None = None,
    ) -> None:
        """Initialise a namespace for each column.

        Parameters
        ----------
        columns : list[str]
            Column names that will be redacted.
        mask_types : dict[str, str] | None
            Optional mapping of column name → mask type string.  When a
            column has a mask type its values are replaced using the
            type-aware mask function instead of the default
            characteristic-preserving replacement.
        """
        mt = mask_types or {}
        self._namespaces: dict[str, _Namespace] = {
            col: _Namespace(col, mt.get(col)) for col in columns
        }

    def redact(self, column: str, value: str) -> str:
        """Return the replacement for ``value`` in ``column``.

        Blank values are returned unchanged.  Unknown columns are passed
        through with a warning rather than raising.

        Parameters
        ----------
        column : str
            Column name — must match one of the names passed to ``__init__``.
        value : str
            Original field value.

        Returns
        -------
        str
            Characteristic-preserving replacement, or original if blank /
            column not registered.
        """
        if not value or not value.strip():
            return value

        ns = self._namespaces.get(column)
        if ns is None:
            _logger.warning("RedactionRegistry: unknown column '%s' — passing through.", column)
            return value

        return ns.get_or_create(value)

    def summary(self) -> dict[str, int]:
        """Return a mapping of column → number of unique values redacted."""
        return {col: ns.count for col, ns in self._namespaces.items()}


# ---------------------------------------------------------------------------
# Private — per-column namespace
# ---------------------------------------------------------------------------

class _Namespace:
    """Holds the value→replacement map and counter for one column."""

    def __init__(self, name: str, mask_type: str | None = None) -> None:
        self.name:      str            = name
        self.count:     int            = 0
        self.mask_type: str | None     = mask_type
        self._map:      dict[str, str] = {}

    def get_or_create(self, value: str) -> str:
        """Return existing replacement or generate and store a new one."""
        if value in self._map:
            return self._map[value]

        self.count += 1

        if self.mask_type:
            replacement = apply_mask(self.mask_type, value, self.count)
        else:
            pattern     = analyze_pattern(value)
            replacement = generate_replacement(pattern, self.count)

            if len(replacement) != len(value):
                _logger.warning(
                    "Replacement length mismatch for column '%s': "
                    "original=%d replacement=%d value=%r",
                    self.name, len(value), len(replacement), value,
                )

        self._map[value] = replacement
        return replacement
