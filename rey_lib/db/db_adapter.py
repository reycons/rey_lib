"""
Backend-agnostic database adapter.

DBAdapter is the single point through which all DB work flows in rey_lib's
file pipeline and any app code that opts in. Each method resolves the
backend from the registry and delegates — no provider-specific logic lives
here.

Adding a new backend
--------------------
1. Create ``rey_lib/db/{provider}_utils.py`` implementing the interface
   (see existing sqlserver_utils / duckdb_utils as reference).
2. Add one entry to ``_REGISTRY`` below.
3. Add one entry to ``_MODULE_PREFIXES`` so open connections are recognised.
4. Set ``provider: {name}`` in the connection config YAML.

No other changes to this file are required.

Provider resolution
-------------------
``get_connection`` inspects ``db_cfg.provider`` (case-insensitive). When that
field is absent, the adapter falls back to inferring the provider from the
``driver`` string — any driver containing ``"sql server"`` resolves to
``sqlserver``. Other backends must declare ``provider`` explicitly.

All other methods detect the provider from the open connection object via
``_provider_for_conn``.
"""

from __future__ import annotations

import importlib
from typing import Any, Optional

from rey_lib.db.database_query import PageResult, StatementResult
from rey_lib.db.routine_call import RoutineCall
from rey_lib.logs import AppLogger
from rey_lib.errors.error_utils import (
    ConfigError,
    DatabaseError,
    UnsupportedDatabaseCapabilityError,
)

__all__ = ["DBAdapter"]


# ---------------------------------------------------------------------------
# Backend registry
#
# Maps provider name → fully-qualified module path.
# All backend modules must implement the interface defined by the existing
# sqlserver_utils and duckdb_utils modules.
#
# To add a new backend: add one entry here and create the utils module.
# Nothing else in this file needs to change.
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, str] = {
	"sqlserver": "rey_lib.db.sqlserver_utils",
	"duckdb":    "rey_lib.db.duckdb_utils",
	"mysql":     "rey_lib.db.mysql_utils",
	"postgres":  "rey_lib.db.postgres_utils",
}

# Maps connection object module prefix → provider name.
# Used by _provider_for_conn when conn.provider is not set.
# Add one entry here per new backend.
_MODULE_PREFIXES: dict[str, str] = {
	"pyodbc":          "sqlserver",
	"duckdb":          "duckdb",
	"_duckdb":         "duckdb",
	"mysql.connector": "mysql",
	"psycopg":         "postgres",
	# Kept while a connection opened by the previous driver can still exist --
	# a live process, or an environment that has not been reinstalled. It costs
	# one entry and removes a way for provider inference to answer nothing.
	"psycopg2":        "postgres",
}

_METADATA_CAPABILITIES = frozenset(
    {
        "catalogs",
        "schemas",
        "tables",
        "views",
        "columns",
        "primary_keys",
        "foreign_keys",
        "indexes",
        "unique_constraints",
        "check_constraints",
        "procedures",
        "functions",
    }
)

# Routine capability → the backend's routine kind. The SQLAlchemy Inspector has
# no routines API, so both providers answer these from their own catalog rather
# than through the shared inspector helpers.
_ROUTINE_KINDS: dict[str, str] = {
    "procedures": "procedure",
    "functions": "function",
}

_SQLALCHEMY_METADATA_CAPABILITIES = _METADATA_CAPABILITIES
_PROVIDER_METADATA_CAPABILITIES: dict[str, frozenset[str]] = {
    "postgres": _SQLALCHEMY_METADATA_CAPABILITIES,
    "mysql": _SQLALCHEMY_METADATA_CAPABILITIES,
    "duckdb": frozenset({"catalogs", "schemas", "tables", "views"}),
    # Schemas from its inventory (backlog 723), as DuckDB's are.
    "sqlserver": frozenset({"schemas", "procedures", "functions"}),
}

# The provider contract: the methods this adapter publishes and fulfils by
# calling a function of the SAME NAME on the provider module. Declared here so
# that a provider which does not implement one REFUSES BY NAME instead of
# raising AttributeError from inside a dispatch -- the adapter was presenting a
# contract its providers do not uniformly satisfy, and could not detect it.
#
# Support is RESOLVED from the module, not listed beside it. A second
# hand-maintained table of which provider implements what would drift from the
# modules it describes, which is this same defect one level up. ``call_routine``
# already refuses this way: look the function up, and name the provider when it
# is absent.
#
# ``is_truncation_error`` is deliberately NOT here. It takes no connection, so
# there is no provider to refuse for; it asks every backend in turn and a
# missing implementation is already absorbed by that loop.
_PROVIDER_CONTRACT_CAPABILITIES = frozenset(
    {
        "call_proc",
        "call_proc_with_output",
        "get_table_columns",
        "bulk_insert",
        # Optional by design, and the only two here that are. A provider
        # without `insert_from_path` loses nothing: the record-based path
        # remains the guaranteed way to load anything, so support is an
        # opportunity rather than a requirement.
        "insert_from_path",
        # THE TWO PRIMITIVES DatabaseTable's statements are built from -- its
        # `drop` and `delete_all_rows` are written ONCE (backlog 722), and
        # need from a provider only what genuinely differs: how it spells
        # an identifier, and how its driver runs one statement on a connection
        # and counts what it touched. Required, not optional: a provider
        # without them fails the contract test rather than a load.
        "quote_identifier",
        "execute_statement",
        # What a provider's catalog says about one table (backlog 695): its
        # columns, for a provider whose table DDL the table composes; or the
        # complete definition the server itself supplies. Each is implemented
        # by the providers it applies to and refused by name elsewhere.
        "table_column_catalog",
        "table_native_definition",
        # Whether a provider's catalog holds a table, and a view's definition as
        # the server supplies it (backlog 698): facts only -- existence and
        # view DDL semantics stay DatabaseTable's and DatabaseView's.
        "table_exists",
        "view_native_definition",
        # The schema's structure facts -- tables with keys, indexes and
        # constraints, views -- in one shape for every provider (backlog 725).
        # Facts only: DatabaseSchema.read builds the objects.
        "schema_metadata",
        # A routine's own definition, as the server stores it (backlog 695):
        # the fact DatabaseProcedure (and later DatabaseFunction) reads its DDL
        # from. ``kind`` says which routine; the semantics stay the object's.
        "get_routine_definition",
        # What a catalog says about one index on one table (backlog 695): its
        # key columns and kind, for a provider whose index DDL the index
        # composes; or the definition the server itself supplies.
        "index_column_catalog",
        "index_native_definition",
        # What a catalog says about one constraint on one table (backlog 695):
        # its kind, columns, references and check, for a provider whose
        # constraint DDL the owning object composes; or the definition the
        # server itself supplies.
        "constraint_catalog",
        "constraint_native_definition",
        # A trigger's own definition, as the server stores it, found by its
        # table and name (backlog 695): what DatabaseTrigger reads its DDL from.
        "trigger_native_definition",
        # What a catalog says about one sequence (backlog 695): its start,
        # increment, bounds and cycling, for a provider whose sequence DDL the
        # sequence composes; or the definition the server itself supplies.
        "sequence_catalog",
        "sequence_native_definition",
        # What a catalog says about one user-defined type (backlog 695): its
        # kind, base type, default, nullability and enum labels, from which
        # DatabaseUserDefinedType composes its DDL.
        "user_type_catalog",
        # Which objects a database holds, with what each depends on: the
        # inventory the database objects claim their own entries from
        # (DatabaseObjectListing, backlog 723).
        "list_database_objects",
        # One-row and targeted table DML (backlog database.table_dml_surface):
        # what DatabaseTable.insert / update / delete need from a provider --
        # the statement, its binding, and how the provider returns the inserted
        # row's key and the rows it touched. Semantics stay DatabaseTable's.
        "insert",
        "update",
        "delete",
    }
)

