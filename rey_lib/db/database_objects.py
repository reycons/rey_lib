"""What a database holds, as objects rather than as rows of metadata.

A schema read answers with these: a schema holding tables and views, each table
or view carrying its own identity, what it is made of, the SQL that created it
and the statement that opens it. Everything downstream consumes the object
instead of rediscovering what it describes.

Concrete classes, no base class and no marker types. A table is a
``DatabaseTable`` and a view is a ``DatabaseView`` -- the class says which, so
nothing carries a type discriminator for other code to branch on.
``DatabaseObjectIdentity`` is a field these objects have rather than something
they are: a table *has* an identity, it is not one.

They are values, and they hold no connection. ``DatabaseTable`` is also the
canonical table object (backlog 690): it owns what a table's existence means,
creating one from a Rey logical schema, and widening a column -- the table DDL
and the provider differences in it -- reaching the database only through the
connector's primitives (``DBAdapter.provider`` / ``quote_identifier`` /
``execute_statement`` / ``fetch_rows``), with every column type from
``DatabaseType``. An operation is handed the connection; the object never keeps
one, so the same value is what the Console's tree draws.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from rey_lib.data.logical_type import LogicalType
from rey_lib.db.database_type import DatabaseType
from rey_lib.errors.error_utils import DatabaseError

__all__ = [
    "DatabaseColumn",
    "DatabaseFunction",
    "DatabaseConstraint",
    "DatabaseForeignKey",
    "DatabaseIndex",
    "DatabaseObjectIdentity",
    "DatabasePrimaryKey",
    "DatabaseProcedure",
    "DatabaseRoutineIdentity",
    "DatabaseSchema",
    "DatabaseTable",
    "DatabaseView",
]


@dataclass(frozen=True)
class DatabaseObjectIdentity:
    """Which object, said in the vocabulary a connection answered in.

    Four names, kept apart. Never a rendered SQL reference: how an object is
    written into a statement belongs to the provider, and a caller holding this
    holds an identity rather than a piece of SQL.
    """

    connection: str
    catalog: str
    schema: str
    name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "connection": self.connection,
            "catalog": self.catalog,
            "schema": self.schema,
            "name": self.name,
        }


@dataclass(frozen=True)
class DatabaseRoutineIdentity:
    """Which routine, including the arguments that tell it from its overloads.

    A routine is not identified by name alone: a database may hold several of
    one name, differing only in what they take. The signature is the argument
    list the provider itself uses to name one among them, so two overloads are
    two objects and the identity is what says so.

    Kept apart from :class:`DatabaseObjectIdentity` rather than adding a
    signature to it: a table has no signature, and a routine is not identified
    without one.
    """

    connection: str
    catalog: str
    schema: str
    name: str
    signature: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "connection": self.connection,
            "catalog": self.catalog,
            "schema": self.schema,
            "name": self.name,
            "signature": self.signature,
        }


@dataclass(frozen=True)
class DatabaseColumn:
    """One column, in the position the provider reported it."""

    name: str
    type: str
    nullable: bool
    default: str | None
    ordinal: int
    ddl: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "nullable": self.nullable,
            "default": self.default,
            "ordinal": self.ordinal,
            "ddl": self.ddl,
        }


@dataclass(frozen=True)
class DatabasePrimaryKey:
    """A relation's primary key. Its columns are ordered and that order means something."""

    name: str
    columns: tuple[str, ...] = ()
    ddl: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "columns": list(self.columns), "ddl": self.ddl}


@dataclass(frozen=True)
class DatabaseForeignKey:
    """One foreign key: local columns, and what they point at.

    The target is named the same way anything else is -- by schema and name --
    so a reader can find it without being handed a second identity object for a
    relation this one does not own.
    """

    name: str
    columns: tuple[str, ...] = ()
    referenced_schema: str = ""
    referenced_table: str = ""
    referenced_columns: tuple[str, ...] = ()
    ddl: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "columns": list(self.columns),
            "referenced_schema": self.referenced_schema,
            "referenced_table": self.referenced_table,
            "referenced_columns": list(self.referenced_columns),
            "ddl": self.ddl,
        }


