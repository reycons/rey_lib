"""The Target: the canonical Loader-facing object for where a load's records go.

    Source -> Transform -> Target

**ONE OBJECT, EVERY ENTRY POINT**, as with ``Source`` and ``Transform``. The
Console, a workflow step, the CLI, a Runner-launched process and scheduled work
construct this same class and resolve through it. Canonical is the class and
its contract, not an instance: no live object crosses a process boundary.

**It owns CONFIGURATION, not execution** -- which kinds of target a load can
have, what has been said about each, which one is selected, and the WRITE
POLICY: what the load may do to what is already there. What writes is still the
existing machinery, built by the existing resolution points when something
executes and discarded when it is done. Nothing here holds a loader, an adapter
or a connection.

    database   connection, table,                -> DatabaseObjectIdentity
               create, replace, recreate, append     + DataLoader
    file       out-file                          -> the target DataFile

**The field names ARE the loader's declared destination parameters**, every one
of them, so ``TARGET_PARAMETERS`` and ``TARGET_FIELDS`` are the same names.

**APPEND IS A FIELD, NOT A FOURTH POLICY ARGUMENT.** It is the explicit spelling
of the existing default: ``append`` and "no mode flag" both build the loader
with every flag false, which is what append has always meant. It takes part in
the rule that a destination has one mode; it never widens the builder.

Manifest supplies nothing here: the governed context carries no destination.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from rey_lib.errors.error_utils import ConfigError
from rey_lib.files.data_file import data_file_for
from rey_lib.load import load_operation

__all__ = [
    "MODES", "Target", "TARGET_FIELDS", "TARGET_KINDS", "TARGET_PARAMETERS",
]


@dataclass(frozen=True)
class _Kind:
    """One kind of target: the fields that are its own, and which it needs."""

    id: str
    fields: tuple[str, ...]
    required: tuple[str, ...]


#: The write modes a database target may be given, at most one at a time.
MODES: tuple[str, ...] = ("create", "replace", "recreate", "append")

#: The kinds a Target can be, in the order a reader is offered them.
TARGET_KINDS: tuple[_Kind, ...] = (
    _Kind("database", ("connection", "table", *MODES), ("connection", "table")),
    _Kind("file", ("out-file",), ("out-file",)),
)

#: Every field a Target holds, across its kinds.
TARGET_FIELDS: tuple[str, ...] = tuple(dict.fromkeys(
    name for kind in TARGET_KINDS for name in kind.fields
))

#: The fields the loader declares as parameters -- here, every one of them.
TARGET_PARAMETERS: tuple[str, ...] = TARGET_FIELDS

_BY_ID: dict[str, _Kind] = {kind.id: kind for kind in TARGET_KINDS}


def _held(value: Any) -> bool:
    """Whether a value says anything. None, empty text and a false flag do not."""
    if value is None or value is False:
        return False
    text = str(value).strip()
    return text != "" and text.lower() != "false"


class Target:
    """Where a load's records go: its configurations, and the one selected.

    Built directly, or from a declaration with :meth:`from_declaration`, which is
    how every entry point constructs one.
    """

    def __init__(
        self,
        values: Optional[Mapping[str, Any]] = None,
        selected: Optional[str] = None,
    ) -> None:
        """Hold what has been said about each kind, and which one is chosen.

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
    def from_declaration(cls, declaration: Mapping[str, Any]) -> "Target":
        """Build a Target from its declarative form -- the inverse of :meth:`declaration`."""
        return cls(
            values=dict(declaration.get("values") or {}),
            selected=declaration.get("selected") or None,
        )

    def declaration(self) -> dict[str, Any]:
        """The declarative form: what was said, and what was chosen. Nothing live."""
        return {"selected": self._selected, "values": dict(self._values)}

    # -- reading -------------------------------------------------------------

    @staticmethod
    def kinds() -> tuple[str, ...]:
        """Every kind a Target can be, in offering order."""
        return tuple(kind.id for kind in TARGET_KINDS)

    def selected_kind(self) -> str:
        """The kind in force: chosen, else the first whose own fields say something.

        A flag says something only when it is true, so a stored ``append: false``
        never raises a kind.
        """
        if self._selected is not None:
            return self._selected
        for kind in TARGET_KINDS:
            if any(_held(self._values.get(name)) for name in kind.fields):
                return kind.id
        return TARGET_KINDS[0].id

    def value(self, name: str) -> Any:
        """What has been said for one field, under any kind, or None."""
        return self._values.get(name)

    def configuration(self) -> dict[str, Any]:
        """The selected kind's fields and their values -- what is in force."""
        kind = _BY_ID[self.selected_kind()]
        return {name: self._values.get(name) for name in kind.fields}

    def modes_given(self) -> list[str]:
        """The write modes the database configuration was given, in their order."""
        return [name for name in MODES if _held(self._values.get(name))]

    def write_policy(self) -> str:
        """What the load may do to what is there: one mode, or ``append`` where none.

        With more than one given there is no single answer; :meth:`validate`
        says so, and this answers the first so that nothing here guesses harder.
        """
        given = self.modes_given()
        return given[0] if given else "append"

    def validate(self) -> list[str]:
        """What the selected kind still needs, or what is wrong with it.

        Information, never a refusal: the missing requirements by name, and --
        for a database target given more than one write mode -- one entry
        naming the modes given together, because a destination has one mode.
        """
        kind = _BY_ID[self.selected_kind()]
        held = self.configuration()
        problems = [name for name in kind.required if not _held(held.get(name))]
        if kind.id == "database":
            given = self.modes_given()
            if len(given) > 1:
                problems.append(f"one mode, not {', '.join(given)} together")
        return problems

    # -- changing ------------------------------------------------------------

    def select(self, kind: str) -> None:
        """Choose which kind is in force. The other kind's values are kept.

        Raises:
            ValueError: If no kind is called that.
        """
        if kind not in _BY_ID:
            raise ConfigError(
                f"Target: no target kind is called '{kind}'. "
                f"Kinds: {', '.join(self.kinds())}."
            )
        self._selected = kind

    def update(self, name: str, value: Any) -> None:
        """Say something about one field.

        Raises:
            ValueError: If the field is not a Target field.
        """
        if name not in TARGET_FIELDS:
            raise ConfigError(
                f"Target: '{name}' is not a target field. "
                f"Fields: {', '.join(TARGET_FIELDS)}."
            )
        self._values[name] = value

    def set_write_policy(self, policy: str) -> None:
        """Say what the load may do: one mode on, the other three off.

        THE ONE-MODE RULE IS APPLIED HERE, by the Target, rather than by
        whoever offered the choice. ``append`` is set as ``append`` -- the
        explicit spelling of the default -- exactly as a reader choosing it
        states it.

        Raises:
            ValueError: If ``policy`` is not one of the write modes.
        """
        if policy not in MODES:
            raise ConfigError(
                f"Target: '{policy}' is not a write mode. Modes: {', '.join(MODES)}."
            )
        for mode in MODES:
            self._values[mode] = mode == policy

    # -- execution -----------------------------------------------------------

    def resolve(self, ctx: Any) -> Any:
        """What the selected configuration writes through, built and handed back.

        Through the existing resolution points, and never kept: nothing is
        opened here, and what is built is the caller's.

        Returns:
            For ``database``, the pair ``(DatabaseObjectIdentity, DataLoader)``
            -- the object written to, and the policy it is written with. For
            ``file``, the target ``DataFile``.

        Raises:
            ConfigError: If the selected kind is incomplete or was given more
                than one write mode. Nothing is built first.
            ValueError: Where a resolution point refuses what it was given --
                a destination with no schema, a suffix that names no format.
        """
        problems = self.validate()
        if problems:
            raise ConfigError(
                f"Target ({self.selected_kind()}) is not complete: {'; '.join(problems)}."
            )
        held = self.configuration()
        if self.selected_kind() == "file":
            destination = data_file_for(str(held["out-file"]))
            # A destination is WRITTEN, so it must name a format with a writer.
            # data_file_for represents any physical file (an UntypedFile when
            # no format claims it, backlog 624); a load target still refuses
            # one here, before anything is built or read.
            if not destination.file_type:
                raise ConfigError(
                    f"Target (file): '{held['out-file']}' names no writable format."
                )
            return destination
        policy = self.write_policy()
        # APPEND IS THE DEFAULT, spelled: every flag false, as it has always been.
        return (
            load_operation._destination_identity(
                str(held["table"]), str(held["connection"]),
            ),
            load_operation._build_data_loader(
                ctx, policy == "create", policy == "replace", policy == "recreate",
            ),
        )