_logger = AppLogger(__name__)

#: How much of a statement identifies it in a log. Enough to recognise which
#: query was running, and never the whole text -- which is long and can carry
#: literals a caller inlined.
_STATEMENT_GLIMPSE = 160


def _glimpse(sql_text: str) -> str:
    """One statement, shortened to what identifies it."""
    said = " ".join(str(sql_text or "").split())
    return said if len(said) <= _STATEMENT_GLIMPSE else f"{said[:_STATEMENT_GLIMPSE]}..."


def _backend(provider: str) -> Any:
    """Return the backend module for provider, importing lazily on first use."""
    path = _REGISTRY.get(provider)
    if path is None:
        raise ConfigError(
            f"DBAdapter: unsupported provider '{provider}'. "
            f"Registered providers: {sorted(_REGISTRY)}."
        )
    return importlib.import_module(path)


def _execute_statements_over_dbapi(
    conn: Any,
    sql_text: str,
    *,
    limit: int,
) -> list[StatementResult]:
    """Execute over the DBAPI cursor the generic path already uses.

    The starting implementation for providers already reading through
    ``cursor.execute`` / ``description`` / ``fetchmany``. It reuses those
    mechanics; the behaviour is not the same as ``query_rows``, because this
    normalizes a result that returned nothing and answers with a list.

    **One execute, then every result that execution exposes.** The batch already
    ran; what was missing was reading past the first set, which capped this path
    at one however capable the driver was.

    A driver that exposes one after the batch returns one, and that is an
    answer rather than a shortfall.
    """
    cursor = None
    try:
        # Control leaves Rey here. What the driver raises says what went wrong
        # and never what was being attempted, and the error boundary that
        # finally catches it is further still from knowing.
        _logger.debug(
            "attempting dbapi statement execution limit=%s statement=%s",
            limit, _glimpse(sql_text),
        )
        cursor = conn.cursor()
        cursor.execute(sql_text)
        collected: list[StatementResult] = []
        while _advanced(cursor) if collected else True:
            collected.append(_result_from_cursor(cursor, limit=limit))
            if len(collected) >= _MAX_RESULT_SETS:
                _logger.debug(
                    "driver reported more than %s result sets for statement=%s",
                    _MAX_RESULT_SETS, _glimpse(sql_text),
                )
                raise DatabaseError(
                    "DBAdapter: the driver reported more than "
                    f"{_MAX_RESULT_SETS} result sets from one execution, which "
                    "is a driver that never says it has finished rather than a "
                    "batch that large."
                )
        return collected
    except Exception as exc:
        _logger.debug(
            "dbapi statement execution failed for statement=%s", _glimpse(sql_text),
        )
        raise DatabaseError(f"DBAdapter: execution failed: {exc}") from exc
    finally:
        if cursor is not None and hasattr(cursor, "close"):
            cursor.close()


#: How many result sets one execution may expose before this stops believing the
#: driver.
#:
#: Not a limit on a batch. DBAPI says ``nextset`` answers true or None, and a
#: driver that answers true forever would spin this loop inside a request thread
#: with nothing to show for it. Reaching this is a broken driver, and it is
#: reported as one rather than hung on.
_MAX_RESULT_SETS = 256


def _advanced(cursor: Any) -> bool:
    """Whether the cursor moved to another result set.

    A presence check, and deliberately no ``try``. A driver that has the method
    and fails while advancing has failed mid-batch, which is an execution error
    and not the end of the results -- swallowing it would report a truncated
    batch as a complete one.

    The one driver whose ``nextset`` *raises* rather than answering is psycopg2,
    and it never arrives here: ``postgres_utils`` implements its own
    ``execute_statements``. Catching for its sake would blind this path for
    every driver that does participate.
    """
    nextset = getattr(cursor, "nextset", None)
    return bool(nextset()) if callable(nextset) else False


def _result_from_cursor(cursor: Any, *, limit: int) -> StatementResult:
    """One cursor's current result, normalized.

    No description means the statement returned no rows -- which is what DDL and
    a call do, and is an answer rather than an error.
    """
    description = getattr(cursor, "description", None) or []
    columns = [str(column[0]) for column in description]
    values = cursor.fetchmany(max(1, int(limit))) if columns else []
    return StatementResult(
        columns=columns,
        rows=[dict(zip(columns, row)) for row in values],
        row_count=_row_count(cursor),
    )


def _row_count(cursor: Any) -> int | None:
    """What the driver said it affected, or None where it said nothing useful.

    DBAPI uses -1 for "unknown", and drivers differ on what the number means for
    a query versus a write. Only a count that is meaningful crosses this
    boundary; the rest becomes None and is reported as unknown rather than as a
    number somebody might believe.
    """
    count = getattr(cursor, "rowcount", None)
    try:
        count = int(count)
    except (TypeError, ValueError):
        return None
    return None if count < 0 else count