@dataclass(frozen=True)
class DatabaseIndex:
    """One index, and whether it enforces uniqueness."""

    name: str
    columns: tuple[str, ...] = ()
    unique: bool = False
    ddl: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "columns": list(self.columns),
            "unique": self.unique,
            "ddl": self.ddl,
        }


@dataclass(frozen=True)
class DatabaseConstraint:
    """One constraint, in the provider's own word for what kind it is."""

    name: str
    columns: tuple[str, ...] = ()
    kind: str = ""
    ddl: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "columns": list(self.columns),
            "kind": self.kind,
            "ddl": self.ddl,
        }


@dataclass(frozen=True)
class DatabaseTable:
    """One table: what it is, what it is made of, and how it opens.

    ``ddl`` and ``initial_select`` are written when the object is constructed,
    from the same read that produced its columns. They are values it carries,
    not questions it answers later.
    """

    identity: DatabaseObjectIdentity
    columns: tuple[DatabaseColumn, ...] = ()
    primary_key: DatabasePrimaryKey | None = None
    foreign_keys: tuple[DatabaseForeignKey, ...] = ()
    indexes: tuple[DatabaseIndex, ...] = ()
    constraints: tuple[DatabaseConstraint, ...] = ()
    ddl: str = ""
    initial_select: str = ""

    @classmethod
    def exists(
        cls, conn: Any, identity: DatabaseObjectIdentity, adapter: Any = None,
    ) -> bool:
        """Whether this table exists on the connection.

        What "exists" means is the table's: one row in the provider's
        ``information_schema.tables`` for this schema and name -- for SQL
        Server, the named database's own when the identity carries one. Asked
        directly, not as "has columns", so "no such table" is never confused
        with "no columns".
        """
        adapter = adapter or _adapter()
        catalog_prefix = (
            f"{adapter.quote_identifier(conn, identity.catalog)}."
            if identity.catalog and adapter.provider(conn) == "sqlserver" else ""
        )
        rows = adapter.fetch_rows(
            conn,
            f"SELECT 1 AS present FROM {catalog_prefix}information_schema.tables "
            "WHERE table_schema = :schema AND table_name = :table",
            {"schema": identity.schema, "table": identity.name},
        )
        return bool(rows)

    @classmethod
    def create(
        cls,
        conn: Any,
        identity: DatabaseObjectIdentity,
        columns: list[tuple[str, LogicalType]],
        adapter: Any = None,
    ) -> bool:
        """Create this table from a Rey logical schema, where it is not already there.

        Every column is nullable, typed by ``DatabaseType`` for the
        connection's provider, in the order given. An existing table is left
        untouched.

        Commits where the provider's DDL is not part of the load's transaction
        (SQL Server, MySQL), as table creation always has there. PostgreSQL
        and DuckDB DDL is transactional, so a table made for a load that then
        fails goes away with it.

        Returns:
            True if this call created it; False if it was already there.

        Raises:
            DatabaseError: If no columns are given, or the DDL fails.
        """
        adapter = adapter or _adapter()
        if not columns:
            raise DatabaseError(
                f"DatabaseTable.create: no columns given for {_named(identity)}."
            )
        if cls.exists(conn, identity, adapter):
            return False
        provider = adapter.provider(conn)
        types = DatabaseType(provider)
        nullable = "" if provider == "duckdb" else " NULL"
        listed = ",\n\t".join(
            f"{adapter.quote_identifier(conn, name)} {types.column_type(logical)}{nullable}"
            for name, logical in columns
        )
        table = _qualified(adapter, conn, identity)
        _run(adapter, conn, identity, f"CREATE TABLE {table} (\n\t{listed}\n)")
        if provider in _COMMITS_TABLE_DDL:
            conn.commit()
        return True

    def widen_column(
        self, conn: Any, column: str, logical: LogicalType, adapter: Any = None,
    ) -> None:
        """Change one existing column to this logical type, in place.

        The ALTER is this provider's own form, and the column type is
        ``DatabaseType``'s -- the same translation ``create`` uses.
        Nullability is kept as every created column has it.

        Raises:
            DatabaseError: If the provider has no ALTER here, or it fails.
        """
        adapter = adapter or _adapter()
        provider = adapter.provider(conn)
        quoted = adapter.quote_identifier(conn, column)
        new_type = DatabaseType(provider).column_type(logical)
        change = {
            "postgres": f"ALTER COLUMN {quoted} TYPE {new_type}",
            "mysql": f"MODIFY COLUMN {quoted} {new_type} NULL",
            "sqlserver": f"ALTER COLUMN {quoted} {new_type} NULL",
            "duckdb": f"ALTER COLUMN {quoted} TYPE {new_type}",
        }.get(provider)
        if change is None:
            raise DatabaseError(
                f"DatabaseTable.widen_column: provider '{provider}' cannot alter a column."
            )
        table = _qualified(adapter, conn, self.identity)
        _run(adapter, conn, self.identity, f"ALTER TABLE {table} {change}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "columns": [column.to_dict() for column in self.columns],
            "primary_key": None if self.primary_key is None else self.primary_key.to_dict(),
            "foreign_keys": [key.to_dict() for key in self.foreign_keys],
            "indexes": [index.to_dict() for index in self.indexes],
            "constraints": [constraint.to_dict() for constraint in self.constraints],
            "ddl": self.ddl,
            "initial_select": self.initial_select,
        }


