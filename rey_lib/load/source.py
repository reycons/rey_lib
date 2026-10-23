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

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from rey_lib.db.connection import shared_connection
from rey_lib.db.query_source import QuerySource
from rey_lib.errors.error_utils import ConfigError
from rey_lib.files.data_file import DataFile
from rey_lib.files.data_file.base import DataFile
from rey_lib.files.file_utils import read_text_file
from rey_lib.load.manifest_source import ManifestSource, SourceContextReader
from rey_lib.errors.error_utils import StateError
from rey_lib.logs import AppLogger

__all__ = ["Source", "SOURCE_FIELD_METADATA", "SOURCE_FIELDS", "SOURCE_KINDS"]

_logger = AppLogger(__name__)


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

#: What each Source field is: the type of value it holds, what it means, and --
#: only where one helps -- an example of its form. The one statement of these
#: facts: the projection carries them and the loader's CLI declaration reads
#: them (#1151). No placeholder is a real answer.
SOURCE_FIELD_METADATA: dict[str, dict[str, str]] = {
    "file": {
        "value_type": "path", "placeholder": "/path/to/file.csv",
        "description": "The one file to load.",
    },
    "file-type": {
        "value_type": "choice", "placeholder": "Auto detect",
        "description": "The file's format, where its suffix does not name one.",
    },
    "file-manifest-id": {
        "value_type": "string",
        "description": "The governed file to load, by its manifest identity. The current "
                       "state of that file is resolved from it.",
    },
    "file-mutation-id": {
        "value_type": "string",
        "description": "One specific materialised state of a governed file. Use it to load "
                       "that state rather than the file's current one.",
    },
    "file-type-id": {
        "value_type": "string",
        "description": "The file type to work within, where type-level configuration is "
                       "being edited. Omit it to open the file without a governing scope.",
    },
    "source-connection": {
        "value_type": "choice",
        "description": "The connection the SOURCE statement runs on. The destination has "
                       "its own, and they may differ.",
    },
    # SQL, SAID BY THE VOCABULARY. A surface gives it a code editor because the
    # value IS code -- the same way every other value_type decides its control,
    # and without anything recognising a field called "statement". `sql-file`
    # stays a path: it is the alternate transport for this text, not the text.
    "statement": {
        "value_type": "sql", "placeholder": "select ... from ...",
        "description": "The SOURCE query, whose result is loaded.",
    },
    "sql-file": {
        "value_type": "path", "placeholder": "/path/to/query.sql",
        "description": "A file holding the SOURCE query, instead of giving it inline.",
    },
}

_BY_ID: dict[str, _Kind] = {kind.id: kind for kind in SOURCE_KINDS}

#: The governed identities, in the order they are shown.
_GOVERNED_IDENTITY: tuple[str, ...] = (
    "file_manifest_id", "file_mutation_id", "installation_id", "data_profile_id",
)

def _held(value: Any) -> bool:
    """Whether a value says anything. Empty text and None say nothing."""
    return value is not None and str(value).strip() != ""