class DBAdapter:
    """
    Dispatch DB operations to the correct backend based on connection config.

    A single instance can serve any mix of backends — provider lookup happens
    per call. Stateless and safe to share across threads of a single run.
    """

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def get_connection(self, db_cfg: Any, *, ctx: Any = None) -> Any:
        """
        Open a backend-appropriate connection for ``db_cfg``.

        Parameters
        ----------
        db_cfg : Any
            Connection config Namespace. Must expose a ``provider`` field.
            Falls back to inferring from ``driver`` for backwards compatibility
            with older SQL Server configs that pre-date the provider field.
        ctx : Any
            Application context, carried through to the backend so a credential
            naming an environment variable can be read as the connection opens.
            Backends needing no credential ignore it.

        Returns
        -------
        Any
            An open backend connection (e.g. ``pyodbc.Connection``).

        Raises
        ------
        ConfigError
            If the provider is missing, unknown, or cannot be inferred, or a
            credential names an environment variable that cannot be read now.
        """
        provider = self._provider_for_cfg(db_cfg)
        conn = _backend(provider).get_connection(db_cfg, ctx=ctx)
        try:
            setattr(conn, "provider", provider)
        except (AttributeError, TypeError):
            pass
        return conn

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def run_sql(
        self,
        conn:     Any,
        sql_text: str,
        params:   Optional[list[Any]] = None,
    ) -> Any:
        """Execute ad hoc SQL text (e.g. a generated DDL file) and commit.

        Delegates to the backend's ``run_sql`` implementation. Use for raw
        SQL where a named query or stored procedure is not appropriate, such
        as applying generated DDL files.

        Parameters
        ----------
        conn : Any
            Open backend connection.
        sql_text : str
            SQL statement(s) to execute.
        params : Optional[list[Any]]
            Positional query parameters, or ``None`` for parameterless SQL.

        Returns
        -------
        Any
            Backend-specific result (e.g. rows affected for DDL).

        Raises
        ------
        NotImplementedError
            If the connection's provider has no ad hoc SQL support.
        """
        backend = _backend(self._provider_for_conn(conn))
        if not hasattr(backend, "run_sql"):
            raise UnsupportedDatabaseCapabilityError(
                f"DBAdapter: provider '{self._provider_for_conn(conn)}' "
                "does not support run_sql."
            )
        return backend.run_sql(conn, sql_text, params)

    def execute_sql(
        self,
        conn:         Any,
        sql_text:     str,
        named_params: dict[str, Any],
        result_mode:  str,
    ) -> Any:
        """Execute parameter-bound SQL text and return a value per result_mode.

        Named ``:param`` placeholders are bound safely (never interpolated).
        ``result_mode`` is ``no_return`` (None), ``scalar_result`` (one value),
        or ``dataset_result`` (list of column→value dict rows). Used by the
        shared mapped-SQL / ad hoc SQL execution path.

        Raises
        ------
        NotImplementedError
            If the connection's provider has no named-SQL support.
        """
        backend = _backend(self._provider_for_conn(conn))
        if not hasattr(backend, "execute_named_sql"):
            raise UnsupportedDatabaseCapabilityError(
                f"DBAdapter: provider '{self._provider_for_conn(conn)}' "
                "does not support execute_named_sql."
            )
        return backend.execute_named_sql(conn, sql_text, named_params, result_mode)

    def execute_statements(
        self,
        conn: Any,
        sql_text: str,
        *,
        limit: int = 1_000,
    ) -> list[StatementResult]:
        """Execute SQL text as written and return every result it produced.

        The text is not read, classified or rewritten here. What may be executed
        is the connected role's answer, not this layer's: nothing counts
        statements, checks a leading keyword or scans for a forbidden word.

        A statement that returns no rows is an ordinary outcome -- DDL and a
        procedure call both produce one -- and appears as a result with no
        columns rather than as a failure.

        **The result is a list from the outset.** A provider that can expose
        several results returns several; one that exposes a single result
        returns one. Nothing above has to change when a provider's capability
        does, and no caller may assume there is exactly one.

        Args:
            conn: Open connection handle.
            sql_text: The SQL to execute, exactly as written.
            limit: The most rows to read from any one result.

        Returns:
            One :class:`StatementResult` per result the driver exposed.

        Raises:
            DatabaseError: If execution fails.
        """
        provider = self._provider_for_conn(conn)
        backend = _backend(provider)
        own = getattr(backend, "execute_statements", None)
        if callable(own):
            # A provider whose batch semantics, result advancement or
            # command-only results differ answers for itself. Which drivers
            # those are is measured, never assumed from a method existing.
            return list(own(conn, sql_text, limit=limit))
        return _execute_statements_over_dbapi(conn, sql_text, limit=limit)

    def execute_page(
        self,
        conn: Any,
        sql_text: str,
        *,
        offset: int,
        limit: int,
        order_by: Optional[list[dict[str, Any]]] = None,
        filters: Optional[list[dict[str, Any]]] = None,
    ) -> PageResult:
        """Return one page of a query's result, and the size of the whole.

        **This dispatches and builds nothing.** Paging syntax, the count, and
        the judgement of whether a statement can be paged at all are dialect
        facts, so they live in the provider. There is deliberately no generic
        DBAPI implementation: DBAPI gives execution mechanics, not paging or
        count semantics, and writing one here would put SQL rendering back into
        the shared layer this is keeping it out of.

        A provider that declares no paging capability does not have one, and
        that is an answer rather than a shortfall -- reported, never softened
        into an empty page, which would publish "this query has no rows" as
        though it were a fact.

        **The invariant a caller depends on:** a provider may decline *only
        before it has executed the submitted statement*. A declination is
        answered by executing that statement again on the eager path, and the
        statement is the reader's own -- ``execute_statements`` exists to serve
        DDL and calls -- so a declination raised after execution began would
        apply an insert, a delete or a definition twice. Once execution starts,
        every outcome is an execution outcome: this result, or a DatabaseError
        that propagates.

        Args:
            conn: Open connection handle.
            sql_text: The query to page, exactly as written.
            offset: Where the page starts. Validated by the provider.
            limit: The most rows the page may hold. Validated by the provider
                against its own maximum, so a caller cannot ask for the whole
                table by naming a large enough number.
            order_by: The ordering to apply, as column/direction mappings, or
                None to leave the statement's own ordering alone.
            filters: The filters to apply, as column/operator/value mappings,
                or None for an unfiltered read.

        Returns:
            One :class:`PageResult`.

        Raises:
            UnsupportedDatabaseCapabilityError: If this provider cannot page at
                all, or cannot page this statement. Raised before the statement
                is executed, never after.
            DatabaseError: If execution failed. A refusal is not a declination
                and must not be reported as one.
        """
        provider = self._provider_for_conn(conn)
        backend = _backend(provider)
        own = getattr(backend, "execute_page", None)
        if not callable(own):
            raise UnsupportedDatabaseCapabilityError(
                f"DBAdapter: provider '{provider}' cannot page a query result."
            )
        return own(
            conn,
            sql_text,
            offset=offset,
            limit=limit,
            order_by=order_by,
            filters=filters,
        )

    def query_rows(
        self,
        conn: Any,
        sql_text: str,
        *,
        limit: int | None = 1_000,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Run one read query and return its columns with at most ``limit`` rows.

        ``limit=None`` returns EVERY row, which is a different act from a
        larger number: a reader previewing a result asks for a bound, and a
        consumer that must have all of it -- a load reading its source -- has
        no bound to name. Naming a big one instead would be a guess that
        silently truncates when the result outgrows it.

        It is this rather than a second function because "read this query" is
        one act, and two spellings of it would be one mechanism implemented
        twice.

        NOT ``execute_page``. That is a reader's paging control and every
        provider caps it -- 500 rows -- so reading a whole source through it
        would be thousands of round trips against a bound designed to protect
        a grid.
        """
        try:
            provider = self._provider_for_conn(conn)
        except ConfigError:
            provider = ""
        if provider in {"postgres", "mysql"}:
            backend = _backend(provider)
            if hasattr(backend, "query_rows"):
                return backend.query_rows(conn, sql_text, limit=limit)

        cursor = None
        try:
            _logger.debug(
                "attempting dbapi read provider=%s limit=%s statement=%s",
                provider or "(unresolved)", limit, _glimpse(sql_text),
            )
            cursor = conn.cursor()
            cursor.execute(sql_text)
            columns = [str(column[0]) for column in (cursor.description or [])]
            if not columns:
                return columns, []
            values = (
                cursor.fetchall() if limit is None
                else cursor.fetchmany(max(1, int(limit)))
            )
            return columns, [dict(zip(columns, row)) for row in values]
        except Exception as exc:
            _logger.debug(
                "dbapi read failed for statement=%s", _glimpse(sql_text),
            )
            raise DatabaseError(f"DBAdapter: query failed: {exc}") from exc
        finally:
            if cursor is not None and hasattr(cursor, "close"):
                cursor.close()

    # ------------------------------------------------------------------
    # Provider-neutral metadata
    # ------------------------------------------------------------------

    def supports(self, conn: Any, capability: str) -> bool:
        """Return whether ``conn`` supports a declared metadata capability."""
        normalized = str(capability).strip().lower()
        if normalized not in _METADATA_CAPABILITIES:
            raise ConfigError(
                f"DBAdapter: unknown metadata capability '{capability}'. "
                f"Known capabilities: {sorted(_METADATA_CAPABILITIES)}."
            )
        provider = self._provider_for_conn(conn)
        return normalized in _PROVIDER_METADATA_CAPABILITIES.get(provider, frozenset())

    def supports_provider_capability(self, conn: Any, capability: str) -> bool:
        """Return whether ``conn``'s provider implements a declared contract method.

        Parameters
        ----------
        conn : Any
            Open backend connection.
        capability : str
            One of the declared provider-contract method names.

        **A QUESTION ALWAYS ANSWERS.** A provider whose module cannot be
        imported -- its driver is not installed in this environment -- is
        reported as not implementing anything, rather than raising the import
        failure out of something declared to return a bool. Otherwise the
        answer depends on what happens to be installed: the same provider
        would answer ``True``, ``False``, or raise.

        THREE OUTCOMES, KEPT APART BY THE EXCEPTION EACH ALREADY RAISES:

            unknown capability name    ConfigError   a TYPO -- see below
            unregistered provider      ConfigError   a CONFIGURATION mistake,
                                                     raised by ``_backend``
                                                     before any import
            registered, cannot import  ImportError   this installation cannot
                                                     do it -> False

        So no new error type is needed to tell a mistake from an absence.

        Returns
        -------
        bool
            ``True`` if this provider implements it. ``False`` when it does
            not, and when its module cannot be imported at all.

        Raises
        ------
        ConfigError
            If ``capability`` is not a declared name. An undeclared name can
            only be a typo, and answering ``False`` for it would read as
            "unsupported" and hide the mistake. Also if the provider itself is
            not registered, which is the same kind of mistake one level up.
        """
        normalized = str(capability).strip().lower()
        if normalized not in _PROVIDER_CONTRACT_CAPABILITIES:
            raise ConfigError(
                f"DBAdapter: unknown provider capability '{capability}'. "
                f"Known capabilities: {sorted(_PROVIDER_CONTRACT_CAPABILITIES)}."
            )
        provider = self._provider_for_conn(conn)
        try:
            backend = _backend(provider)
        except ImportError:
            # ImportError, NOT Exception. ModuleNotFoundError is a subclass, so
            # this catches an absent driver and nothing else -- a backend that
            # raises something else while importing is a FAULT rather than an
            # absence, and must keep propagating.
            #
            # Logged because the refusal built on this answer cannot say it.
            # `_require_provider_capability` asks this first, so it reports
            # "provider 'x' does not implement capability 'y'" -- which is not
            # what happened. The provider does implement it; its module will
            # not load. Without this line an operator goes looking for a
            # function that is already there.
            _logger.warning(
                "DBAdapter: provider '%s' is registered but its module could "
                "not be imported, so its capabilities are reported as "
                "unavailable. The driver it needs is not installed here.",
                provider, exc_info=True,
            )
            return False
        return getattr(backend, normalized, None) is not None

    def list_catalogs(self, conn: Any) -> list[dict[str, str]]:
        """Return the catalog/database to which ``conn`` is attached."""
        provider = self._require_metadata_capability(conn, "catalogs")
        catalog = str(_backend(provider).get_current_database(conn))
        return [{"name": catalog}]

    def list_schemas(self, conn: Any) -> list[dict[str, str]]:
        """Return normalized schemas visible to the current connection."""
        provider = self._require_metadata_capability(conn, "schemas")
        catalog = self._metadata_catalog(conn, provider)
        if provider in {"postgres", "mysql"}:
            from rey_lib.db._sqlalchemy import metadata_list_schemas

            # ORDERED HERE, whatever the provider answered (backlog 278).
            return sorted(metadata_list_schemas(conn, catalog), key=lambda item: item["name"])
        objects = _backend(provider).list_database_objects(conn, catalog)
        names = {
            str(obj.get("schema") or obj.get("name") or "")
            for obj in objects
            if str(obj.get("object_type", "")).lower().rstrip("s") == "schema"
        }
        return [
            {"catalog": catalog, "name": name}
            for name in sorted(name for name in names if name)
        ]

    def inspect_database_references(
        self,
        conn: Any,
        schema: str | None = None,
    ) -> dict[str, Any]:
        """Return everything a database index needs about this database.

        One capability rather than a menu of getters, and the reason it is here
        rather than imported directly: which provider answers is the adapter's
        to know. A caller that imported a provider module would name a database
        server in a layer that must not know one.

        Args:
            conn: An open connection.
            schema: One schema to confine the inspection to, or None for every
                non-system schema.

        Returns:
            ``{"objects": [...], "members": [...], "observations": [...]}`` in
            the database index's vocabulary.

        Raises:
            UnsupportedDatabaseCapabilityError: If this provider cannot answer.
                Reported rather than returning an empty inspection, which would
                publish "this database has nothing" as though it were a fact.
        """
        provider = self._provider_for_conn(conn)
        backend = _backend(provider)
        inspect = getattr(backend, "inspect_database_references", None)
        if inspect is None:
            raise UnsupportedDatabaseCapabilityError(
                f"DBAdapter: provider '{provider}' cannot inspect database "
                f"references."
            )
        return inspect(conn, schema)

    def list_procedures(
        self,
        conn: Any,
        schema: str | None = None,
    ) -> list[dict[str, str]]:
        """Return normalized procedures, optionally restricted to one schema.

        See :meth:`list_functions` for what "normalized" carries.
        """
        return self._list_routines(conn, "procedures", schema)

    def list_functions(
        self,
        conn: Any,
        schema: str | None = None,
    ) -> list[dict[str, str]]:
        """Return normalized functions, optionally restricted to one schema.

        Each routine carries ``schema``, ``name`` and ``signature`` -- the
        argument list the provider itself uses to tell one routine from its
        overloads.

        It **may** also carry ``invocation``: a statement that calls it, written
        the way that provider writes one. How a call is spelled, how its
        arguments are placed, how overloads are told apart and whether a routine
        returns a value or a set are all provider facts, so the provider renders
        the whole statement and no caller assembles one from the parts.

        **Optional, and absent is an ordinary answer.** A provider that renders
        none returns the key not at all, and a caller is left with no statement
        rather than one it built itself -- which is the right outcome, because a
        call composed by a caller that does not know the provider's rules can
        invoke the wrong routine rather than fail.

        Returns:
            One dict per routine: ``schema``, ``name``, ``signature``, and
            ``invocation`` where the provider renders one.
        """
        return self._list_routines(conn, "functions", schema)

    def get_schema_metadata(
        self, conn: Any, schema: str, *, database_name: str = "",
    ) -> dict[str, Any]:
        """Return one schema's structure facts, in one shape for every provider.

        A dispatch to the provider's ``schema_metadata`` primitive (backlog
        725): every table with its primary key, foreign keys, indexes, unique
        and check constraints, and every view, by name. Facts only --
        DatabaseSchema.read builds the objects. No column facts (those are
        table_column_catalog's) and no view definitions (DatabaseView.read_ddl's).

        Args:
            conn: Open connection.
            schema: The schema to read.
            database_name: The database the schema sits in, where the provider
                can read one other than the connected database (SQL Server);
                empty for the connected one (backlog 727).

        Returns:
            ``{"tables": [...], "views": [...]}``, each in name order
            (backlog 278).

        Raises:
            UnsupportedDatabaseCapabilityError: If the provider does not answer this.
        """
        # The provider answers the structure facts (backlog 725); this only
        # dispatches and keeps the common contract and order.
        read = self._require_provider_capability(conn, "schema_metadata")
        inspected = read(conn, schema, database_name=database_name)
        # ORDERED HERE, whatever the provider answered (backlog 278).
        return {
            "tables": sorted(inspected.get("tables") or [], key=lambda item: item["name"]),
            "views": sorted(inspected.get("views") or [], key=lambda item: item["name"]),
        }

    def _require_metadata_capability(self, conn: Any, capability: str) -> str:
        provider = self._provider_for_conn(conn)
        if not self.supports(conn, capability):
            raise UnsupportedDatabaseCapabilityError(
                f"DBAdapter: provider '{provider}' does not support metadata "
                f"capability '{capability}'."
            )
        return provider

    def _require_provider_capability(self, conn: Any, capability: str) -> Any:
        """Return the provider's function for ``capability``, or refuse by name.

        Every provider-contract dispatch goes through here. Declaring the gap
        would not be enough on its own: while the call still reached
        ``_backend(provider).method(...)`` it would raise AttributeError
        exactly as before, with a correct declaration sitting beside it unread.
        """
        provider = self._provider_for_conn(conn)
        if not self.supports_provider_capability(conn, capability):
            raise UnsupportedDatabaseCapabilityError(
                f"DBAdapter: provider '{provider}' does not implement provider "
                f"capability '{capability}'."
            )
        return getattr(_backend(provider), capability)

    def _metadata_catalog(self, conn: Any, provider: str) -> str:
        return str(_backend(provider).get_current_database(conn))

    def _list_routines(
        self,
        conn: Any,
        capability: str,
        schema: str | None,
    ) -> list[dict[str, Any]]:
        """Return Rey-owned routine records for one capability.

        The signature is the provider's identity argument list, normalized to a
        string. It is empty for a routine taking no arguments, and it is what
        keeps overloaded routines distinguishable — a routine is identified by
        catalog, schema, name, object_type and signature together, never by
        name alone.

        ``invocation`` rides along untouched where the provider rendered one,
        and ``parameters`` where the provider listed them (backlog 747).
        This layer normalizes identity; it does not read, validate or rewrite a
        statement, because it does not know the syntax the statement is in.
        """
        provider = self._require_metadata_capability(conn, capability)
        catalog = self._metadata_catalog(conn, provider)
        kind = _ROUTINE_KINDS[capability]
        rows = _backend(provider).list_routines(conn, catalog, schema, kind)
        result = []
        for row in rows:
            record = {
                "catalog": str(catalog),
                "schema": str(row.get("schema") or ""),
                "name": str(row.get("name") or ""),
                "object_type": kind,
                "signature": str(row.get("signature") or ""),
            }
            # Carried through exactly as rendered, and only when rendered. The
            # key is added rather than defaulted, so a provider that renders no
            # call is distinguishable here from one that rendered an empty one.
            invocation = row.get("invocation")
            if invocation:
                record["invocation"] = str(invocation)
            # The routine's parameters (backlog 747), carried as the provider
            # listed them and only where it listed them, like the call.
            parameters = row.get("parameters")
            if parameters is not None:
                record["parameters"] = [dict(one) for one in parameters]
            result.append(record)
        return sorted(
            result,
            key=lambda item: (item["schema"], item["name"], item["signature"]),
        )

    # ------------------------------------------------------------------
    # Stored procedures
    # ------------------------------------------------------------------

    def call_proc(
        self,
        conn: Any,
        proc_name: str,
        params: Optional[list[Any]] = None,
    ) -> Any:
        """
        Execute a stored procedure by name with positional parameters.

        Parameters
        ----------
        conn : Any
            Open backend connection.
        proc_name : str
            Fully-qualified procedure name.
        params : Optional[list[Any]]
            Positional input parameters; ``None`` for zero-parameter procs.

        Returns
        -------
        Any
            Backend-specific cursor or result handle.

        Raises
        ------
        UnsupportedDatabaseCapabilityError
            If the connection's provider has no stored-procedure support.
        """
        call = self._require_provider_capability(conn, "call_proc")
        return call(conn, proc_name, params)

    def call_proc_with_output(
        self,
        conn: Any,
        proc_name: str,
        named_inputs: list[tuple[str, Any]],
        output_specs: list[tuple[str, str]],
    ) -> dict[str, Any]:
        """
        Execute a stored procedure and capture named OUTPUT parameter values.

        Parameters
        ----------
        conn : Any
            Open backend connection.
        proc_name : str
            Fully-qualified procedure name.
        named_inputs : list[tuple[str, Any]]
            Input parameters as ``(name, value)`` pairs in declaration order.
        output_specs : list[tuple[str, str]]
            Output parameters as ``(name, sql_type)`` pairs.

        Returns
        -------
        dict[str, Any]
            Output parameter values keyed by parameter name.

        Raises
        ------
        UnsupportedDatabaseCapabilityError
            If the connection's provider has no stored-procedure support.
        """
        call = self._require_provider_capability(conn, "call_proc_with_output")
        return call(conn, proc_name, named_inputs, output_specs)

    # ------------------------------------------------------------------
    # Function and procedure execution (PostgreSQL control database)
    # ------------------------------------------------------------------

    def call_routine(self, conn: Any, call: RoutineCall) -> Any:
        """Execute one normalized routine call on whichever provider this is.

        One dispatch, not one per statement form. What the call *is* was
        decided by the connector; this only finds the backend that speaks the
        dialect and hands it over. A provider that cannot render a routine call
        says so by name rather than by a missing attribute.
        """
        provider = self._provider_for_conn(conn)
        backend = _backend(provider)
        render = getattr(backend, "render_and_execute", None)
        if render is None:
            raise UnsupportedDatabaseCapabilityError(
                f"DBAdapter: provider '{provider}' renders no routine calls."
            )
        return render(conn, call)

    # ------------------------------------------------------------------
    # Staging / bulk insert
    # ------------------------------------------------------------------

    # -- connector primitives (backlog 690) ------------------------------------
    # What database objects need to reach a database: which provider a
    # connection is, how it spells an identifier, and running one statement or
    # one read. Table and type semantics are the objects' own, not these.

    def provider(self, conn: Any) -> str:
        """Which provider an open connection belongs to."""
        return self._provider_for_conn(conn)

    def quote_identifier(self, conn: Any, name: str) -> str:
        """One identifier, quoted the way this connection's provider writes it.

        Raises:
            UnsupportedDatabaseCapabilityError: If the provider cannot.
        """
        return self._require_provider_capability(conn, "quote_identifier")(name)

    def execute_statement(self, conn: Any, sql_text: str) -> int:
        """Run one statement on the connection; the provider's affected count. No commit.

        Raises:
            UnsupportedDatabaseCapabilityError: If the provider cannot.
        """
        return self._require_provider_capability(conn, "execute_statement")(conn, sql_text)

    def table_column_catalog(
        self, conn: Any, schema: str, table: str, *, database_name: str = "",
    ) -> list[dict[str, Any]]:
        """One table's columns as the provider's catalog states them.

        Name, type, default and nullability per column. A metadata primitive,
        not DDL. Empty when there is no such table.

        ``database_name`` names the database the table sits in, where the
        provider can read one other than the connected database (backlog 727).

        Raises:
            UnsupportedDatabaseCapabilityError: If the provider does not answer this.
        """
        read = self._require_provider_capability(conn, "table_column_catalog")
        return read(conn, schema, table, database_name=database_name)

    def list_database_objects(
        self, conn: Any, database: str | None = None,
    ) -> list[dict[str, Any]]:
        """Which objects the database holds, with what each depends on.

        A metadata primitive (backlog 723): the provider's inventory, as it
        states it. Nothing above the database objects reads it -- each canonical
        type claims its own entries (DatabaseObjectListing), and an entry no
        type claims is refused there.

        Raises:
            UnsupportedDatabaseCapabilityError: If the provider does not answer this.
        """
        read = self._require_provider_capability(conn, "list_database_objects")
        return read(conn, database=database)

    def table_exists(self, conn: Any, catalog: str, schema: str, table: str) -> bool:
        """Whether the provider's catalog holds a table of this schema and name.

        A metadata primitive, not what "exists" means -- that is DatabaseTable's.

        Raises:
            UnsupportedDatabaseCapabilityError: If the provider does not answer this.
        """
        read = self._require_provider_capability(conn, "table_exists")
        return bool(read(conn, catalog, schema, table))

    def view_native_definition(self, conn: Any, schema: str, view: str) -> str:
        """A view's definition exactly as the destination database supplies it.

        Retrieved, never generated -- for some providers a full statement, for
        PostgreSQL the SELECT body. Turning it into DDL is DatabaseView's. Empty
        when there is none.

        Raises:
            UnsupportedDatabaseCapabilityError: If the provider does not answer this.
        """
        read = self._require_provider_capability(conn, "view_native_definition")
        return str(read(conn, schema, view))

    def table_native_definition(self, conn: Any, schema: str, table: str) -> str:
        """The complete table definition the destination database itself supplies.

        Retrieved, never generated: the server's own statement, as it reports
        it. Empty when there is none.

        Raises:
            UnsupportedDatabaseCapabilityError: If the provider does not answer this.
        """
        read = self._require_provider_capability(conn, "table_native_definition")
        return read(conn, schema, table)

    def index_column_catalog(
        self, conn: Any, schema: str, table: str, index: str, *, database_name: str = "",
    ) -> dict[str, Any]:
        """One index on one table as the provider's catalog states it.

        Whether it is unique, whether it backs the primary key, and its key
        columns in order. A metadata primitive, not DDL. Empty when the table
        has no such index.

        ``database_name`` names the database the table sits in, where the
        provider can read one other than the connected database (backlog 727).

        Raises:
            UnsupportedDatabaseCapabilityError: If the provider does not answer this.
        """
        read = self._require_provider_capability(conn, "index_column_catalog")
        return read(conn, schema, table, index, database_name=database_name)

    def index_native_definition(self, conn: Any, schema: str, table: str, index: str) -> str:
        """The index definition the destination database itself supplies.

        Retrieved, never generated: the server's own statement, addressed by
        schema, table and index name. Empty when there is none.

        Raises:
            UnsupportedDatabaseCapabilityError: If the provider does not answer this.
        """
        read = self._require_provider_capability(conn, "index_native_definition")
        return read(conn, schema, table, index)

    def constraint_catalog(
        self, conn: Any, schema: str, table: str, name: str, *, database_name: str = "",
    ) -> dict[str, Any]:
        """One constraint on one table as the provider's catalog states it.

        Its kind in Rey's words, its columns, what they reference, and a
        check's expression. A metadata primitive, not DDL. Empty when the
        table has no such constraint.

        ``database_name`` names the database the table sits in, where the
        provider can read one other than the connected database (backlog 727).

        Raises:
            UnsupportedDatabaseCapabilityError: If the provider does not answer this.
        """
        read = self._require_provider_capability(conn, "constraint_catalog")
        return read(conn, schema, table, name, database_name=database_name)

    def constraint_native_definition(
        self, conn: Any, schema: str, table: str, name: str,
    ) -> str:
        """A constraint's definition as the destination database itself supplies it.

        Retrieved, never generated: the server's own clause, addressed by
        schema, table and constraint name. Empty when there is none.

        Raises:
            UnsupportedDatabaseCapabilityError: If the provider does not answer this.
        """
        read = self._require_provider_capability(conn, "constraint_native_definition")
        return read(conn, schema, table, name)

    def user_type_catalog(self, conn: Any, schema: str, name: str) -> dict[str, Any]:
        """One user-defined type as the provider's catalog states it.

        Its kind, base type, default, whether it is NOT NULL, and an enum's
        labels in order. A metadata primitive, not DDL. Empty when there is no
        such type.

        Raises:
            UnsupportedDatabaseCapabilityError: If the provider does not answer this.
        """
        read = self._require_provider_capability(conn, "user_type_catalog")
        return read(conn, schema, name)

    def sequence_catalog(self, conn: Any, schema: str, name: str) -> dict[str, Any]:
        """One sequence as the provider's catalog states it.

        Start, increment, minimum, maximum and whether it cycles. A metadata
        primitive, not DDL. Empty when there is no such sequence.

        Raises:
            UnsupportedDatabaseCapabilityError: If the provider does not answer this.
        """
        read = self._require_provider_capability(conn, "sequence_catalog")
        return read(conn, schema, name)

    def sequence_native_definition(self, conn: Any, schema: str, name: str) -> str:
        """The sequence definition the destination database itself supplies.

        Retrieved, never generated: the server's own statement, addressed by
        schema and name. Empty when there is none.

        Raises:
            UnsupportedDatabaseCapabilityError: If the provider does not answer this.
        """
        read = self._require_provider_capability(conn, "sequence_native_definition")
        return str(read(conn, schema, name))

    def trigger_native_definition(self, conn: Any, schema: str, table: str, name: str) -> str:
        """One trigger's definition as the destination database itself supplies it.

        A retrieval primitive, not a trigger owner: the server's stored
        statement, addressed by the trigger's table and its name. Empty when
        the table has no such trigger.

        Raises:
            UnsupportedDatabaseCapabilityError: If the provider has no triggers.
        """
        read = self._require_provider_capability(conn, "trigger_native_definition")
        return str(read(conn, schema, table, name))

    def routine_native_definition(
        self, conn: Any, schema: str, name: str, signature: str, kind: str,
    ) -> str:
        """One routine's definition as the destination database itself supplies it.

        A retrieval primitive, not a routine owner: the server's own source,
        addressed by Rey's routine identity (schema, name, signature) and the
        ``kind`` of routine asked for. Terminators and presentation are the
        database object's.

        Raises:
            UnsupportedDatabaseCapabilityError: If the provider has no routines.
            DatabaseError: When there is no such routine.
        """
        read = self._require_provider_capability(conn, "get_routine_definition")
        return str(read(conn, "", schema, name, signature, kind))

    def get_table_columns(
        self,
        conn: Any,
        schema: str,
        table: str,
    ) -> list[str]:
        """Return table columns in ordinal order for the connection backend.

        An empty list means the table does not exist -- a table that exists
        always has at least one column.

        Raises
        ------
        UnsupportedDatabaseCapabilityError
            If the connection's provider cannot describe a table.
        """
        describe = self._require_provider_capability(conn, "get_table_columns")
        return describe(conn, schema, table)

    def bulk_insert(
        self,
        conn: Any,
        schema: str,
        table: str,
        rows: list[dict[str, Any]],
        columns: list[str],
    ) -> int:
        """
        Insert ``rows`` into ``schema.table`` using the most efficient bulk
        mechanism for the backend.

        Parameters
        ----------
        conn : Any
            Open backend connection.
        schema : str
            Target schema (or database.schema).
        table : str
            Target table name.
        rows : list[dict[str, Any]]
            Row dicts; keys must include every entry of ``columns``.
        columns : list[str]
            Column names defining the insert order.

        Returns
        -------
        int
            Number of rows inserted.

        Raises
        ------
        UnsupportedDatabaseCapabilityError
            If the connection's provider cannot bulk insert.
        """
        insert_rows = self._require_provider_capability(conn, "bulk_insert")
        return insert_rows(conn, schema, table, rows, columns)

    def insert(self, conn: Any, schema: str, table: str, row: dict[str, Any]) -> Any:
        """Insert one row; the inserted row's primary-key value, as the provider returns it.

        Raises:
            UnsupportedDatabaseCapabilityError: If the provider cannot.
        """
        return self._require_provider_capability(conn, "insert")(conn, schema, table, row)

    def update(
        self,
        conn: Any,
        schema: str,
        table: str,
        values: dict[str, Any],
        criteria: dict[str, Any],
    ) -> int:
        """Set ``values`` on the rows matching ``criteria``; the rows affected.

        Raises:
            UnsupportedDatabaseCapabilityError: If the provider cannot.
        """
        return self._require_provider_capability(conn, "update")(
            conn, schema, table, values, criteria,
        )

    def delete(
        self, conn: Any, schema: str, table: str, criteria: dict[str, Any],
    ) -> int:
        """Remove the rows matching ``criteria``; the rows affected.

        Raises:
            UnsupportedDatabaseCapabilityError: If the provider cannot.
        """
        return self._require_provider_capability(conn, "delete")(conn, schema, table, criteria)

    def insert_from_path(
        self,
        conn: Any,
        schema: str,
        table: str,
        source_path: Any,
        columns: list[str],
    ) -> int:
        """Insert into ``schema.table`` straight from a file the provider reads.

        The non-row equivalent of ``bulk_insert``: the same rows arrive, read
        by the engine over the file rather than carried through this process.

        **ASK BEFORE CALLING.** Unlike every other method here this is
        OPTIONAL -- ``supports_provider_capability(conn, "insert_from_path")``
        answers whether this connection's provider has it, and a caller that
        does not ask gets the same refusal any missing capability raises. The
        record-based path stays the guaranteed one.

        Parameters
        ----------
        conn : Any
            Open backend connection.
        schema : str
            Target schema (or database.schema).
        table : str
            Target table name.
        source_path : Any
            The file to read. What the provider can read is the provider's
            answer; nothing here interprets the path.
        columns : list[str]
            Column names defining the insert order.

        Returns
        -------
        int
            Rows inserted, as the ENGINE counted them. Zero means the source
            held no rows -- a real answer, not a failure.

        Raises
        ------
        UnsupportedDatabaseCapabilityError
            If the connection's provider cannot insert from a path.
        """
        insert_native = self._require_provider_capability(
            conn, "insert_from_path",
        )
        return insert_native(conn, schema, table, source_path, columns)

    def is_truncation_error(self, exc: Exception) -> bool:
        """
        Return ``True`` if ``exc`` is a backend-recognized truncation error.

        Iterates all registered backends — the first one to claim the
        exception wins. Backends that do not have a truncation concept
        return ``False``. This method never raises.

        Parameters
        ----------
        exc : Exception
            The exception raised by a prior ``bulk_insert`` call.

        Returns
        -------
        bool
            ``True`` if the exception is a recoverable truncation error.
        """
        for provider in _REGISTRY:
            try:
                if _backend(provider).is_truncation_error(exc):
                    return True
            except Exception:  # noqa: BLE001 — backend module unavailable
                pass
        return False

    # ------------------------------------------------------------------
    # Private — provider resolution
    # ------------------------------------------------------------------

    def _provider_for_cfg(self, db_cfg: Any) -> str:
        """
        Resolve provider name from a connection config.

        Honors ``db_cfg.provider`` when present (case-insensitive). Falls back
        to inferring from ``db_cfg.driver`` so older configs that pre-date the
        provider field keep working.

        Parameters
        ----------
        db_cfg : Any
            Connection config Namespace.

        Returns
        -------
        str
            Lowercase provider name.

        Raises
        ------
        ConfigError
            When neither ``provider`` nor a recognizable ``driver`` is set.
        """
        provider = (getattr(db_cfg, "provider", None) or "").strip().lower()

        if provider:
            return provider

        driver = (getattr(db_cfg, "driver", None) or "").lower()

        if "sql server" in driver:
            return "sqlserver"

        if "mysql" in driver:
            return "mysql"

        if "duckdb" in driver:
            return "duckdb"

        name = getattr(db_cfg, "name", "<unnamed>")

        raise ConfigError(
            f"DBAdapter: connection '{name}' has no 'provider' field and "
            "no recognizable 'driver'. Add 'provider' to the connection config."
        )

    def _provider_for_conn(self, conn: Any) -> str:
        """
        Resolve provider name from an already-open connection object.

        Checks for an explicit ``provider`` attribute first, then falls back
        to matching the connection's module name against ``_MODULE_PREFIXES``.

        Parameters
        ----------
        conn : Any
            An open backend connection.

        Returns
        -------
        str
            Lowercase provider name.

        Raises
        ------
        ConfigError
            When the provider cannot be determined.
        """
        explicit = getattr(conn, "provider", None)

        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip().lower()

        module = type(conn).__module__ or ""

        for prefix, provider in _MODULE_PREFIXES.items():
            if module.startswith(prefix):
                return provider

        raise ConfigError(
            f"DBAdapter: cannot determine provider for connection of type "
            f"{type(conn).__name__} (module={module!r}). "
            f"Add an entry to _MODULE_PREFIXES in db_adapter.py."
        )