@dataclass(frozen=True)
class DatabaseView:
    """One view: its columns, its definition, and how it opens.

    A view is columns and a definition. It carries no keys, indexes or
    constraints, because a view has none to carry.
    """

    identity: DatabaseObjectIdentity
    columns: tuple[DatabaseColumn, ...] = ()
    ddl: str = ""
    initial_select: str = ""

    @classmethod
    def read_ddl(
        cls, conn: Any, identity: DatabaseObjectIdentity, adapter: Any = None,
    ) -> str:
        """This view's own DDL, as its provider writes it (backlog 695).

        View DDL is the view's: each provider's way of producing it lives here,
        reaching the database only through the connector's primitives. The DDL
        export asks this for a view; reading a schema does too.

        Raises:
            DatabaseError: If the provider has no view DDL here, or the read fails.
        """
        adapter = adapter or _adapter()
        provider = adapter.provider(conn)
        # schema.view, as view DDL has always been written: the catalog a read
        # carries is the connection's own database, not part of the statement.
        qualified = ".".join(
            adapter.quote_identifier(conn, part) for part in (identity.schema, identity.name)
        )
        names = {"schema": identity.schema, "view": identity.name}
        if provider == "postgres":
            rows = adapter.fetch_rows(
                conn,
                "SELECT pg_get_viewdef(c.oid, true) AS definition "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema AND c.relname = :view",
                names,
            )
            definition = rows[0]["definition"] if rows else ""
            return f"CREATE OR REPLACE VIEW {qualified} AS\n{definition};"
        if provider == "mysql":
            rows = adapter.fetch_rows(conn, f"SHOW CREATE VIEW {qualified}", {})
            return str((rows[0] if rows else {}).get("Create View") or "").rstrip() + ";"
        if provider == "sqlserver":
            rows = adapter.fetch_rows(
                conn,
                "SELECT OBJECT_DEFINITION(OBJECT_ID(:qualified)) AS definition",
                {"qualified": qualified},
            )
            definition = rows[0]["definition"] if rows else None
            return str(definition).rstrip() + "\nGO" if definition else ""
        if provider == "duckdb":
            # The catalog's own statement. SHOW CREATE VIEW, used before this
            # moved here, is not DuckDB syntax and produced nothing.
            rows = adapter.fetch_rows(
                conn,
                "SELECT sql FROM duckdb_views() "
                "WHERE schema_name = :schema AND view_name = :view",
                names,
            )
            return str(rows[0]["sql"]).rstrip() if rows else ""
        raise DatabaseError(f"DatabaseView.read_ddl: provider '{provider}' has no view DDL.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "columns": [column.to_dict() for column in self.columns],
            "ddl": self.ddl,
            "initial_select": self.initial_select,
        }


