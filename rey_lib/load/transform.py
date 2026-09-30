"""The Transform: the canonical Loader-facing object for what is done to a load's records.

    Source -> Transform -> Target

**ONE OBJECT, EVERY ENTRY POINT**, as with ``Source``. The Console, a workflow
step, the CLI, a Runner-launched process and scheduled work construct this same
class and resolve through it. Canonical is the class and its contract, not an
instance: no live object crosses a process boundary.

**It owns CONFIGURATION, not execution.** It holds which kinds of transform a
load can have, what has been said about each, and which one is selected. What
applies the rules is still the existing resolved object -- ``ColumnTransform``
or ``IdentityTransform`` -- built by ``_build_transform`` when something
executes, which is also where secrets are resolved. Nothing here holds a
transform object, a context or a secret.

    identity     (nothing)                     -> IdentityTransform
    declaration  transform                     -> ColumnTransform
    yaml         transform-file                -> ColumnTransform
    manifest     declaration, persistence      -> ColumnTransform

**MANIFEST AND YAML ARE CONFIGURATION ORIGINS, not implementations.** A stored
definition fills ``declaration`` and ``persistence`` -- ManifestSource is one way
they arrive, a hand configuration is another -- and every kind ends in the same
builder. ``persistence`` is ``TransformPersistence`` held as data: optional for
execution, and what a later write back to the database keys on.

**IT AUTHORS ITS OWN DECLARATION.** A column mapping is edited here -- one
field of one entry, a column added or moved -- and never by whatever draws it.
Every edit first COMPLETES the declaration against the source's columns, which
this instance is told (``observe_source_columns``) as a fact of its context:
never a field, never in the declarative form, never persisted.

**TWO VOCABULARIES.** ``TRANSFORM_FIELDS`` is everything this object holds.
``TRANSFORM_PARAMETERS`` is the subset the loader declares as parameters --
the names a CLI or a panel types. ``declaration`` and ``persistence`` are
fields of this object and not loader parameters.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from rey_lib.config.config_loader import parse_yaml
from rey_lib.data.column_transform import TransformPersistence, authorable_starters
from rey_lib.errors.error_utils import ConfigError
from rey_lib.files.file_utils import read_text_file
from rey_lib.load import load_operation

__all__ = [
    "COLUMN_FIELDS", "Transform", "TRANSFORM_FIELDS", "TRANSFORM_KINDS", "TRANSFORM_PARAMETERS",
]


@dataclass(frozen=True)
class _Kind:
    """One kind of transform: the fields that are its own, and which it needs."""

    id: str
    fields: tuple[str, ...]
    required: tuple[str, ...]


#: The kinds a Transform can be, in the order a reader is offered them.
TRANSFORM_KINDS: tuple[_Kind, ...] = (
    _Kind("identity", (), ()),
    _Kind("declaration", ("transform",), ("transform",)),
    _Kind("yaml", ("transform-file",), ("transform-file",)),
    # `persistence` is never required to EXECUTE: a declaration defines the
    # transform, and the identities only say where it can be written back.
    _Kind("manifest", ("declaration", "persistence"), ("declaration",)),
)

#: Every field a Transform holds, across its kinds.
TRANSFORM_FIELDS: tuple[str, ...] = tuple(dict.fromkeys(
    name for kind in TRANSFORM_KINDS for name in kind.fields
))

#: The fields the loader declares as parameters -- the externally typed subset.
TRANSFORM_PARAMETERS: tuple[str, ...] = ("transform", "transform-file")

_BY_ID: dict[str, _Kind] = {kind.id: kind for kind in TRANSFORM_KINDS}

#: The one field of one entry an edit may change, as ``edit_column`` names it.
COLUMN_FIELDS: tuple[str, ...] = ("source", "name", "datatype", "export", "type", "transform")

#: Where each authorable kind keeps its declaration.
_AUTHORED_IN: dict[str, str] = {"declaration": "transform", "manifest": "declaration"}

#: The persisted facts held per entry, beside the declaration and never in it.
_ALIGNED: tuple[str, ...] = ("column_ids", "column_ordinals")


def _held(value: Any) -> bool:
    """Whether a value says anything. None, empty text and an empty mapping do not."""
    if value is None:
        return False
    if isinstance(value, Mapping):
        return bool(value)
    return str(value).strip() != ""


class Transform:
    """What is done to a load's records: its configurations, and the one selected.

    Built directly, or from a declaration with :meth:`from_declaration`, which is
    how every entry point constructs one.
    """

    def __init__(
        self,
        values: Optional[Mapping[str, Any]] = None,
        selected: Optional[str] = None,
    ) -> None:
        """Hold what has been said about each kind, and which one is chosen.

        Args:
            values: Field values, across every kind. Switching kinds must not
                lose what was said under another, but only a Transform field
                may be given.
            selected: The chosen kind, or None to let the values decide.

        Raises:
            ValueError: If a value names a field no kind has, or ``selected``
                names no kind.
        """
        self._values: dict[str, Any] = {}
        self._selected: Optional[str] = None
        # CONTEXT, NOT CONFIGURATION: the columns the source was last observed
        # to carry. Never in `declaration()`, never persisted.
        self._source_columns: tuple[str, ...] = ()
        # EPHEMERAL OUTPUT, not configuration: the last preview produced through
        # this transform. Never in `declaration()`, never persisted or run.
        self._preview: Optional[dict[str, Any]] = None
        for name, value in (values or {}).items():
            self.update(name, value)
        if selected is not None:
            self.select(selected)

    # -- construction --------------------------------------------------------

    @classmethod
    def from_declaration(cls, declaration: Mapping[str, Any]) -> "Transform":
        """Build a Transform from its declarative form -- the inverse of :meth:`declaration`."""
        return cls(
            values=dict(declaration.get("values") or {}),
            selected=declaration.get("selected") or None,
        )

    def declaration(self) -> dict[str, Any]:
        """The declarative form: what was said, and what was chosen.

        What travels, and what a write back would store: nothing in it is a
        live object, a context or a resolved secret.
        """
        return {"selected": self._selected, "values": dict(self._values)}

    # -- reading -------------------------------------------------------------

    @staticmethod
    def kinds() -> tuple[str, ...]:
        """Every kind a Transform can be, in offering order."""
        return tuple(kind.id for kind in TRANSFORM_KINDS)

    def selected_kind(self) -> str:
        """The kind in force.

        Chosen, else the first kind whose own fields carry values, else
        ``identity`` -- a load told nothing about its columns loads its rows as
        they came.
        """
        if self._selected is not None:
            return self._selected
        for kind in TRANSFORM_KINDS:
            if any(_held(self._values.get(name)) for name in kind.fields):
                return kind.id
        return TRANSFORM_KINDS[0].id

    def value(self, name: str) -> Any:
        """What has been said for one field, under any kind, or None."""
        return self._values.get(name)

    def configuration(self) -> dict[str, Any]:
        """The selected kind's fields and their values -- what is in force."""
        kind = _BY_ID[self.selected_kind()]
        return {name: self._values.get(name) for name in kind.fields}

    def validate(self) -> list[str]:
        """What the selected kind still needs, or nothing where it is complete."""
        kind = _BY_ID[self.selected_kind()]
        held = self.configuration()
        return [name for name in kind.required if not _held(held.get(name))]

    # -- changing ------------------------------------------------------------

    def select(self, kind: str) -> None:
        """Choose which kind is in force. The other kinds' values are kept.

        Raises:
            ValueError: If no kind is called that.
        """
        if kind not in _BY_ID:
            raise ValueError(
                f"Transform: no transform kind is called '{kind}'. "
                f"Kinds: {', '.join(self.kinds())}."
            )
        self._selected = kind

    def update(self, name: str, value: Any) -> None:
        """Say something about one field.

        Raises:
            ValueError: If the field is not a Transform field. A source or a
                destination setting handed to a transform is a wiring fault.
        """
        if name not in TRANSFORM_FIELDS:
            raise ValueError(
                f"Transform: '{name}' is not a transform field. "
                f"Fields: {', '.join(TRANSFORM_FIELDS)}."
            )
        self._values[name] = value

    # -- authoring -----------------------------------------------------------

    def observe_source_columns(self, columns: Any) -> None:
        """Be told which columns the source carries, so a mapping can be completed.

        A fact of this instance's context -- whoever read the source says so --
        and not configuration: it changes what an edit completes against and
        what :meth:`columns` shows, and nothing about what is declared.
        """
        self._source_columns = tuple(str(one) for one in (columns or ()))

    def source_columns(self) -> list[str]:
        """The columns the source was last observed to carry."""
        return list(self._source_columns)

    def observe_preview(self, preview: Optional[Mapping[str, Any]]) -> None:
        """Hold the last preview produced through this transform, or None.

        Ephemeral output: nothing about what is declared, selected or valid
        changes, and it is never part of the declarative form.
        """
        self._preview = deepcopy(dict(preview)) if preview is not None else None

    def current_preview(self) -> Optional[dict[str, Any]]:
        """The last preview produced through this transform, or None."""
        return deepcopy(self._preview) if self._preview is not None else None

    # -- persistence ---------------------------------------------------------

    def saves(self) -> bool:
        """Whether this transform can save its mapping to a governed transform."""
        if self.selected_kind() not in _AUTHORED_IN:
            return False
        persistence = self._values.get("persistence")
        return _held(persistence) and dict(persistence).get("transform_id") is not None

    def save(self, ctx: Any) -> None:
        """Save the working column mapping to the governed transform it came from.

        THE TRANSFORM SAVES ITSELF: every entry of :meth:`columns` is sent as it
        stands, with the stored ``transform_column_id`` it is aligned to (None
        for one no stored row stands behind), through the runtime's own database
        access. The database owns how they become rows; the ids it answers with,
        in working order, become this transform's stored identities.

        Args:
            ctx: The runtime context, whose control database this is saved to.

        Raises:
            ValueError: If this transform holds no governed transform to save to.
            ConfigError: If the runtime has no control database.
        """
        if not self.saves():
            raise ValueError(
                "Transform: only a transform populated from a governed file can save it."
            )
        declared, columns, aligned = self._authoring()
        ids = list((aligned or {}).get("column_ids") or [None] * len(columns))
        entries = [
            {**deepcopy(entry), "transform_column_id": ids[at] if at < len(ids) else None}
            for at, entry in enumerate(columns)
        ]
        # Imported here: the bootstrap reaches the load package, not the other way.
        from rey_lib.config.bootstrap import open_shared_control

        control = open_shared_control(ctx).shared_control
        if control is None:
            raise ConfigError("Transform: this runtime has no control database to save to.")
        saved = control.update_transform_columns(
            dict(self._values["persistence"])["transform_id"], entries,
        )
        self._write(declared, columns, {
            "column_ids": list(saved),
            "column_ordinals": list(range(1, len(saved) + 1)),
        })

    def in_force_declaration(self) -> Optional[dict[str, Any]]:
        """The selected kind's declaration as data, or None where there is none to read.

        None for ``identity``; for ``yaml``, whose file is read at resolution;
        and for declaration text that is not JSON -- a declaration a reader
        wrote as YAML by hand is left alone rather than reformatted.
        """
        field = _AUTHORED_IN.get(self.selected_kind())
        if field is None:
            return None
        return _json_mapping(self._values.get(field))

    def columns(self) -> list[dict[str, Any]]:
        """The mapping as it would be authored: every source column represented.

        READ-ONLY. It shows what a first edit would write -- a pass-through for
        each source column the declaration does not already name -- and writes
        nothing, so looking at a mapping authors none.
        """
        declared = self.in_force_declaration() or {}
        completed, _ = _completed(_entries(declared), None, self._source_columns)
        return completed

    def column_ids(self) -> list[Optional[int]]:
        """The stored ``transform_column_id`` of each entry of :meth:`columns`.

        None for an entry no stored row stands behind -- one spliced in or
        added -- and empty where no stored identities are held at all.
        """
        return self._stored("column_ids")

    def column_ordinals(self) -> list[Optional[int]]:
        """The stored ``column_ordinal`` of each entry of :meth:`columns`.

        The PERSISTED ordinal, not the entry's position: the two agree only
        until an entry is moved or added, and what is shown as stored must be
        what is stored. Aligned as :meth:`column_ids` is.
        """
        return self._stored("column_ordinals")

    def _stored(self, key: str) -> list[Any]:
        """One persisted per-entry fact, aligned to :meth:`columns`."""
        aligned = self._held_alignment()
        if aligned is None or key not in aligned:
            return []
        declared = self.in_force_declaration() or {}
        _, completed = _completed(_entries(declared), aligned, self._source_columns)
        return list(completed[key])

    def _held_alignment(self) -> Optional[dict[str, list[Any]]]:
        """The per-entry persisted facts the Manifest kind holds, or None.

        ``column_ids`` wherever a persistence is held, as it always has been;
        ``column_ordinals`` only where the persistence stored them -- none are
        manufactured for one that never had them.
        """
        if self.selected_kind() not in _AUTHORED_IN:
            return None
        persistence = self._values.get("persistence")
        if not _held(persistence):
            return None
        held = dict(persistence)
        return {
            key: list(held.get(key) or ())
            for key in _ALIGNED if key == "column_ids" or key in held
        }

    def edit_column(self, at: int, field: str, value: Any) -> None:
        """Change one field of one entry, and nothing else about it.

        Clearing ``source`` or ``datatype`` removes it -- no source is a
        legitimate shape, and absent datatype is derived. ``export`` is written
        only to say false, because absent means exported. ``type`` puts a COPY
        of that type's starter on the entry, and leaves the entry alone for a
        type the library publishes no starter for. ``transform`` sets the rule,
        or removes it for None.

        Raises:
            ValueError: If the selected kind is not authored here, the field is
                not one an entry has, or no entry is at that position.
        """
        if field not in COLUMN_FIELDS:
            raise ValueError(
                f"Transform: '{field}' is not a column field. "
                f"Fields: {', '.join(COLUMN_FIELDS)}."
            )
        declared, columns, aligned = self._authoring()
        entry = columns[_position(at, columns)]

        if field == "source":
            _set_or_clear(entry, "source", value)
        elif field == "name":
            entry["name"] = "" if value is None else str(value)
        elif field == "datatype":
            _set_or_clear(entry, "datatype", value)
        elif field == "export":
            if value is False or str(value).lower() == "false":
                entry["export"] = False
            else:
                entry.pop("export", None)
        elif field == "type":
            if not _held(value):
                entry.pop("transform", None)
            else:
                starter = authorable_starters().get(str(value))
                if starter is not None:
                    entry["transform"] = starter
        else:  # transform
            if value is None or (isinstance(value, str) and not value.strip()):
                entry.pop("transform", None)
            elif isinstance(value, Mapping):
                entry["transform"] = deepcopy(dict(value))
            else:
                raise ValueError("Transform: a column's transform is a mapping, or None.")

        self._write(declared, columns, aligned)

    def add_column(self, after: Optional[int] = None) -> None:
        """Add an entry after one, from the same source, or at the end.

        After an entry, the new one is a second output from that entry's source,
        named as it; with none given, or beside an entry with no source, it is an
        empty output the reader names.

        Raises:
            ValueError: If the selected kind is not authored here, or ``after``
                names no entry.
        """
        declared, columns, aligned = self._authoring()
        if after is None:
            landed, made = len(columns), {"name": ""}
        else:
            beside = columns[_position(after, columns)]
            source = str(beside.get("source") or "")
            made = {"source": source, "name": source} if source else {"name": ""}
            landed = int(after) + 1
        columns.insert(landed, made)
        for stored in (aligned or {}).values():
            stored.insert(landed, None)
        self._write(declared, columns, aligned)

    def move_column(self, at: int, to: int) -> None:
        """Move one entry to another position. Moving it nowhere authors nothing.

        Raises:
            ValueError: If the selected kind is not authored here, or either
                position names no entry.
        """
        declared, columns, aligned = self._authoring()
        start, end = _position(at, columns), _position(to, columns)
        if start == end:
            return
        columns.insert(end, columns.pop(start))
        for stored in (aligned or {}).values():
            stored.insert(end, stored.pop(start))
        self._write(declared, columns, aligned)

    def _authoring(
        self,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], Optional[dict[str, list[Any]]]]:
        """The declaration an edit lands in, completed, with its stored facts where held.

        Raises:
            ValueError: For a kind that is not authored here, or declaration
                text that is not JSON.
        """
        kind = self.selected_kind()
        field = _AUTHORED_IN.get(kind)
        if field is None:
            raise ValueError(
                f"Transform ({kind}): nothing is authored here. "
                + ("Choose Declaration to author a mapping."
                   if kind == "identity" else
                   "A declaration file is edited where it lives.")
            )
        held = self._values.get(field)
        declared = _json_mapping(held) if _held(held) else {}
        if declared is None:
            raise ValueError(
                f"Transform ({kind}): the declaration is not JSON, and is left as "
                "it was written rather than reformatted."
            )
        columns, aligned = _completed(
            _entries(declared), self._held_alignment(), self._source_columns,
        )
        return declared, columns, aligned

    def _write(
        self,
        declared: dict[str, Any],
        columns: list[dict[str, Any]],
        aligned: Optional[dict[str, list[Any]]],
    ) -> None:
        """Put the whole authored declaration back where its kind keeps it."""
        kind = self.selected_kind()
        written = {**declared, "columns": columns}
        if kind == "declaration":
            # JSON text: the loader parameter's own form, read by `parse_yaml`.
            self._values["transform"] = json.dumps(written, indent=2)
        else:
            self._values["declaration"] = written
        # ONLY WHAT IS HELD. No stored fact is manufactured for a declaration
        # that was never stored, nor a key a persistence never had.
        if aligned:
            self._values["persistence"] = {
                **dict(self._values["persistence"]),
                **{key: list(stored) for key, stored in aligned.items()},
            }

    # -- execution -----------------------------------------------------------

    def executed_declaration(self) -> Optional[dict[str, Any]]:
        """The declaration execution sees, as data -- or None for identity.

        THE ONE STATEMENT OF WHAT RUNS. ``resolve`` builds from this, and
        anything that must show what a load would do -- a preview, an
        inspection -- reads it rather than working it out again. Data, never a
        built transform: building is ``_build_transform``'s.

        Returns:
            None for ``identity``; the parsed declaration for ``declaration``
            (inline YAML or JSON text, or a mapping) and ``yaml`` (the file,
            read here); the stored declaration for ``manifest``.

        Raises:
            ConfigError: If the selected kind is incomplete, or a declaration is
                missing, empty, unreadable or declares no columns.
        """
        missing = self.validate()
        if missing:
            raise ConfigError(
                f"Transform ({self.selected_kind()}) is missing: {', '.join(missing)}."
            )
        held = self.configuration()
        kind = self.selected_kind()
        if kind == "identity":
            return None
        if kind == "declaration":
            return _declared(held["transform"], "transform")
        if kind == "yaml":
            return _declared_in(held["transform-file"])
        return _plain(held["declaration"], "declaration")

    def resolve(self, ctx: Any) -> Any:
        """The transform the selected configuration applies, as the load builds it.

        Every kind goes through ``_build_transform`` -- the one place a load's
        transform is built, and where secrets are resolved -- from
        :meth:`executed_declaration`, and what it builds is handed back, never
        kept.

        Args:
            ctx: The runtime context, for context values a rule may reference.

        Returns:
            An ``IdentityTransform`` for ``identity``; a ``ColumnTransform`` for
            every other kind, carrying its ``TransformPersistence`` where the
            configuration holds one.

        Raises:
            ConfigError: As :meth:`executed_declaration` refuses.
        """
        declared = self.executed_declaration()
        # GOVERNED PERSISTENCE travels only where it is held, for the kinds
        # authored here.
        persistence = self._values.get("persistence")
        if self.selected_kind() in _AUTHORED_IN and _held(persistence):
            return load_operation._build_transform(
                ctx, None, declared, persistence=_persistence(persistence),
            )
        return load_operation._build_transform(ctx, None, declared)


