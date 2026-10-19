"""One subject, resolved to the statement that reads it (backlog 733).

    run · feed · declared statement · database object · database identity
        -> ResolvedQuery(display_name, statement, connection_map, ddl, name, schema_name)

**WHAT SQL READS THIS DATABASE THING.** That is this object's whole question. A
run and a feed are read by a configured statement filtered to one subject; a
declared statement is a template with one value; a browsed database object
travelled with its own statement; a database identity is read as its table.
Nothing here executes anything -- a query surface draws the statement and its
Run button executes it, which is what a query surface is for.

**IT DOES NOT ASK WHERE A MUTATION WENT.** That is the file manifest's question
(``FileManifest.mutation_destination``), answered as a
``DatabaseObjectIdentity``; this takes the identity. Nothing here imports
``rey_lib.files``.

**SUBSTITUTED RATHER THAN BOUND**, because the statement is read by a person
before it is run. Only rendered values reach it: a value is rendered as the
SQL literal that stands for it first, and only then written in, so no value's
characters compose SQL.
"""

from __future__ import annotations

import threading
from typing import Any, Final

from rey_lib.db.connection import is_database_connection, shared_connection
from rey_lib.db.database_objects import DatabaseObjectIdentity, DatabaseTable
from rey_lib.db.database_query import DatabaseQuery
from rey_lib.db.procedure_map import get_procedure_map, resolve_sql_binding
from rey_lib.errors.error_utils import ConfigError

__all__ = ["ResolvedQuery"]

#: The map the run and feed statements live in, and the binding within it for
#: each subject. Named for the connection they run on, which is an ordinary one:
#: these are queries handed to a person, not the application reaching its own
#: control tables.
_USER_MAP: Final = "rey_apps"
_RUN_VIEW: Final = "run_view"
_FILE_VIEW: Final = "file_view"

#: What a declared statement calls the node's own value. One name, so a
#: declaration is written against the contract rather than against whichever
#: node type happens to use it.
_CURRENT_VALUE: Final = "current_value"


