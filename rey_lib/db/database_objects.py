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
from typing import Any

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
