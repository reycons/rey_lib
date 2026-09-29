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

**TWO VOCABULARIES.** ``TRANSFORM_FIELDS`` is everything this object holds.
``TRANSFORM_PARAMETERS`` is the subset the loader declares as parameters --
the names a CLI or a panel types. ``declaration`` and ``persistence`` are
fields of this object and not loader parameters.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from rey_lib.config.config_loader import parse_yaml
from rey_lib.data.column_transform import TransformPersistence
from rey_lib.errors.error_utils import ConfigError
from rey_lib.files.file_utils import read_text_file
from rey_lib.load import load_operation

__all__ = ["Transform", "TRANSFORM_FIELDS", "TRANSFORM_KINDS", "TRANSFORM_PARAMETERS"]


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

    # -- execution -----------------------------------------------------------

    def resolve(self, ctx: Any) -> Any:
        """The transform the selected configuration applies, as the load builds it.

        Every kind goes through ``_build_transform`` -- the one place a load's
        transform is built, and where secrets are resolved -- and what it
        builds is handed back, never kept.

        Args:
            ctx: The runtime context, for context values a rule may reference.

        Returns:
            An ``IdentityTransform`` for ``identity``; a ``ColumnTransform`` for
            every other kind, carrying its ``TransformPersistence`` where the
            configuration holds one.

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
            return load_operation._build_transform(ctx, None, None)
        if kind == "declaration":
            return load_operation._build_transform(
                ctx, None, _declared(held["transform"], "transform"),
            )
        if kind == "yaml":
            return load_operation._build_transform(
                ctx, None, _declared_in(held["transform-file"]),
            )
        # manifest -- persistence travels only where it is held.
        persistence = held.get("persistence")
        if _held(persistence):
            return load_operation._build_transform(
                ctx, None, _plain(held["declaration"], "declaration"),
                persistence=_persistence(persistence),
            )
        return load_operation._build_transform(
            ctx, None, _plain(held["declaration"], "declaration"),
        )


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