class ResolvedQuery:
    """One subject, as the representations it can be read through.

    Attributes:
        display_name: What the surface calls it.
        statement: The SQL text, with this subject's filters written in.
        connection_map: Which configured map the statement runs on.
        ddl: The definition that created it, for a subject that has one; empty
            for one that does not -- a run and a feed were never defined by one.
        name: What the subject is CALLED, where it is an object with a name of
            its own -- identity, not presentation, so not ``display_name``.
        schema_name: Where the object LIVES, for a subject that lives somewhere.
    """

    __slots__ = (
        "_acting", "connection_map", "ddl", "display_name", "name", "schema_name",
        "statement",
    )

    def __init__(
        self,
        display_name: str,
        statement: str,
        connection_map: str,
        ddl: str = "",
        name: str = "",
        schema_name: str = "",
    ) -> None:
        self.display_name = display_name
        self.statement = statement
        self.connection_map = connection_map
        self.ddl = ddl
        self.name = name
        self.schema_name = schema_name
        # The lock one act is taken under, for a query held on a page runtime
        # and read from more than one request (backlogs 981, 982).
        self._acting = threading.RLock()

    # -- held for a surface ------------------------------------------------------

    def act(self) -> Any:
        """The lock one act is taken under: an operation, and the snapshot answering it."""
        return self._acting

    def projection(self, ctx: Any) -> dict[str, Any]:
        """What this query shows: the representations it was resolved to.

        Read from this object -- nothing is resolved again -- with one thing
        from the installation beside it: the configured database connections a
        reader may run the statement on, the one it runs on first.

        Args:
            ctx: The installation context whose connections are listed.

        Returns:
            display_name, statement, ddl, readings (which of statement and ddl
            are present, in that order), name, schema_name and connections.

        Raises:
            ConfigError: When the connection this statement runs on is not
                configured, so it has nothing to run on.
        """
        with self._acting:
            return {
                "display_name": self.display_name,
                "statement": self.statement,
                "ddl": self.ddl,
                "readings": [
                    reading for reading, present in (
                        ("query", self.statement), ("ddl", self.ddl),
                    ) if present
                ],
                "name": self.name,
                "schema_name": self.schema_name,
                "connections": _connections_led_by(ctx, self.connection_map),
            }

    # -- configured statements -------------------------------------------------

    @classmethod
    def for_run(cls, ctx: Any, run_id: Any) -> "ResolvedQuery":
        """The statement that reads what one run recorded.

        Raises:
            ConfigError: When the run is not named by a whole-number identity,
                or the binding cannot be resolved.
        """
        try:
            run = int(run_id)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"A run is named by its identity; {run_id!r} is not one.") from exc
        return cls(
            display_name=f"Run {run}",
            statement=_bound(ctx, _RUN_VIEW, {"run_id": run, "run_log_id": None}),
            connection_map=_USER_MAP,
        )

    @classmethod
    def for_feed(cls, ctx: Any, feed_identity: Any) -> "ResolvedQuery":
        """The statement that reads one feed's files.

        Raises:
            ConfigError: When the feed is not named, or the binding cannot be
                resolved.
        """
        feed = str(feed_identity or "").strip()
        if not feed:
            raise ConfigError("A feed is named by its identity.")
        return cls(
            display_name=feed,
            statement=_bound(ctx, _FILE_VIEW, {"feed": feed, "file_manifest_id": None}),
            connection_map=_USER_MAP,
        )

    @classmethod
    def for_declared(cls, statement: Any, current_value: Any) -> "ResolvedQuery":
        """A declared statement, with the node's value written in.

        The statement came from the declaration, so there is no binding to look
        up and no subject to recognise -- a template and a value.

        Raises:
            ConfigError: When the declaration carries no statement.
        """
        template = str(statement or "").strip()
        if not template:
            raise ConfigError(
                "A declared query carries the statement it opens, and this one "
                "declared none."
            )
        return cls(
            # The value is the only thing known about the subject, so it is
            # what the surface calls it.
            display_name=str(current_value or ""),
            statement=DatabaseQuery(template, {_CURRENT_VALUE: current_value}).text(),
            connection_map=_USER_MAP,
        )

    # -- database objects --------------------------------------------------------

    @classmethod
    def for_database_object(
        cls,
        name: Any,
        statement: Any,
        ddl: Any,
        connection: Any,
        schema_name: Any = "",
    ) -> "ResolvedQuery":
        """A browsed database object, with the representations it travelled with.

        NOTHING IS COMPOSED. Its statement was written when the database object
        was constructed and travelled signed, so what opens is what the server
        established. Refused only with nothing to show: a routine whose
        provider rendered no call still opens on its definition.

        Raises:
            ConfigError: When it is unnamed, or arrived with neither a statement
                nor a definition.
        """
        named = str(name or "").strip()
        if not named:
            raise ConfigError("A database object is named, never blank.")
        text = str(statement or "").strip()
        definition = str(ddl or "").strip()
        if not text and not definition:
            raise ConfigError(
                f"No representation was authorized for '{named}', so there is "
                "nothing to open."
            )
        return cls(
            display_name=named,
            statement=text,
            connection_map=str(connection or "").strip(),
            ddl=definition,
            name=named,
            schema_name=str(schema_name or "").strip(),
        )

    @classmethod
    def for_database_identity(
        cls, ctx: Any, identity: DatabaseObjectIdentity,
    ) -> "ResolvedQuery":
        """The table one identity names, read as itself: its select and its DDL.

        ONE TABLE, READ BY ``DatabaseTable.read`` -- as a schema read would build
        it -- never a whole schema scanned for it.

        Raises:
            ConfigError: When the connection cannot be opened.
            DatabaseError: When the schema holds no such table.
        """
        handle = shared_connection(ctx, identity.connection).handle()
        table = DatabaseTable.read(handle, identity)
        return cls(
            display_name=identity.name,
            statement=str(table.initial_select or ""),
            connection_map=identity.connection,
            ddl=str(table.ddl or ""),
            name=identity.name,
            schema_name=identity.schema,
        )


def _connections_led_by(ctx: Any, connection: str) -> list[str]:
    """Every configured database connection, the one named first.

    File-reading (DuckDB) connections before the others, as the query surface
    has always listed them, and then the statement's own moved to the front:
    it reads a database object, which a file-reading connection cannot see.

    Raises:
        ConfigError: When ``connection`` is not one of them.
    """
    file_reading: list[str] = []
    others: list[str] = []
    for configured in list(getattr(ctx, "connections", None) or []):
        if not is_database_connection(configured):
            continue
        name = str(getattr(configured, "name", "") or "").strip()
        if not name or name in file_reading or name in others:
            continue
        if str(getattr(configured, "provider", "") or "").lower() == "duckdb":
            file_reading.append(name)
        else:
            others.append(name)
    names = file_reading + others
    if connection not in names:
        raise ConfigError(
            f"No connection named '{connection}' is configured, so the statement "
            "has nothing to run on."
        )
    return [connection] + [name for name in names if name != connection]


def _bound(ctx: Any, binding_name: str, filters: dict[str, Any]) -> str:
    """One configured statement, its filters written in as SQL literals.

    The writing is the query's own (``DatabaseQuery.text``). A filter a subject
    does not use is given as None, which the statement reads as ``NULL`` rather
    than leaving it dangling.
    """
    binding = resolve_sql_binding(get_procedure_map(ctx, _USER_MAP), _USER_MAP, binding_name)
    return DatabaseQuery(str(binding["sql"]), filters).text()