def _control_of(ctx: Any, purpose: str) -> Any:
    """The runtime's one Control, or a refusal naming what needed it.

    Raises:
        ConfigError: If the runtime has no control database.
    """
    # Imported here: the bootstrap reaches the load package, not the other way.
    from rey_lib.config.bootstrap import open_shared_control  # noqa: PLC0415

    control = open_shared_control(ctx).shared_control
    if control is None:
        _logger.debug("source: refused -- no control database to %s", purpose)
        raise ConfigError(f"Source: this runtime has no control database to {purpose}.")
    return control


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
        # THE GOVERNED FILE'S ONE READ (backlog 830), held as the Transform holds
        # its selected HTTP operation: the ManifestSource this source was established from,
        # replaced by its own re-read after a save. Never in `declaration()`,
        # never an execution argument, never persisted.
        self._manifest: Optional[ManifestSource] = None
        # ONE ACT AT A TIME: an operation and the snapshot answering it are taken
        # under this, so the snapshot is the state that operation produced.
        self._acting = threading.RLock()
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

    @classmethod
    def from_governed(
        cls,
        ctx: Any,
        *,
        file_manifest_id: Any = None,
        file_mutation_id: Any = None,
    ) -> "Source":
        """Establish a Source from one governed file: its ONE read (backlog 830).

        The file's persisted type governs, so its stored transform is read. The
        ManifestSource that read is held -- the Source's saved settings and its
        re-reads go through it -- and the Source is populated from it as an
        ordinary source. No Transform is needed: one built ``from_source`` on
        this Source takes its own half from the same read.

        Args:
            ctx: The runtime context, whose control database is read.
            file_manifest_id: The governed file, by manifest.
            file_mutation_id: The governed file, by one of its mutations.

        Returns:
            The Source, populated and holding its governed read.

        Raises:
            ConfigError: If the runtime has no control database.
        """
        manifest = ManifestSource.create(
            _control_of(ctx, "read a governed file"),
            file_manifest_id=file_manifest_id,
            file_mutation_id=file_mutation_id,
            adopt_persisted_type=True,
        )
        built = cls()
        built._manifest = manifest
        manifest.populate_source(built)
        return built

    def manifest(self) -> Optional[ManifestSource]:
        """The governed read this Source was established from, or None."""
        return self._manifest

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

    # -- its snapshot --------------------------------------------------------

    def act(self) -> Any:
        """The lock one act is taken under: an operation, and the snapshot answering it."""
        return self._acting

    def projection(self, ctx: Any) -> dict[str, Any]:
        """What this Source shows: ONE snapshot of ONE state (backlog 681).

        Built from copies, never from its own containers, so the snapshot is
        never a second path into its state. Its governed context is read once,
        and DISTRIBUTED here to where each fact is shown, so a surface never
        reads a governed column name to decide anything.

        Args:
            ctx: The runtime context. Unused: nothing is read.
        """
        with self._acting:
            governed = self.governed_context()
            return {
                "kinds": list(self.kinds()),
                # EACH KIND'S OWN FIELDS, as the Source declares them and in order.
                "fields": {kind.id: list(kind.fields) for kind in SOURCE_KINDS},
                "selected": self.selected_kind(),
                "values": dict(self.declaration()["values"]),
                "configuration": dict(self.configuration()),
                "missing": list(self.validate()),
                # WHAT EACH FIELD IS, as the Source's vocabulary states it.
                "field_metadata": {name: dict(found)
                                   for name, found in SOURCE_FIELD_METADATA.items()},
                "governed": _governed_projection(governed),
                "choices": _choices(governed),
                # WHAT IT CAN DO BEYOND ITS EDITS.
                "operations": ["save_query"] if self.saves_query() else [],
            }

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
        # THE HELD READ IS STALE: the persisted query changed (backlog 830).
        self.reread_governed(ctx)

    def reread_governed(self, ctx: Any) -> bool:
        """Read this Source's governed file again, after a save made its read stale.

        ONE re-read through the held ManifestSource, keeping the selected
        transform; this Source is then told the fresh governed context. Its own
        configuration, unsaved edits included, is kept. A Source not established
        from a governed file reads nothing.

        Args:
            ctx: The runtime context, whose control database is read.

        Returns:
            Whether anything was read.

        Raises:
            ConfigError: If the runtime has no control database.
        """
        if self._manifest is None:
            return False
        self._manifest = self._manifest.reread(_control_of(ctx, "re-read a governed file"))
        self.observe_governed_context(self._manifest.governed_context())
        return True

    def save_saved_settings(self, ctx: Any, settings: Any) -> None:
        """Persist the edited names and default of this governed file's saved settings.

        The one write, through the held ManifestSource; this Source is then told
        its governed context once more, so it carries the saved names and default.

        Args:
            ctx: The runtime context, whose control database is written.
            settings: The edited rows, each ``transform_id``, ``transform_name``
                and ``is_default``.

        Raises:
            StateError: If this Source was not established from a governed file.
            ConfigError: If the runtime has no control database.
            DataStructureError: If a row is not one of the file type's transforms.
        """
        if self._manifest is None:
            _logger.debug("source: refused -- saved settings on a source with no governed file")
            raise StateError(
                "Source: this source was not opened on a governed file; it has no saved settings."
            )
        self._manifest.save_transforms(_control_of(ctx, "save saved settings"), settings)
        self.observe_governed_context(self._manifest.governed_context())

    # -- execution -----------------------------------------------------------

    def resolve(
        self,
        ctx: Any,
        *,
        reader: Optional[SourceContextReader] = None,
    ) -> DataFile | QuerySource:
        """The resolved object the selected configuration reads through.

        Built by the existing resolvers and handed back, never kept: the
        connection and the reader are this call's, and so is what they
        produce. A query runs through DatabaseQuery (backlog 757), so no
        adapter is chosen here.

        Args:
            ctx: The runtime context, for the configured connections.
            reader: Whatever answers the source-context contract. Needed only
                for a manifest source.

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
            return DataFile.for_path(
                str(held["file"]), file_type=str(held.get("file-type") or ""),
            )
        if kind == "database":
            return self._query(ctx, held["source-connection"], str(held["statement"]))
        if kind == "sql_file":
            return self._query(
                ctx, held["source-connection"], _statement_in(held["sql-file"]),
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
    def _query(ctx: Any, connection: Any, statement: str) -> QuerySource:
        """A QuerySource on one configured connection, as the load builds one."""
        return QuerySource(shared_connection(ctx, str(connection)).handle(), statement)


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


def _governed_projection(context: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    """A Source's governed facts, DISTRIBUTED to where each is shown.

    The file's identities as one group, its name as another, and its saved
    settings -- every transform of the file's type, and the one selected, None
    while the selection is unresolved.
    """
    if not context:
        return None
    return {
        "identity": {name: context.get(name) for name in _GOVERNED_IDENTITY},
        "file": {"file_name": context.get("file_name")},
        "saved_settings": {
            "choices": [dict(one) for one in context.get("transform_choices") or []],
            "selected": context.get("selected_transform_id"),
        },
    }


def _choices(context: Optional[Mapping[str, Any]]) -> dict[str, list[dict[str, str]]]:
    """The values a Source field may be chosen from, labelled for display.

    `file-type` is offered the data formats of File's one registry
    (``DataFile.formats()``, #1133), read now and never copied. The governed
    file's live mutations offer `file-mutation-id`, each labelled by its
    `result`; it is offered nothing where there is nothing held.
    """
    formats = [{"value": one, "label": one} for one in sorted(DataFile.formats())]
    offered = [
        {"value": str(one.get("file_mutation_id")), "label": str(one.get("result") or "")}
        for one in (context or {}).get("mutation_choices") or []
    ]
    return {"file-type": formats, **({"file-mutation-id": offered} if offered else {})}
