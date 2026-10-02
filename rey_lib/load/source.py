"""The Source: the canonical Loader-facing object for where a load's records come from.

    Source -> Transform -> Target

**ONE OBJECT, EVERY ENTRY POINT.** The Console, a workflow step, the CLI, a
Runner-launched process and scheduled work all construct this same class and
resolve through it. Entry points differ only in how declarative input reaches
it -- a panel's operations, a step's declaration, parsed arguments -- never in
what a source means. Canonical is the class and its contract, not an instance:
no live object crosses a process boundary, and every process builds its own.

**It owns CONFIGURATION, not execution.** It holds which kinds of source this
load can have, what has been said about each, and which one is selected. What
reads records is still the existing resolved object -- ``DataFile`` or
``QuerySource`` -- built by the existing resolvers when something executes, and
discarded when it is done. Nothing here holds a connection, an adapter, a
reader or a resolved object.

**The field names ARE the loader's declared parameter names.** One vocabulary,
from the CLI to whatever projects this object, so nothing translates between
two spellings of the same setting.

    file        file, file-type                          -> DataFile
    database    source-connection, statement             -> QuerySource
    sql_file    source-connection, sql-file              -> QuerySource
    manifest    file-manifest-id, file-mutation-id,      -> DataFile, through
                file-type-id                                ManifestSource

``sql_file`` is a different KIND of source to choose, and the same RESOLVED
form: the file is transport for a statement, read at resolution, exactly as the
CLI reads ``--sql-file`` at its boundary. Both end in one ``QuerySource``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from rey_lib.db.connection import shared_connection
from rey_lib.db.db_adapter import DBAdapter
from rey_lib.db.query_source import QuerySource
from rey_lib.errors.error_utils import ConfigError
from rey_lib.files.data_file import data_file_for
from rey_lib.files.data_file.base import DataFile
from rey_lib.files.file_utils import read_text_file
from rey_lib.load.manifest_source import ManifestSource, SourceContextReader
from rey_lib.errors.error_utils import StateError

__all__ = ["Source", "SOURCE_FIELDS", "SOURCE_KINDS"]


@dataclass(frozen=True)
class _Kind:
    """One kind of source: the fields that are its own, and which it needs."""

    id: str
    fields: tuple[str, ...]
    required: tuple[str, ...]


#: The kinds a Source can be, in the order a reader is offered them.
SOURCE_KINDS: tuple[_Kind, ...] = (
    # `file-mutation-id` names WHICH governed mutation `file` is the path of,
    # where the source was populated from a governed file. Never required.
    _Kind("file", ("file", "file-type", "file-mutation-id"), ("file",)),
    _Kind("database", ("source-connection", "statement"),
          ("source-connection", "statement")),
    _Kind("sql_file", ("source-connection", "sql-file"),
          ("source-connection", "sql-file")),
    # NEITHER IDENTITY IS REQUIRED ALONE -- the rule is the pair's, and
    # `validate` states it. `file-type-id` is never required: without it the
    # file is governed by the type it already has.
    _Kind("manifest", ("file-manifest-id", "file-mutation-id", "file-type-id"), ()),
)

#: Every field a Source holds, across its kinds.
SOURCE_FIELDS: tuple[str, ...] = tuple(dict.fromkeys(
    name for kind in SOURCE_KINDS for name in kind.fields
))

_BY_ID: dict[str, _Kind] = {kind.id: kind for kind in SOURCE_KINDS}

def _held(value: Any) -> bool:
    """Whether a value says anything. Empty text and None say nothing."""
    return value is not None and str(value).strip() != ""


class Source:
    """Where a load's records come from: its configurations, and the one selected.

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
            values: Field values, across every kind. A field of any kind may be
                given -- switching kinds must not lose what was said under
                another -- but only a Source field.
            selected: The chosen kind, or None to let the values decide.

        Raises:
            ValueError: If a value names a field no kind has, or ``selected``
                names no kind.
        """
        self._values: dict[str, Any] = {}
        self._selected: Optional[str] = None
        # CONTEXT, NOT CONFIGURATION: the governed file this source was hydrated
        # with, once, and keeps for its lifetime. Never in `declaration()`, never
        # validated, never persisted.
        self._governed: Optional[dict[str, Any]] = None
        for name, value in (values or {}).items():
            self.update(name, value)
        if selected is not None:
            self.select(selected)

    # -- construction --------------------------------------------------------

    @classmethod
    def from_declaration(cls, declaration: Mapping[str, Any]) -> "Source":
        """Build a Source from its declarative form.

        The inverse of :meth:`declaration`, and the one way an entry point that
        holds only declarative input -- a step, a request, parsed arguments --
        arrives at the same object the others have.
        """
        return cls(
            values=dict(declaration.get("values") or {}),
            selected=declaration.get("selected") or None,
        )

    def declaration(self) -> dict[str, Any]:
        """The declarative form: what was said, and what was chosen.

        What travels, where anything must: nothing in it is a live object.
        """
        return {"selected": self._selected, "values": dict(self._values)}

    # -- reading -------------------------------------------------------------

    @staticmethod
    def kinds() -> tuple[str, ...]:
        """Every kind a Source can be, in offering order."""
        return tuple(kind.id for kind in SOURCE_KINDS)

    def selected_kind(self) -> str:
        """The kind in force.

        Chosen, else the kind whose OWN fields carry values, else the first. A
        field two kinds share -- ``source-connection`` -- cannot decide: a
        connection alone says nothing about whether a query was typed or read
        from a file.
        """
        if self._selected is not None:
            return self._selected
        for kind in SOURCE_KINDS:
            own = [name for name in kind.fields if self._shared_by(name) == 1]
            if any(_held(self._values.get(name)) for name in own):
                return kind.id
        return SOURCE_KINDS[0].id

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
        if kind.id == "manifest":
            if not (_held(held["file-manifest-id"]) or _held(held["file-mutation-id"])):
                return ["file-manifest-id or file-mutation-id"]
            return []
        return [name for name in kind.required if not _held(held.get(name))]

    # -- changing ------------------------------------------------------------

    def select(self, kind: str) -> None:
        """Choose which kind is in force. The other kinds' values are kept.

        Raises:
            ValueError: If no kind is called that.
        """
        if kind not in _BY_ID:
            raise ConfigError(
                f"Source: no source kind is called '{kind}'. "
                f"Kinds: {', '.join(self.kinds())}."
            )
        # SELECTING IS NOT DESTROYING. Which kind is in force is chosen here; the
        # governed context -- like every kind's values -- stays with the object
        # for its lifetime.
        self._selected = kind

    def update(self, name: str, value: Any) -> None:
        """Say something about one field.

        Raises:
            ValueError: If the field is not a Source field. A destination or a
                transform setting handed to a source is a wiring fault, and
                keeping it would carry it somewhere it means nothing.
        """
        if name not in SOURCE_FIELDS:
            raise ConfigError(
                f"Source: '{name}' is not a source field. "
                f"Fields: {', '.join(SOURCE_FIELDS)}."
            )
        # AN EDIT CHANGES THIS VALUE AND NOTHING ELSE -- with one exception: a
        # governed mutation chosen by id brings its own path and resolved SQL.
        # The governed context stays for the object's lifetime.
        self._values[name] = value
        if name == "file-mutation-id":
            chosen = self._mutation_choice(value)
            if chosen is not None:
                self._values["file"] = chosen.get("path") or ""
                self._values["statement"] = chosen.get("resolved_query_sql") or ""

    def _mutation_choice(self, file_mutation_id: Any) -> Optional[dict[str, Any]]:
        """The governed mutation choice with this id, or None."""
        for choice in (self._governed or {}).get("mutation_choices") or []:
            if str(choice.get("file_mutation_id")) == str(file_mutation_id):
                return dict(choice)
        return None

    # -- governed context ----------------------------------------------------

    def observe_governed_context(self, context: Optional[Mapping[str, Any]]) -> None:
        """Be told the governed facts of the file this source reads, or None.

        A fact of this instance's context -- whoever read the governed file
        says so -- and not configuration: nothing about what is declared,
        selected or valid changes.
        """
        self._governed = dict(context) if context else None

    def governed_context(self) -> Optional[dict[str, Any]]:
        """The governed facts last observed for this source, or None."""
        return None if self._governed is None else dict(self._governed)

    def saves_query(self) -> bool:
        """Whether this source's statement can be saved as a governed template."""
        return (self._governed or {}).get("transform_query_id") is not None

    def save_query(self, ctx: Any) -> None:
        """Save the working statement as the governed transform's template.

        THE SOURCE SAVES ITSELF: it holds the transform_query_id and the
        statement, and persists them through the runtime's own database access.
        Both are handed over exactly as held; the database owns turning the
        reader's file argument into the template token.

        Args:
            ctx: The runtime context, whose control database this is saved to.

        Raises:
            ValueError: If this source was not populated from a governed file
                that holds a transform query.
            ConfigError: If the runtime has no control database.
        """
        if not self.saves_query():
            raise StateError(
                "Source: only a source populated from a governed transform query can save it."
            )
        # Imported here: the bootstrap reaches the load package, not the other way.
        from rey_lib.config.bootstrap import open_shared_control

        control = open_shared_control(ctx).shared_control
        if control is None:
            raise ConfigError("Source: this runtime has no control database to save to.")
        control.update_transform_query_sql(
            self._governed["transform_query_id"], self._values.get("statement"),
        )

    # -- execution -----------------------------------------------------------

    def resolve(
        self,
        ctx: Any,
        *,
        reader: Optional[SourceContextReader] = None,
        adapter: Optional[Any] = None,
    ) -> DataFile | QuerySource:
        """The resolved object the selected configuration reads through.

        Built by the existing resolvers and handed back, never kept: the
        connection, the adapter and the reader are this call's, and so is what
        they produce.

        Args:
            ctx: The runtime context, for the configured connections.
            reader: Whatever answers the source-context contract. Needed only
                for a manifest source.
            adapter: The database adapter a query reads through. A plain
                ``DBAdapter`` where none is given.

        Returns:
            A ``DataFile`` for a file or a governed file; a ``QuerySource`` for a
            database query or a SQL file.

        Raises:
            ConfigError: If the selected kind is incomplete, a SQL file is
                missing or empty, or a manifest source has no reader.
        """
        missing = self.validate()
        if missing:
            raise ConfigError(
                f"Source ({self.selected_kind()}) is missing: {', '.join(missing)}."
            )
        held = self.configuration()
        kind = self.selected_kind()

        if kind == "file":
            return data_file_for(
                str(held["file"]), file_type=str(held.get("file-type") or ""),
            )
        if kind == "database":
            return self._query(ctx, held["source-connection"], str(held["statement"]), adapter)
        if kind == "sql_file":
            return self._query(
                ctx, held["source-connection"], _statement_in(held["sql-file"]), adapter,
            )
        # manifest
        if reader is None:
            raise ConfigError(
                "Source (manifest): a source-context reader is required to "
                "resolve a governed file."
            )
        return ManifestSource.create(
            reader,
            file_manifest_id=_as_int(held["file-manifest-id"]),
            file_mutation_id=_as_int(held["file-mutation-id"]),
            file_type_id=_as_int(held["file-type-id"]),
            adopt_persisted_type=True,
        ).data_file()

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _shared_by(name: str) -> int:
        """How many kinds declare one field."""
        return sum(name in kind.fields for kind in SOURCE_KINDS)

    @staticmethod
    def _query(ctx: Any, connection: Any, statement: str, adapter: Any) -> QuerySource:
        """A QuerySource on one configured connection, as the load builds one."""
        return QuerySource(
            shared_connection(ctx, str(connection)).handle(),
            statement,
            adapter=adapter if adapter is not None else DBAdapter(),
        )


def _statement_in(path_text: Any) -> str:
    """The statement a SQL file holds.

    Refused rather than passed on when there is none: a missing file is a
    mistyped path, and an empty one would reach the database as a syntax error
    naming the wrong thing -- the same two refusals the CLI makes at its
    boundary.
    """
    path = Path(str(path_text))
    if not path.is_file():
        raise ConfigError(f"Source (sql_file): no such file: {path}")
    statement = read_text_file(path)
    if not statement.strip():
        raise ConfigError(f"Source (sql_file): {path} holds no statement.")
    return statement


def _as_int(value: Any) -> Optional[int]:
    """An identity as the contract takes it, or None where none was given."""
    return int(value) if _held(value) else None