@dataclass(frozen=True)
class DatabaseProcedure:
    """One procedure: which one it is, the SQL that created it, and the call.

    What a call looks like is still not this object's to decide -- it carries
    the one its provider rendered. How a call is spelled, how its arguments are
    placed and how overloads are told apart differ by provider, so the object
    holds the answer rather than the rules.

    ``invocation`` is empty where the provider renders none, and that is an
    ordinary state: no statement is safer than one composed by something that
    does not know the provider's rules.
    """

    identity: DatabaseRoutineIdentity
    ddl: str = ""
    invocation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "ddl": self.ddl,
            "invocation": self.invocation,
        }


@dataclass(frozen=True)
class DatabaseFunction:
    """One function, identified as a procedure is: by name and arguments.

    Carries its provider's rendered call for the same reason, and on the same
    terms -- empty where none was rendered.
    """

    identity: DatabaseRoutineIdentity
    ddl: str = ""
    invocation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "ddl": self.ddl,
            "invocation": self.invocation,
        }


@dataclass(frozen=True)
class DatabaseSchema:
    """One schema, as the objects it holds.

    The unit a read answers with: a schema is opened once and everything under
    it arrives together.
    """

    connection: str
    catalog: str
    schema: str
    tables: tuple[DatabaseTable, ...] = ()
    views: tuple[DatabaseView, ...] = ()
    procedures: tuple[DatabaseProcedure, ...] = ()
    functions: tuple[DatabaseFunction, ...] = ()

    @classmethod
    def read(
        cls,
        conn: Any,
        connection: str,
        catalog: str,
        schema: str,
        adapter: Any = None,
    ) -> "DatabaseSchema":
        """Read one schema as the objects it holds (backlog 684).

        Tables with their columns, keys, indexes and constraints, views with
        their columns, procedures and functions -- each carrying its own DDL
        and the statement that opens it, relations listed by name.

        Args:
            conn: Open connection to read on; not closed here.
            connection: The configured connection's name, carried in each identity.
            catalog: Which catalog the schema sits in, as the caller knows it.
            schema: The schema to read.

        Raises:
            DatabaseError: When the schema cannot be read. A failed read raises
                rather than returning a half-built schema.
        """
        adapter = adapter or _adapter()
        try:
            read = adapter.get_schema_metadata(conn, schema)
        except Exception as exc:
            raise DatabaseError(f"The schema '{schema}' could not be read: {exc}") from exc
        tables = _ordered(
            _table(adapter, conn, connection, catalog, schema, entry)
            for entry in read.get("tables") or []
            if str(entry.get("name") or "")
        )
        views = _ordered(
            _view(adapter, conn, connection, catalog, schema, entry)
            for entry in read.get("views") or []
            if str(entry.get("name") or "")
        )
        return cls(
            connection=connection,
            catalog=catalog,
            schema=schema,
            tables=tables,
            views=views,
            procedures=tuple(_routines(
                adapter, conn, connection, catalog, schema, "procedures", DatabaseProcedure,
            )),
            functions=tuple(_routines(
                adapter, conn, connection, catalog, schema, "functions", DatabaseFunction,
            )),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "connection": self.connection,
            "catalog": self.catalog,
            "schema": self.schema,
            "tables": [table.to_dict() for table in self.tables],
            "views": [view.to_dict() for view in self.views],
            "procedures": [procedure.to_dict() for procedure in self.procedures],
            "functions": [function.to_dict() for function in self.functions],
        }


#: Providers whose table DDL is not part of the surrounding transaction, so a
#: created table is committed as it always has been there.
_COMMITS_TABLE_DDL: frozenset[str] = frozenset({"sqlserver", "mysql"})


