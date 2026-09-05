"""
Deterministic per-column redaction registry.

Maps original values to characteristic-preserving replacements within a
named column namespace.  The same original value always produces the same
replacement within a registry instance.  Different columns use independent
counters so replacements never collide across columns.

A column named for redaction must be obscured. That is the whole contract, and
it does not depend on profiling recognising the data: a proposed mask type is
honoured only when the masking vocabulary actually holds it, and anything else
falls back to the characteristic-preserving replacement rather than to the
original value.

Public API
----------
RedactionRegistry   Stateful registry mapping originals to replacements.
RedactionExhausted  Raised when no replacement can satisfy the contract.
"""

from __future__ import annotations

from typing import Optional

from rey_lib.errors.error_utils import AppError
from rey_lib.logs import get_logger
from rey_lib.redaction.char_utils import analyze_pattern, generate_replacement
from rey_lib.redaction.masks import KNOWN_MASKS, apply_mask

__all__ = ["RedactionExhausted", "RedactionRegistry"]

_logger = get_logger(__name__)

#: How many successive counters the search will try before giving up. The
#: encoding is periodic in the counter with period 10 for a single slot, so ten
#: consecutive counters enumerate every output that slot can produce; the bound
#: is that period with room to spare, not a tuning constant.
_MAX_ATTEMPTS: int = 32


class RedactionExhausted(AppError):
    """Raised when no replacement can satisfy the redaction contract.

    The search is an online allocator: it assigns as each value arrives and
    cannot revise a replacement it has already handed out. It therefore does
    **not** guarantee full use of the finite replacement space -- it can strand,
    where candidates remain in principle but the only unused one is equal to the
    value now being redacted. Exhaustion means *no currently available non-self
    unique encoding remains*, which is not the same as the namespace having
    filled the space.

    Aliasing two different sources onto one replacement, or handing back the
    source itself, would both be silent information leaks. Failing is the only
    other option, so this is raised rather than either.
    """


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
    'AAAAB'
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
        self.mask_type: str | None     = _resolve_mask(name, mask_type)
        self._map:      dict[str, str] = {}
        # Every replacement handed out for this column, so a second source can
        # never be given one that is already spoken for.
        self._assigned: set[str]       = set()

    def get_or_create(self, value: str) -> str:
        """Return existing replacement or generate and store a new one."""
        if value in self._map:
            return self._map[value]

        if self.mask_type:
            self.count += 1
            replacement = apply_mask(self.mask_type, value, self.count)
        else:
            replacement = self._generic(value)

        if len(replacement) != len(value):
            _logger.warning(
                "Replacement length mismatch for column '%s': "
                "original=%d replacement=%d value=%r",
                self.name, len(value), len(replacement), value,
            )

        self._map[value] = replacement
        self._assigned.add(replacement)
        return replacement

    def _generic(self, value: str) -> str:
        """Return a replacement that is neither the value nor already taken.

        Successive counters are consumed rather than reused, so every distinct
        source keeps its own counter and two sources cannot land on one
        replacement. A candidate is rejected when it equals the source -- the
        encoding is right-aligned and pad-filled, so a value that happens to be
        the encoding of its own counter would otherwise survive verbatim
        (``1``, ``B`` and ``AB`` all do this at counter 1) -- or when it has
        already been handed out.

        Raises:
            RedactionExhausted: When no candidate in range satisfies both.
        """
        pattern = analyze_pattern(value)
        for _ in range(_MAX_ATTEMPTS):
            self.count += 1
            candidate = _generate(pattern, self.count)
            if candidate != value and candidate not in self._assigned:
                return candidate

        raise RedactionExhausted(
            f"Column '{self.name}': no replacement left that differs from the "
            f"value and is not already assigned "
            f"(width={len(value)}, assigned={len(self._assigned)}). "
            "Refusing to hand back the source or to reuse a replacement."
        )


def _resolve_mask(column: str, mask_type: str | None) -> str | None:
    """Return ``mask_type`` when the masking vocabulary holds it, else ``None``.

    A proposal comes from profiling, whose vocabulary is its own: it answers
    *what kind of data is this*, and its answers include ``alpha``,
    ``alphanumeric``, ``boolean`` and ``blank``, none of which name a mask. The
    two vocabularies are not required to agree, and no mapping between them is
    kept -- that would make every future profiling datatype a masking
    obligation.

    An unresolved proposal means generic redaction, which is what ``None``
    already selects. It never means no redaction.
    """
    if mask_type is None or mask_type in KNOWN_MASKS:
        return mask_type

    _logger.warning(
        "Column '%s': proposed mask type '%s' is not a mask -- using generic "
        "redaction. Detection proposes; masking decides.",
        column, mask_type,
    )
    return None


def _generate(pattern: list[tuple[str, str]], counter: int) -> str:
    """Return the counter's replacement for ``pattern``.

    This is :func:`generate_replacement`, with one exception.

    A pattern of separators alone has no alphanumeric position to vary, so
    ``generate_replacement`` reproduces it verbatim and can never differ from
    its source. There, and only there, character class and separator identity
    are given up so that the value can be obscured at all; **width is preserved
    on every path** and is never traded. Every other pattern keeps its
    separators exactly where they were.

    The replacement emitted for that case is alphanumeric, so it differs from a
    separator-only source by construction. That guarantee rests on this path
    being reached only when ``analyze_pattern`` classified no character as a
    digit or a letter -- if that classification ever changes, the guarantee
    needs rechecking, and a test says so by name.
    """
    if any(cls != "S" for cls, _ in pattern):
        return generate_replacement(pattern, counter)

    return generate_replacement([("U", "A")] * len(pattern), counter)
