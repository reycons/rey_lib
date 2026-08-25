"""What identifies one governed file, defined once.

    A governed file has one identity -- control.file_manifest.file_manifest_id
    -- which application code may call file_id; no separately minted UUID
    exists and no file_id column is ever added.
        -- 01_core_architecture.yaml

The database mints it. Recording the file is what gives it identity, the same
way a run's identity is the row that records it.

Why this module exists
----------------------
Fourteen places used to decide for themselves what a file_id was, each carrying
the retired UUID's assumption that it is a non-empty string. Fixing one exposed
the next: the classification candidate, then its lifecycle link, then the
governed reference, then the routing result -- one defect written fourteen
times. An identity that is an integer in one layer and a string in the next is
not a type mismatch, it is two identities.

So it is described here and imported. A producer or consumer that needs to know
what a governed file is called asks this module; nothing re-derives it.
"""

from __future__ import annotations

from typing import Any

__all__ = ["FileId", "governed_file_id", "is_governed_file_id"]

#: The identity of one governed file: control.file_manifest.file_manifest_id.
#:
#: A plain int rather than a NewType, because it *is* the database's value and
#: crosses the JSON boundary as one. The name is here so a reader of a
#: signature learns which integer this is.
FileId = int


def is_governed_file_id(value: Any) -> bool:
    """Whether this value identifies a governed file.

    A positive integer. ``bool`` is excluded because it is an int in Python and
    ``True`` is not a file.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def governed_file_id(value: Any, *, subject: str = "") -> FileId:
    """Return the governed file identity, or refuse with one message.

    Parameters
    ----------
    value : Any
        The candidate identity, as it arrived.
    subject : str
        What is being identified, for the refusal -- "a classification record",
        "a mutation context". Optional: the message is complete without it.

    Raises
    ------
    ValueError
        When the value does not identify a governed file. A string is called
        out by name because that is the retired shape, and a reader who sees
        one is looking at code that predates the single identity.
    """
    if is_governed_file_id(value):
        return int(value)

    about = f"{subject} " if subject else ""
    if isinstance(value, str):
        raise ValueError(
            f"{about}was given the file id {value!r} as a string. A governed "
            "file is identified by control.file_manifest.file_manifest_id, "
            "which the database mints; the string identity was retired."
        )
    raise ValueError(
        f"{about}requires a governed file id -- a positive "
        f"file_manifest_id -- and was given {value!r}."
    )