def _adapter() -> Any:
    """The connector, constructed where an operation is not handed one."""
    from rey_lib.db.db_adapter import DBAdapter  # noqa: PLC0415 -- the adapter imports widely

    return DBAdapter()


def _named(identity: DatabaseObjectIdentity) -> str:
    """The identity's parts, joined, for a message."""
    return ".".join(part for part in (identity.catalog, identity.schema, identity.name) if part)


def _qualified(adapter: Any, conn: Any, identity: DatabaseObjectIdentity) -> str:
    """The table as one DDL reference: each part quoted, catalog where it is given."""
    return ".".join(
        adapter.quote_identifier(conn, part)
        for part in (identity.catalog, identity.schema, identity.name) if part
    )


def _run(adapter: Any, conn: Any, identity: DatabaseObjectIdentity, statement: str) -> None:
    """Run one table statement, naming the table where it fails."""
    try:
        adapter.execute_statement(conn, statement)
    except DatabaseError as exc:
        raise DatabaseError(f"{_named(identity)}: {exc}") from exc

# -- reading ----------------------------------------------------------------
# A schema read once into the objects that describe it (backlog 684): moved
# from the Console's database_schema_reader unchanged. Every provider question
# is asked through DBAdapter's existing metadata methods; nothing here names a
# provider. A relation, column or routine whose DDL cannot be produced is still
# that object -- losing the whole schema to one unwritable object is the wrong
# trade.


def _ordered(
    objects: Iterable[DatabaseTable | DatabaseView],
) -> tuple[DatabaseTable | DatabaseView, ...]:
    """Database objects, listed by name.

    The provider reports relations in whatever order its inspector walked them,
    which is not an order a reader can use: the Explorer listed 58 tables
    starting `edge, repository, realization_stage`. Routines already arrive
    sorted from the adapter, so this is what made two lists in the same tree
    obey different rules.

    Case-insensitive, because that is what alphabetical means to a reader. The
    raw name breaks a tie so two objects differing only in case keep a settled
    order rather than the one the provider happened to read them in.

    Relations only. A table's COLUMNS are deliberately left in reported order,
    which is ordinal order and carries meaning.

    Args:
        objects: The constructed relations, in the order they were read.

    Returns:
        The same objects, by name.
    """
    return tuple(sorted(
        objects,
        key=lambda one: (one.identity.name.casefold(), one.identity.name),
    ))


def _table(
    adapter: Any,
    handle: Any,
    connection: str,
    catalog: str,
    schema: str,
    read: dict[str, Any],
) -> DatabaseTable:
    """One table, from the schema read plus its own DDL."""
    name = str(read.get("name") or "")
    columns = _columns(read.get("columns"))
    return DatabaseTable(
        identity=DatabaseObjectIdentity(
            connection=connection, catalog=catalog, schema=schema, name=name,
        ),
        columns=_described(adapter, handle, schema, name, columns),
        primary_key=_primary_key(adapter, handle, schema, read.get("primary_key")),
        foreign_keys=tuple(
            _foreign_key(adapter, handle, schema, entry)
            for entry in read.get("foreign_keys") or []
        ),
        indexes=tuple(
            _index(adapter, handle, schema, entry) for entry in read.get("indexes") or []
        ),
        constraints=tuple(
            _constraint(adapter, handle, schema, entry)
            for entry in read.get("unique_constraints") or []
        ) + tuple(
            _check(adapter, handle, schema, entry)
            for entry in read.get("check_constraints") or []
        ),
        ddl=_ddl(adapter, handle, "tables", schema, name),
        initial_select=_initial_select(schema, name, columns),
    )


