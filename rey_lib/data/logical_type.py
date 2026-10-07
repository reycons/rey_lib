"""What a column's data MEANS, said in Rey's own neutral vocabulary (backlog 690).

    LogicalType -> DatabaseType -> the destination's physical type

**A VALUE, NOTHING MORE.** A kind and the bounds that matter for it. It renders
nothing and knows no provider: how a destination spells a type is
``rey_lib.db.database_type.DatabaseType``'s, and only ``DatabaseTable`` asks it.
Shared, load, data and profiling code describe columns with this and never with
a physical type name.

**NOT what a database reports.** ``DatabaseColumn.type`` is the physical type an
existing column has, as its database answers; this is what Rey wants a new or
changed column to mean. The two are not collapsed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from rey_lib.errors.error_utils import ConfigError

__all__ = ["LOGICAL_KINDS", "LogicalType"]


#: The kinds a column may mean. ``time`` is here because the ``time`` transform
#: produces it; ``boolean`` arrived with backlog 386.
LOGICAL_KINDS: tuple[str, ...] = (
    "text", "integer", "decimal", "date", "datetime", "time", "boolean",
)


@dataclass(frozen=True)
class LogicalType:
    """One column's meaning: its kind, and the bounds that kind takes.

    Attributes:
        kind: One of :data:`LOGICAL_KINDS`.
        length: For ``text``, the most characters it holds; None is unbounded.
        precision: For ``decimal``, the total digits.
        scale: For ``decimal``, the digits after the point.
    """

    kind: str
    length: Optional[int] = None
    precision: Optional[int] = None
    scale: Optional[int] = None

    def __post_init__(self) -> None:
        """Refuse a kind Rey does not have.

        Raises:
            ConfigError: If ``kind`` is not one of :data:`LOGICAL_KINDS`.
        """
        if self.kind not in LOGICAL_KINDS:
            raise ConfigError(
                f"LogicalType: no kind is called '{self.kind}'. "
                f"Kinds: {', '.join(LOGICAL_KINDS)}."
            )