def _json_mapping(value: Any) -> Optional[dict[str, Any]]:
    """A declaration as data where it is a mapping or JSON text of one, else None."""
    if isinstance(value, Mapping):
        return deepcopy(dict(value))
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _entries(declared: Mapping[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """The declaration's column entries, each with its position in ``columns``."""
    held = declared.get("columns")
    if not isinstance(held, list):
        return []
    return [(at, dict(one)) for at, one in enumerate(held) if isinstance(one, Mapping)]


def _completed(
    entries: list[tuple[int, dict[str, Any]]],
    stored: Optional[Mapping[str, list[Any]]],
    structure: tuple[str, ...],
) -> tuple[list[dict[str, Any]], Optional[dict[str, list[Any]]]]:
    """Every source column represented, with no existing entry reordered.

    **A DECLARATION IS AUTHORITATIVE ONCE IT NAMES ONE COLUMN** -- every source
    column it does not name is dropped by a load that looks like it succeeded.
    So an edit authors a mapping that matches the source.

    Each missing source column, in source order, is spliced:

      1. after the LAST entry of the nearest source column before it that has
         one;
      2. failing that, before the FIRST entry of the nearest one after it;
      3. failing both, at the end.

    Every stored per-entry fact stays aligned: a spliced entry has none.
    """
    columns = [entry for _, entry in entries]
    aligned: Optional[dict[str, list[Any]]] = None
    if stored is not None:
        aligned = {
            key: [facts[at] if at < len(facts) else None for at, _ in entries]
            for key, facts in stored.items()
        }

    def held(source: str) -> list[int]:
        return [at for at, one in enumerate(columns) if str(one.get("source") or "") == source]

    for which, source in enumerate(structure):
        if held(source):
            continue
        at: Optional[int] = None
        for before in range(which - 1, -1, -1):
            entries_before = held(structure[before])
            if entries_before:
                at = entries_before[-1] + 1
                break
        if at is None:
            for after in range(which + 1, len(structure)):
                entries_after = held(structure[after])
                if entries_after:
                    at = entries_after[0]
                    break
        landed = len(columns) if at is None else at
        columns.insert(landed, {"source": source, "name": source})
        for facts in (aligned or {}).values():
            facts.insert(landed, None)
    return columns, aligned


def _position(at: Any, columns: list[Any]) -> int:
    """One entry's position, refused where no entry is there."""
    try:
        position = int(at)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Transform: '{at}' is not a column position.") from exc
    if position < 0 or position >= len(columns):
        raise ValueError(f"Transform: there is no column at position {position}.")
    return position


def _set_or_clear(entry: dict[str, Any], key: str, value: Any) -> None:
    """Set one key, or remove it where the value says nothing."""
    if _held(value):
        entry[key] = str(value)
    else:
        entry.pop(key, None)


def _plain(value: Any, named: str) -> dict[str, Any]:
    """A declaration as a mapping: taken as it is, or parsed from YAML or JSON text.

    Raises:
        ConfigError: If it holds nothing, cannot be read, or is not a mapping.
    """
    if isinstance(value, Mapping):
        return dict(value)
    text = str(value)
    if not text.strip():
        raise ConfigError(f"Transform ({named}) holds no declaration.")
    try:
        parsed = parse_yaml(text)
    except ConfigError as exc:
        raise ConfigError(f"Transform ({named}) could not be read: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError(f"Transform ({named}) is not a declaration.")
    return parsed


def _declared(value: Any, named: str) -> dict[str, Any]:
    """A typed declaration, refused where it declares no columns.

    The refusals the CLI makes at its boundary today: a declaration with no
    columns would produce records with no fields, and look like success.
    """
    declared = _plain(value, named)
    if not declared.get("columns"):
        raise ConfigError(
            f"Transform ({named}) declares no columns. A transform says what "
            "each output column is and where it comes from; one with none "
            "would produce records with no fields."
        )
    return declared


def _declared_in(path_text: Any) -> dict[str, Any]:
    """The declaration a file holds, read at resolution as a SQL file is.

    Raises:
        ConfigError: If there is no such file, or what it holds is refused.
    """
    path = Path(str(path_text))
    if not path.is_file():
        raise ConfigError(f"Transform (yaml): no such file: {path}")
    return _declared(read_text_file(path), f"yaml {path}")


def _persistence(value: Any) -> TransformPersistence:
    """The stored identities, as the transform object carries them."""
    held = dict(value)
    return TransformPersistence(
        transform_id=int(held["transform_id"]),
        file_type_id=int(held["file_type_id"]),
        column_ids=tuple(
            None if one is None else int(one) for one in held.get("column_ids") or ()
        ),
    )