def _view(
    adapter: Any,
    handle: Any,
    connection: str,
    catalog: str,
    schema: str,
    read: dict[str, Any],
) -> DatabaseView:
    """One view.

    Its columns are asked for separately: the bulk schema read describes a
    view by its definition, and a view's columns are a second question.
    """
    name = str(read.get("name") or "")
    try:
        described = adapter.get_columns(handle, schema, name)
    except Exception:
        described = []
    columns = _described(adapter, handle, schema, name, _columns(described))
    definition = str(read.get("definition") or "")
    return DatabaseView(
        identity=DatabaseObjectIdentity(
            connection=connection, catalog=catalog, schema=schema, name=name,
        ),
        columns=columns,
        ddl=_view_ddl(adapter, handle, connection, catalog, schema, name) or definition,
        initial_select=_initial_select(schema, name, columns),
    )


def _ddl(adapter: Any, handle: Any, object_type: str, schema: str, name: str) -> str:
    """One relation's DDL, or nothing when the provider cannot write it.

    A relation whose DDL cannot be produced is still a relation. Losing the
    whole schema because one object could not be written would be the wrong
    trade.
    """
    try:
        return str(adapter.get_object_ddl(
            handle, {"object_type": object_type, "schema": schema, "name": name},
        ))
    except Exception:
        return ""


def _routines(
    adapter: Any,
    handle: Any,
    connection: str,
    catalog: str,
    schema: str,
    kind: str,
    build: Any,
) -> list[Any]:
    """Every routine of one kind, each with its own definition.

    A provider that answers about neither kind has none to carry, which is not
    a failure: a schema without routines and a provider without routine
    metadata look the same from here, and neither loses the schema.
    """
    lister = adapter.list_procedures if kind == "procedures" else adapter.list_functions
    try:
        listed = lister(handle, schema)
    except Exception:
        return []

    built: list[Any] = []
    for entry in listed or []:
        name = str(entry.get("name") or "")
        if not name:
            continue
        signature = str(entry.get("signature") or "")
        identity = DatabaseRoutineIdentity(
            connection=connection, catalog=catalog, schema=schema,
            name=name, signature=signature,
        )
        built.append(build(
            identity=identity,
            ddl=_routine_ddl(adapter, handle, kind, schema, name, signature),
            # Carried as the provider wrote it. Absent is an ordinary answer,
            # and nothing here composes one in its place: a call assembled by
            # something that does not know the provider's rules can reach the
            # wrong routine rather than fail.
            invocation=str(entry.get("invocation") or ""),
        ))
    return built


def _routine_ddl(
    adapter: Any,
    handle: Any,
    kind: str,
    schema: str,
    name: str,
    signature: str,
) -> str:
    """One routine's source.

    The signature travels with the request because the reader refuses without
    it: a name alone does not say which overload is wanted.
    """
    identity = {
        "schema": schema,
        "name": name,
        "object_type": "procedure" if kind == "procedures" else "function",
        "signature": signature,
    }
    read = (
        adapter.get_procedure_definition
        if kind == "procedures"
        else adapter.get_function_definition
    )
    try:
        return str(read(handle, identity))
    except Exception:
        return ""


def _described(
    adapter: Any,
    handle: Any,
    schema: str,
    table: str,
    columns: tuple[DatabaseColumn, ...],
) -> tuple[DatabaseColumn, ...]:
    """The columns, each carrying its own definition.

    A column is created with its table, so what a provider writes for one is a
    fragment. Where a provider writes none it stays empty, which is the same
    answer every other object gives when its provider cannot write it.
    """
    described: list[DatabaseColumn] = []
    for column in columns:
        try:
            fragment = str(adapter.get_column_ddl(handle, schema, table, column.name))
        except Exception:
            fragment = ""
        described.append(DatabaseColumn(
            name=column.name, type=column.type, nullable=column.nullable,
            default=column.default, ordinal=column.ordinal, ddl=fragment,
        ))
    return tuple(described)


def _child_ddl(
    adapter: Any,
    handle: Any,
    object_type: str,
    schema: str,
    name: str,
) -> str:
    """One index or constraint's own statement, where it has a name to ask by."""
    if not name:
        return ""
    return _ddl(adapter, handle, object_type, schema, name)


