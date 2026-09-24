"""What some data IS: its structure, and what has been read of its values.

A profile DESCRIBES a data object; it is not owned by one. A file is one
thing that can be profiled and a database relation is another, so this sits
beside ``DataFile``, ``DataTransform`` and ``DataLoader`` rather than inside
any of them, and nothing acquires a ``profile()`` accessor.

    DataObject          the thing being described
       ^ described by
    DataProfile
    profiler            creates and enriches it
    profile store       looks it up and persists it
    consumers           use it instead of rediscovering structure

ONE REPRESENTATION, HOWEVER IT WAS OBTAINED. A profile may be read back from
stored state or derived again from the data object, and both produce this.
That is what later lets feed drift be a comparison of two profiles rather
than scattered checks against headers, field rows, hashes and statistics.

**Structure, and its readings, are one profile.** That is already how the
estate stores it -- one profile identity, with clear and redacted readings
written as field rows that "describe a single profiling event and differ only
in the values their fields carry". So this is the whole profile from the
start, rather than a structure-only object something would later have to
wrap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["DataProfile", "FieldProfile", "ProfileField", "profile_for"]


@dataclass(frozen=True)
class ProfileField:
    """One field's structural definition: what it is called and where it is.

    ``start`` and ``end`` are for formats whose fields are POSITIONS rather
    than names -- fixed width is the case in this estate. They are a RANGE,
    matching the vocabulary fixed-width handling already uses, rather than a
    start and a width: two ways of saying the same thing invite a conversion
    that disagrees about whether the end is inclusive.

    Absent for formats where position is not how a field is found.
    """

    name: str
    ordinal: int
    start: int | None = None
    end: int | None = None


@dataclass(frozen=True)
class FieldProfile:
    """One reading of one field's values.

    Bound to its field by ``name`` and ``ordinal`` rather than by position in
    a parallel tuple -- a positional convention would break silently the
    first time a reading covered a subset of the fields.

    Statistics -- detected type, lengths, decimal characteristics, counts --
    arrive with the work that populates them from stored profiles. This
    carries identity only, because nothing produces the rest yet.
    """

    name: str
    ordinal: int


@dataclass(frozen=True)
class DataProfile:
    """The whole profile of one data object.

    Attributes:
        fields: The structural definition, in ordinal order.
        structure_complete: **This profile contains the complete structural
            definition.** Not "the format could describe one" -- what THIS
            instance carries.
        structure_validated: **The data has been checked against that
            definition.** Not "the source has a routine that could check it".
            A routine that did not run is not evidence.
        clear: Readings of the values as they are.
        redacted: Readings of the values after redaction.

    THE TWO FLAGS ARE DIFFERENT QUESTIONS, and conflating them is the failure
    this shape exists to prevent:

        can I describe the shape?                  structure_complete
        can I rely on it instead of revalidating?  structure_validated

    A header declares the intended columns; it does not prove a later row has
    that width. So a consumer deciding whether it may SKIP reading the data
    asks ``structure_validated`` -- asking the other one would trust exactly
    the formats whose records are least checked.

    THEY DESCRIBE EVIDENCE, NOT CONTENT. Two profiles can describe identical
    data and differ on both flags because they were established differently.
    So drift is a comparison over ``fields`` and the readings, and is
    deliberately NOT dataclass equality, which includes the flags. The
    projection that expresses that belongs with drift detection; what matters
    here is not implying ``__eq__`` already means it.
    """

    fields: tuple[ProfileField, ...] = ()
    structure_complete: bool = False
    structure_validated: bool = False
    clear: tuple[FieldProfile, ...] = ()
    redacted: tuple[FieldProfile, ...] = ()


def profile_for(source: Any) -> DataProfile:
    """Resolve the profile that describes ``source``.

    THE RESOLUTION BOUNDARY, and the only entry point. Consumers ask this
    rather than calling a source's structural methods or reading profile
    tables, so that when a stored profile becomes reachable they change
    nothing.

    WHAT IT DOES TODAY is structural discovery from the source's own
    primitives, which has one consequence worth stating plainly: **every
    profile it returns has ``structure_validated=False``.** Nothing here
    reads the data to check it against the structure, so nothing here can
    claim it was checked -- and no profile this produces licenses a consumer
    to skip validating the source itself.

    ``structure_complete`` follows ``declares_structure``: a header, or
    columns a caller named, IS the complete definition. A structure inferred
    from the first record is not, because the first proves nothing about the
    second.

    Args:
        source: The data object being described. Only its structural
            primitives are used; it is never asked to own a profile.

    Returns:
        The profile. An empty one for a source with no fields, which is an
        answer rather than a failure.
    """
    declared = bool(getattr(source, "declares_structure", False))
    names = list(source.source_structure())
    return DataProfile(
        fields=tuple(
            ProfileField(name=str(name), ordinal=ordinal)
            for ordinal, name in enumerate(names, start=1)
        ),
        structure_complete=declared and bool(names),
        # Never true from here. See above.
        structure_validated=False,
    )
