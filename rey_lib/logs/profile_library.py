"""Canonical retrieval and presentation of governed file profiles.

A profile is a mutation. Profiling reads a file as one prior mutation left it
and appends a ``source_file_profile`` record naming that mutation as the record
it consumed, so profiles are read from where mutations are read and this module
holds no storage of its own.

It answers two questions and no others: which profile is current for a consumed
mutation, and which of its two representations a caller asked for. Whether a
particular consumer may receive a representation is asked one layer up --
``profile_access.allowed`` and ``.default`` are enforced by the AI runtime
through ``AI.permitted_access``, which consumed that policy at construction. An
operator reading both representations in the Feeds tree is not subject to that
policy and does not consult it.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "PROFILE_ACCESS_REDACTED",
    "PROFILE_ACCESS_UNREDACTED",
    "PROFILE_RECORD_TYPE",
    "ProfileLibraryError",
    "lookup_profile_record",
    "read_profile_records",
    "resolve_profile_presentation",
]

#: The mutation record type profiling appends.
PROFILE_RECORD_TYPE = "source_file_profile"

PROFILE_ACCESS_REDACTED = "redacted"
PROFILE_ACCESS_UNREDACTED = "unredacted"

# The clear and redacted readings are separate columns on the mutation, so
# selecting one is reading a column. Nothing is copied and stripped: a caller
# handed the redacted representation has no path back to the clear values
# because it was never given a record that held them.
_ACCESS_PROFILE_COLUMNS = {
    PROFILE_ACCESS_REDACTED: "redacted_profile",
    PROFILE_ACCESS_UNREDACTED: "clear_profile",
}


class ProfileLibraryError(Exception):
    """Raised when a governed profile cannot be resolved."""


def _control(ctx: Any) -> Any:
    """The Control this installation reaches its governed mutations through."""
    control = getattr(ctx, "shared_control", None)
    if control is None:
        from rey_lib.control import Control

        control = Control(ctx)
    return control


def read_profile_records(ctx: Any) -> list[dict[str, Any]]:
    """Return every governed profiling mutation, in the order they were written.

    The installation's mutations narrowed to the profiling ones. Nothing here
    decides what a profile means, only which rows are profiles.
    """
    try:
        rows = _control(ctx).list_file_mutations(required=False)
    except Exception as exc:
        raise ProfileLibraryError(
            f"Governed profiles could not be read: {exc}"
        ) from exc
    return [
        dict(row) for row in rows
        if str(row.get("record_type") or "") == PROFILE_RECORD_TYPE
    ]


def lookup_profile_record(
    ctx: Any,
    source_record_id: Any,
    source_hash: str,
) -> dict[str, Any]:
    """Return available, missing, or stale state for one consumed mutation.

    ``source_record_id`` is the mutation whose profile is wanted -- the record
    the profiling step consumed, which is what a profiling mutation names. It is
    never a ``file_manifest_id``: a file has many mutations and knowing the file
    does not say which of them was profiled.

    Re-profiling appends another mutation rather than replacing one, so the last
    profile of that record is the current reading. It is stale when the file has
    changed underneath it, which is the recorded ``source_hash`` differing from
    the one the caller measured.
    """
    key = _consumed_record_id(source_record_id)
    current_hash = _required_text(source_hash, "source_hash")
    matches = [
        record for record in read_profile_records(ctx)
        if _consumed_record_id(record.get("source_record_id"), required=False) == key
    ]
    if not matches:
        return {"status": "profile_missing", "source_record_id": key, "record": None}
    record = matches[-1]
    if _recorded_source_hash(record) != current_hash:
        return {"status": "profile_stale", "source_record_id": key, "record": None}
    return {"status": "profile_available", "source_record_id": key, "record": record}


def resolve_profile_presentation(
    record: Mapping[str, Any],
    access: str,
    *,
    source_record_id: Any = "",
) -> dict[str, Any]:
    """Return the one representation named by ``access``.

    Parameters
    ----------
    record : Mapping[str, Any]
        One governed profiling mutation. Never modified.
    access : str
        ``redacted`` or ``unredacted``.
    source_record_id : Any
        Identity used in failure messages only.

    Raises
    ------
    ProfileLibraryError
        If ``access`` names no known representation, or the mutation does not
        carry the one requested.
    """
    selected = str(access or "").strip()
    if selected not in _ACCESS_PROFILE_COLUMNS:
        raise ProfileLibraryError(
            "profile access may be only redacted or unredacted."
        )
    if not isinstance(record, Mapping):
        raise ProfileLibraryError("A governed profile must be a mapping.")
    column = _ACCESS_PROFILE_COLUMNS[selected]
    representation = record.get(column)
    if not isinstance(representation, Mapping):
        raise ProfileLibraryError(
            f"Governed profile for consumed record '{source_record_id}' carries "
            f"no {column}."
        )
    return dict(representation)


def _recorded_source_hash(record: Mapping[str, Any]) -> str:
    """The hash of the file as profiled, which both representations carry."""
    for column in ("clear_profile", "redacted_profile"):
        section = record.get(column)
        if isinstance(section, Mapping):
            recorded = section.get("source_hash")
            if isinstance(recorded, str) and recorded.strip():
                return recorded.strip()
    raise ProfileLibraryError(
        "Governed profile records no source hash, so whether it is current "
        "cannot be answered."
    )


def _consumed_record_id(value: Any, *, required: bool = True) -> int:
    """One consumed-mutation identity, as the positive integer it always is."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        if not required:
            return 0
        raise ProfileLibraryError(
            "A consumed record is identified by a positive file_mutation_id; "
            f"{value!r} is not one."
        )
    return value


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileLibraryError(f"{field} must be a non-empty string.")
    return value.strip()