def _columns(read: Any) -> tuple[DatabaseColumn, ...]:
    """The columns, in the order the provider reported them.

    That order is ordinal order and means something, so position is taken from
    it rather than from any field: the two readers this accepts do not agree on
    whether they report one.
    """
    return tuple(
        DatabaseColumn(
            name=str(entry.get("name") or ""),
            type=str(entry.get("type") or ""),
            nullable=bool(entry.get("nullable", True)),
            default=None if entry.get("default") is None else str(entry.get("default")),
            ordinal=position,
        )
        for position, entry in enumerate(read or [], start=1)
        if isinstance(entry, dict)
    )


def _primary_key(
    adapter: Any, handle: Any, schema: str, read: Any,
) -> DatabasePrimaryKey | None:
    """The primary key, or nothing when the relation has none.

    An empty constraint is not a key: the inspector answers with an empty
    mapping for a table without one.
    """
    if not isinstance(read, dict):
        return None
    columns = _names(read.get("constrained_columns"))
    if not columns:
        return None
    name = str(read.get("name") or "")
    return DatabasePrimaryKey(
        name=name, columns=columns,
        ddl=_child_ddl(adapter, handle, "constraints", schema, name),
    )


def _foreign_key(
    adapter: Any, handle: Any, schema: str, read: dict[str, Any],
) -> DatabaseForeignKey:
    """One foreign key, and what it points at."""
    name = str(read.get("name") or "")
    return DatabaseForeignKey(
        name=name,
        columns=_names(read.get("constrained_columns")),
        referenced_schema=str(read.get("referred_schema") or ""),
        referenced_table=str(read.get("referred_table") or ""),
        referenced_columns=_names(read.get("referred_columns")),
        ddl=_child_ddl(adapter, handle, "constraints", schema, name),
    )


def _index(
    adapter: Any, handle: Any, schema: str, read: dict[str, Any],
) -> DatabaseIndex:
    name = str(read.get("name") or "")
    return DatabaseIndex(
        name=name,
        columns=_names(read.get("column_names")),
        unique=bool(read.get("unique", False)),
        ddl=_child_ddl(adapter, handle, "indexes", schema, name),
    )


def _constraint(
    adapter: Any, handle: Any, schema: str, read: dict[str, Any],
) -> DatabaseConstraint:
    """One unique constraint, in the provider's own word for its kind."""
    name = str(read.get("name") or "")
    return DatabaseConstraint(
        name=name,
        columns=_names(read.get("column_names")),
        kind="unique",
        ddl=_child_ddl(adapter, handle, "constraints", schema, name),
    )


def _check(
    adapter: Any, handle: Any, schema: str, read: dict[str, Any],
) -> DatabaseConstraint:
    """One check constraint. It constrains an expression, not a column list."""
    name = str(read.get("name") or "")
    return DatabaseConstraint(
        name=name,
        kind="check",
        ddl=_child_ddl(adapter, handle, "constraints", schema, name),
    )


def _names(value: Any) -> tuple[str, ...]:
    """A column name list, with anything unnamed left out."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(name) for name in value if name)


def _initial_select(schema: str, name: str, columns: tuple[DatabaseColumn, ...]) -> str:
    """The statement that opens this object.

    Written from the columns read in the same pass, so opening it asks nothing
    further. Names are written as they were read.
    """
    qualified = f"{schema}.{name}" if schema else name
    projection = [column.name for column in columns if column.name]
    if not projection:
        return f"SELECT * FROM {qualified};"
    listed = ",\n    ".join(projection)
    return f"SELECT\n    {listed}\nFROM {qualified};"


def _view_ddl(
    adapter: Any, handle: Any, connection: str, catalog: str, schema: str, name: str,
) -> str:
    """One view's DDL, as the view writes it, or nothing when it cannot be produced.

    A view whose DDL cannot be produced is still a view, as for every other
    object here.
    """
    identity = DatabaseObjectIdentity(
        connection=connection, catalog=catalog, schema=schema, name=name,
    )
    try:
        return DatabaseView.read_ddl(handle, identity, adapter)
    except Exception:
        return ""
