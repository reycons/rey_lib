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

    **EVERY STATISTIC DEFAULTS TO ``None``, AND ``None`` IS NOT ZERO.**
    ``blank_count=0`` is a measurement that found no blanks;
    ``blank_count=None`` is "not measured". Conflating them would let a
    profile that measured nothing claim a clean column, and it is why the
    store's columns are nullable too.

    A profiler fills what it measured and leaves the rest alone. A reading
    that carries identity only -- which is all this could express before --
    is still a legitimate reading.

    Attributes:
        name: Which field this reads.
        ordinal: Its position, 1-based, matching its ``ProfileField``.
        detected_type: The type the values were found to be, in the
            profiler's vocabulary.
        blank_count: How many values were blank.
        min_length: The shortest non-blank value.
        max_length: The longest non-blank value.
        min_decimal_places: Fewest decimal places seen.
        max_decimal_places: Most decimal places seen.
        min_numeric: Smallest value, where the field is numeric.
        max_numeric: Largest value, where the field is numeric.
        min_date: Earliest value, as the text the source carried. Text
            rather than a date, because a profile records what was THERE --
            parsing it here would discard the format that is itself a fact.
        max_date: Latest, on the same terms.
        sample_values: Representative values, as the profiler ranked them.
        null_like_values: Values found to stand in for absence.
        constant_value: The single value, where the field never varies.
    """

    name: str
    ordinal: int
    detected_type: str | None = None
    blank_count: int | None = None
    min_length: int | None = None
    max_length: int | None = None
    min_decimal_places: int | None = None
    max_decimal_places: int | None = None
    min_numeric: float | None = None
    max_numeric: float | None = None
    min_date: str | None = None
    max_date: str | None = None
    sample_values: Any = None
    null_like_values: Any = None
    constant_value: str | None = None


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
        header_definition: The structure as the source STATED it -- the
            header line the file actually carried, as text. Empty where the
            source declares nothing.
        row_count: How many rows this profiling event saw. An attribute of
            the delivery, not of the structure: two deliveries of one layout
            differ here and are still the same shape.
        distribution: Dataset-level facts the profiler measured, carried
            opaquely. Opaque ON PURPOSE -- what a profiler chooses to count
            is its own, and naming each one here would make this object grow
            a field every time a profiler learned to measure something.

    **FIELD COUNT IS NOT A FIELD.** It is ``len(fields)``. Storing it beside
    them would create two answers that can disagree, and the one that
    disagreed would be the stored one.

    **IDENTITY IS NOT HERE EITHER** -- no profile key, no file, no
    installation, no run. This says what data IS; which stored profile it is
    and what it was read from are the lifecycle's, and a description that
    carried its own identity could not describe a source that has none.

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
    header_definition: str = ""
    row_count: int | None = None
    distribution: Any = None


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
