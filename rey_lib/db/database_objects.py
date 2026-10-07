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
``execute_statement`` / ``table_exists``), with every column type from
``DatabaseType``. An operation is handed the connection; the object never keeps
one, so the same value is what the Console's tree draws.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha1
from types import MethodType
from typing import Any, ClassVar, Iterable

from rey_lib.data.logical_type import LogicalType
from rey_lib.db.database_type import DatabaseType
from rey_lib.errors.error_utils import DatabaseError, UnsupportedDatabaseCapabilityError
from rey_lib.logs import get_logger

_logger = get_logger(__name__)

__all__ = [
    "DatabaseColumn",
    "DatabaseFunction",
    "DatabaseConstraint",
    "DatabaseForeignKey",
    "DatabaseIndex",
    "DatabaseObjectIdentity",
    "DatabaseObjectListing",
    "DatabaseObjectReference",
    "DatabasePrimaryKey",
    "DatabaseProcedure",
    "DatabaseRoutineIdentity",
    "DatabaseSchema",
    "DatabaseSequence",
    "DatabaseTable",
    "DatabaseTrigger",
    "DatabaseUserDefinedType",
    "DatabaseView",
    "schema_and_table",
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
class DatabaseObjectReference:
    """Which canonical object another depends on: an identity, never the object (backlog 723).

    Kind, schema and name address it. A child's table and a routine's
    signature are carried as well, so two objects are never collapsed into one
    reference. A value with no behaviour; an empty name is a reference the
    inventory could not state.
    """

    kind: str
    schema: str
    name: str
    table: str = ""
    signature: str = ""


#: The canonical kind words, and the words a provider's inventory uses for
#: each. The vocabulary is the database objects' (backlog 723), each of which
#: states its own ``KIND``.
_KIND_ALIASES: dict[str, str] = {
    "schema": "schemas",
    "schemas": "schemas",
    "type": "types",
    "types": "types",
    "sequence": "sequences",
    "sequences": "sequences",
    "table": "tables",
    "tables": "tables",
    "constraint": "constraints",
    "constraints": "constraints",
    "index": "indexes",
    "indexes": "indexes",
    "view": "views",
    "views": "views",
    "function": "functions",
    "functions": "functions",
    "procedure": "procedures",
    "procedures": "procedures",
    "trigger": "triggers",
    "triggers": "triggers",
}


def _kind_of(value: str) -> str:
    """A known kind in its canonical word; an unknown one stays as it is.

    Never a table by default: a kind no object owns must reach the listing's
    refusal, not become a DatabaseTable.
    """
    raw = (value or "").strip().lower()
    if raw in _KIND_ALIASES:
        return _KIND_ALIASES[raw]
    singular = raw.rstrip("s")
    if singular in _KIND_ALIASES:
        return _KIND_ALIASES[singular]
    return raw


class _ReadDdl:
    """``read_ddl``: asked of a type with an address, or of an object for its own (723).

    One DDL contract under one name. Asked of the class, the caller addresses
    the object exactly as before. Asked of an object -- one a listing built --
    the object is the address: its identity, its table and name, or its
    schema's name. There is no second DDL method.
    """

    def __init__(self, read: Any) -> None:
        self._read = read
        self.__doc__ = read.__doc__

    def __get__(self, obj: Any, owner: type) -> Any:
        if obj is None:
            return MethodType(self._read, owner)
        read = self._read

        def own(conn: Any, adapter: Any = None, **options: Any) -> str:
            return read(owner, conn, *_address(obj), adapter, **options)

        own.__doc__ = read.__doc__
        return own


def _address(obj: Any) -> tuple[Any, ...]:
    """How an object addresses its own DDL: what its ``read_ddl`` is asked with."""
    if isinstance(obj, DatabaseSchema):
        return (obj.schema,)
    identity = getattr(obj, "identity", None)
    if identity is not None:
        return (identity,)
    if getattr(obj, "table", None) is None:
        raise DatabaseError(
            f"{type(obj).__name__} '{obj.name}' carries no table; "
            f"ask the type for its DDL with one."
        )
    return (obj.table, obj.name)


def _reference(obj: Any) -> DatabaseObjectReference:
    """The object's canonical reference, from its own identity."""
    kind = type(obj).KIND
    if isinstance(obj, DatabaseSchema):
        return DatabaseObjectReference(kind=kind, schema=obj.schema, name=obj.schema)
    identity = getattr(obj, "identity", None)
    if identity is not None:
        return DatabaseObjectReference(
            kind=kind, schema=identity.schema, name=identity.name,
            signature=str(getattr(identity, "signature", "") or ""),
        )
    table = obj.table
    return DatabaseObjectReference(
        kind=kind, schema=table.schema if table else "", name=obj.name,
        table=table.name if table else "",
    )


@dataclass(frozen=True)
class DatabaseColumn:
    """One column, in the position the provider reported it.

    The one owner of a column definition (backlog 721): the fragment a table's
    DDL lists for it, composed from this column's own physical facts. Not a
    statement -- a column has no standalone CREATE, and table-level CREATE or
    ALTER syntax is DatabaseTable's.
    """

    name: str
    type: str
    nullable: bool
    default: str | None
    ordinal: int
    ddl: str = ""

    def definition(self, conn: Any, adapter: Any = None) -> str:
        """This column's definition fragment: name, type, default, nullability.

        ``type`` is the physical type the destination reported, or the one
        DatabaseType resolved for it. The name is quoted by the provider's own
        rule; a provider that takes no explicit NULL gets none.
        """
        adapter = adapter or _adapter()
        parts = [adapter.quote_identifier(conn, self.name), self.type]
        if self.default is not None:
            parts.append(f"DEFAULT {self.default}")
        if not self.nullable:
            parts.append("NOT NULL")
        elif adapter.provider(conn) not in _NO_EXPLICIT_NULL:
            parts.append("NULL")
        return " ".join(parts)

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
    table: DatabaseObjectIdentity | None = None
    depends_on: tuple[DatabaseObjectReference, ...] = ()

    #: Its kind in the canonical vocabulary (backlog 723).
    KIND: ClassVar[str] = "constraints"
    reference = property(_reference)

    @staticmethod
    def _owns_constraint(kind: str) -> bool:
        """Which constraint kinds are this type's: the one partition, to list and to write."""
        return kind == "primary_key"

    @classmethod
    def list(
        cls, conn: Any, database: str | None = None, adapter: Any = None,
    ) -> tuple["DatabasePrimaryKey", ...]:
        """Every primary key the database holds, unread, from its inventory (backlog 723)."""
        return _listed(cls, conn, database, adapter)

    @classmethod
    def _from_inventory(cls, row: dict[str, Any]) -> "DatabasePrimaryKey":
        """One primary key this type claimed, addressed as its ``read_ddl`` addresses it."""
        return cls(name=_row_name(row), table=_row_table(row), depends_on=_depends_on(row))

    @_ReadDdl
    def read_ddl(
        cls, conn: Any, table: DatabaseObjectIdentity, name: str, adapter: Any = None,
        *, database_name: str = "",
    ) -> str:
        """This primary key's own standalone DDL (backlog 695).

        Addressed by the table it belongs to and its own name -- never by a
        provider handle. Standalone: ALTER TABLE ... ADD CONSTRAINT. Where the
        table's own CREATE TABLE already creates it, that is the table's.

        Raises:
            DatabaseError: If there is no such constraint, or it is not a primary key.
            UnsupportedDatabaseCapabilityError: If the provider answers neither.
        """
        return _constraint_ddl(
            cls.__name__, cls._owns_constraint, conn, table, name, adapter,
            database_name=database_name,
        )

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
    table: DatabaseObjectIdentity | None = None
    depends_on: tuple[DatabaseObjectReference, ...] = ()

    #: Its kind in the canonical vocabulary (backlog 723).
    KIND: ClassVar[str] = "constraints"
    reference = property(_reference)

    @staticmethod
    def _owns_constraint(kind: str) -> bool:
        """Which constraint kinds are this type's: the one partition, to list and to write."""
        return kind == "foreign_key"

    @classmethod
    def list(
        cls, conn: Any, database: str | None = None, adapter: Any = None,
    ) -> tuple["DatabaseForeignKey", ...]:
        """Every foreign key the database holds, unread, from its inventory (backlog 723)."""
        return _listed(cls, conn, database, adapter)

    @classmethod
    def _from_inventory(cls, row: dict[str, Any]) -> "DatabaseForeignKey":
        """One foreign key this type claimed, addressed as its ``read_ddl`` addresses it."""
        return cls(name=_row_name(row), table=_row_table(row), depends_on=_depends_on(row))

    @_ReadDdl
    def read_ddl(
        cls, conn: Any, table: DatabaseObjectIdentity, name: str, adapter: Any = None,
        *, database_name: str = "",
    ) -> str:
        """This foreign key's own standalone DDL (backlog 695).

        Addressed by the table it belongs to and its own name -- never by a
        provider handle. Standalone: ALTER TABLE ... ADD CONSTRAINT. Where the
        table's own CREATE TABLE already creates it, that is the table's.

        Raises:
            DatabaseError: If there is no such constraint, or it is not a foreign key.
            UnsupportedDatabaseCapabilityError: If the provider answers neither.
        """
        return _constraint_ddl(
            cls.__name__, cls._owns_constraint, conn, table, name, adapter,
            database_name=database_name,
        )

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
    table: DatabaseObjectIdentity | None = None
    depends_on: tuple[DatabaseObjectReference, ...] = ()

    #: Its kind in the canonical vocabulary (backlog 723).
    KIND: ClassVar[str] = "indexes"
    reference = property(_reference)

    @classmethod
    def list(
        cls, conn: Any, database: str | None = None, adapter: Any = None,
    ) -> tuple["DatabaseIndex", ...]:
        """Every index the database holds, unread, from its inventory (backlog 723)."""
        return _listed(cls, conn, database, adapter)

    @classmethod
    def _from_inventory(cls, row: dict[str, Any]) -> "DatabaseIndex":
        """One index this type claimed, addressed as its ``read_ddl`` addresses it."""
        return cls(name=_row_name(row), table=_row_table(row), depends_on=_depends_on(row))

    @_ReadDdl
    def read_ddl(
        cls, conn: Any, table: DatabaseObjectIdentity, name: str, adapter: Any = None,
        *, database_name: str = "",
    ) -> str:
        """This index's own DDL (backlog 695).

        An index is addressed by the table it belongs to and its own name --
        never by a provider handle. Where the database supplies its own
        definition (PostgreSQL) that definition IS the DDL; elsewhere the index
        composes CREATE [UNIQUE] INDEX from the key columns its catalog states.
        Ordinary and unique indexes only: a primary key's backing index is the
        primary key, whose DDL is DatabasePrimaryKey's, and is refused here.

        Raises:
            DatabaseError: If there is no such index, or it backs the primary key.
            UnsupportedDatabaseCapabilityError: If the provider answers neither.
        """
        adapter = adapter or _adapter()
        where = f"{table.schema}.{table.name}.{name}"
        catalog = adapter.index_column_catalog(
            conn, table.schema, table.name, name, database_name=database_name,
        )
        if not catalog:
            raise DatabaseError(f"index not found: {where}")
        if catalog["primary"]:
            raise DatabaseError(
                f"index {where} backs the primary key; its DDL is DatabasePrimaryKey's"
            )
        provider = adapter.provider(conn)
        if provider in _NATIVE_INDEX_DEFINITION:
            definition = adapter.index_native_definition(
                conn, table.schema, table.name, name,
            ).rstrip()
            if not definition:
                raise DatabaseError(f"index not found: {where}")
            return definition if definition.endswith(";") else definition + ";"
        quote = adapter.quote_identifier
        columns = ", ".join(quote(conn, column) for column in catalog["columns"])
        unique = "UNIQUE " if catalog["unique"] else ""
        return (
            f"CREATE {unique}INDEX {quote(conn, name)} "
            f"ON {quote(conn, table.schema)}.{quote(conn, table.name)} ({columns});"
            + _BATCH_END.get(provider, "")
        )

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
    table: DatabaseObjectIdentity | None = None
    depends_on: tuple[DatabaseObjectReference, ...] = ()

    #: Its kind in the canonical vocabulary (backlog 723).
    KIND: ClassVar[str] = "constraints"
    reference = property(_reference)

    @staticmethod
    def _owns_constraint(kind: str) -> bool:
        """Which constraint kinds are this type's: the one partition, to list and to write."""
        return kind not in ("primary_key", "foreign_key")

    @classmethod
    def list(
        cls, conn: Any, database: str | None = None, adapter: Any = None,
    ) -> tuple["DatabaseConstraint", ...]:
        """Every unique or check constraint the database holds, unread (backlog 723)."""
        return _listed(cls, conn, database, adapter)

    @classmethod
    def _from_inventory(cls, row: dict[str, Any]) -> "DatabaseConstraint":
        """One constraint this type claimed, addressed as its ``read_ddl`` addresses it."""
        return cls(
            name=_row_name(row), kind=str(row.get("kind") or ""),
            table=_row_table(row), depends_on=_depends_on(row),
        )

    @_ReadDdl
    def read_ddl(
        cls, conn: Any, table: DatabaseObjectIdentity, name: str, adapter: Any = None,
        *, database_name: str = "",
    ) -> str:
        """This unique or check constraint's own standalone DDL (backlog 695).

        Addressed by the table it belongs to and its own name -- never by a
        provider handle. Standalone: ALTER TABLE ... ADD CONSTRAINT. Where the
        table's own CREATE TABLE already creates it, that is the table's.

        Raises:
            DatabaseError: If there is no such constraint, or it is a primary or foreign key.
            UnsupportedDatabaseCapabilityError: If the provider answers neither.
        """
        return _constraint_ddl(
            cls.__name__, cls._owns_constraint,
            conn, table, name, adapter, database_name=database_name,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "columns": list(self.columns),
            "kind": self.kind,
            "ddl": self.ddl,
        }


@dataclass(frozen=True)
class DatabaseSequence:
    """One sequence, named within its schema (backlog 695).

    Its identity is schema and name -- never a name with the schema folded
    into it, and never a provider handle.
    """

    identity: DatabaseObjectIdentity
    ddl: str = ""
    depends_on: tuple[DatabaseObjectReference, ...] = ()

    #: Its kind in the canonical vocabulary (backlog 723).
    KIND: ClassVar[str] = "sequences"
    reference = property(_reference)

    @classmethod
    def list(
        cls, conn: Any, database: str | None = None, adapter: Any = None,
    ) -> tuple["DatabaseSequence", ...]:
        """Every sequence the database holds, unread, from its inventory (backlog 723)."""
        return _listed(cls, conn, database, adapter)

    @classmethod
    def _from_inventory(cls, row: dict[str, Any]) -> "DatabaseSequence":
        """One sequence this type claimed, addressed as its ``read_ddl`` addresses it."""
        return cls(identity=_row_identity(row), depends_on=_depends_on(row))

    @_ReadDdl
    def read_ddl(
        cls, conn: Any, identity: DatabaseObjectIdentity, adapter: Any = None,
    ) -> str:
        """This sequence's own DDL.

        Where the database supplies its own definition (DuckDB) that
        definition IS the DDL; elsewhere the sequence composes CREATE SEQUENCE
        from the start, increment, bounds and cycling its catalog states.

        Raises:
            DatabaseError: When there is no such sequence.
            UnsupportedDatabaseCapabilityError: If the provider has no sequences.
        """
        adapter = adapter or _adapter()
        where = f"{identity.schema}.{identity.name}"
        provider = adapter.provider(conn)
        if provider in _NATIVE_SEQUENCE_DEFINITION:
            definition = adapter.sequence_native_definition(
                conn, identity.schema, identity.name,
            ).rstrip()
            if not definition:
                raise DatabaseError(f"sequence not found: {where}")
            return definition if definition.endswith(";") else definition + ";"
        catalog = adapter.sequence_catalog(conn, identity.schema, identity.name)
        if not catalog:
            raise DatabaseError(f"sequence not found: {where}")
        qualified = ".".join(
            adapter.quote_identifier(conn, part) for part in (identity.schema, identity.name)
        )
        create = (
            "CREATE SEQUENCE IF NOT EXISTS"
            if provider in _SEQUENCE_IF_NOT_EXISTS else "CREATE SEQUENCE"
        )
        return (
            f"{create} {qualified} START WITH {catalog['start']} "
            f"INCREMENT BY {catalog['increment']} "
            f"MINVALUE {catalog['minimum']} MAXVALUE {catalog['maximum']} "
            f"{'CYCLE' if catalog['cycle'] else 'NO CYCLE'};"
            + _BATCH_END.get(provider, "")
        )

    def to_dict(self) -> dict[str, Any]:
        return {"identity": self.identity.to_dict(), "ddl": self.ddl}


@dataclass(frozen=True)
class DatabaseUserDefinedType:
    """One user-defined type: a schema-scoped database object (backlog 695).

    ``kind`` is its variant -- ``enum`` or ``domain`` (PostgreSQL), ``alias``
    (SQL Server). Not :class:`~rey_lib.db.database_type.DatabaseType`, which
    is how a Rey logical type is spelled at a destination and has no identity.
    """

    identity: DatabaseObjectIdentity
    kind: str = ""
    ddl: str = ""
    depends_on: tuple[DatabaseObjectReference, ...] = ()

    #: Its kind in the canonical vocabulary (backlog 723).
    KIND: ClassVar[str] = "types"
    reference = property(_reference)

    @classmethod
    def list(
        cls, conn: Any, database: str | None = None, adapter: Any = None,
    ) -> tuple["DatabaseUserDefinedType", ...]:
        """Every user-defined type the database holds, unread, from its inventory (backlog 723)."""
        return _listed(cls, conn, database, adapter)

    @classmethod
    def _from_inventory(cls, row: dict[str, Any]) -> "DatabaseUserDefinedType":
        """One user-defined type this type claimed, addressed as its ``read_ddl`` addresses it."""
        return cls(identity=_row_identity(row), depends_on=_depends_on(row))

    @_ReadDdl
    def read_ddl(
        cls, conn: Any, identity: DatabaseObjectIdentity, adapter: Any = None,
    ) -> str:
        """This type's own DDL, composed from what its catalog states.

        Raises:
            DatabaseError: When there is no such type, or its kind is one this
                object does not represent.
            UnsupportedDatabaseCapabilityError: If the provider has no such types.
        """
        adapter = adapter or _adapter()
        where = f"{identity.schema}.{identity.name}"
        catalog = adapter.user_type_catalog(conn, identity.schema, identity.name)
        if not catalog:
            raise DatabaseError(f"type not found: {where}")
        provider = adapter.provider(conn)
        qualified = ".".join(
            adapter.quote_identifier(conn, part) for part in (identity.schema, identity.name)
        )
        kind = str(catalog["kind"])
        if kind == "enum":
            labels = ", ".join(
                "'" + str(label).replace("'", "''") + "'" for label in catalog["labels"]
            )
            statement = f"CREATE TYPE {qualified} AS ENUM ({labels});"
        elif kind == "domain":
            default = f" DEFAULT {catalog['default']}" if catalog["default"] is not None else ""
            not_null = " NOT NULL" if catalog["not_null"] else ""
            statement = f"CREATE DOMAIN {qualified} AS {catalog['base_type']}{default}{not_null};"
        elif kind == "alias":
            statement = f"CREATE TYPE {qualified} FROM {catalog['base_type']};"
        else:
            raise DatabaseError(
                f"type {where} is a {kind}, which DatabaseUserDefinedType does not represent"
            )
        return statement + _BATCH_END.get(provider, "")

    def to_dict(self) -> dict[str, Any]:
        return {"identity": self.identity.to_dict(), "kind": self.kind, "ddl": self.ddl}


@dataclass(frozen=True)
class DatabaseTrigger:
    """One trigger, named within the table it fires on (backlog 695).

    A trigger name is not unique on its own; its identity is its table's
    identity and its name, never a provider handle such as PostgreSQL's oid or
    SQL Server's object_id.
    """

    name: str
    ddl: str = ""
    table: DatabaseObjectIdentity | None = None
    depends_on: tuple[DatabaseObjectReference, ...] = ()

    #: Its kind in the canonical vocabulary (backlog 723).
    KIND: ClassVar[str] = "triggers"
    reference = property(_reference)

    @classmethod
    def list(
        cls, conn: Any, database: str | None = None, adapter: Any = None,
    ) -> tuple["DatabaseTrigger", ...]:
        """Every trigger the database holds, unread, from its inventory (backlog 723)."""
        return _listed(cls, conn, database, adapter)

    @classmethod
    def _from_inventory(cls, row: dict[str, Any]) -> "DatabaseTrigger":
        """One trigger this type claimed, addressed as its ``read_ddl`` addresses it."""
        return cls(name=_row_name(row), table=_row_table(row), depends_on=_depends_on(row))

    @_ReadDdl
    def read_ddl(
        cls, conn: Any, table: DatabaseObjectIdentity, name: str, adapter: Any = None,
    ) -> str:
        """This trigger's own DDL.

        The server's stored definition is a retrieved fact
        (``DBAdapter.trigger_native_definition``); the trigger ends the
        statement as its provider's script needs.

        Raises:
            DatabaseError: When the table has no such trigger.
            UnsupportedDatabaseCapabilityError: If the provider has no triggers.
        """
        adapter = adapter or _adapter()
        definition = adapter.trigger_native_definition(
            conn, table.schema, table.name, name,
        ).rstrip()
        if not definition:
            raise DatabaseError(f"trigger not found: {table.schema}.{table.name}.{name}")
        return _routine_terminated(adapter.provider(conn), definition)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ddl": self.ddl}


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
    depends_on: tuple[DatabaseObjectReference, ...] = ()

    #: Its kind in the canonical vocabulary (backlog 723).
    KIND: ClassVar[str] = "tables"
    reference = property(_reference)

    @classmethod
    def list(
        cls, conn: Any, database: str | None = None, adapter: Any = None,
    ) -> tuple["DatabaseTable", ...]:
        """Every table the database holds, unread, from its inventory (backlog 723)."""
        return _listed(cls, conn, database, adapter)

    @classmethod
    def _from_inventory(cls, row: dict[str, Any]) -> "DatabaseTable":
        """One table this type claimed, addressed as its ``read_ddl`` addresses it."""
        return cls(identity=_row_identity(row), depends_on=_depends_on(row))

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
        # How the provider's catalog is asked is the connector's (backlog 698).
        return adapter.table_exists(conn, identity.catalog, identity.schema, identity.name)

    @classmethod
    def read(
        cls, conn: Any, identity: DatabaseObjectIdentity, adapter: Any = None,
    ) -> "DatabaseTable":
        """This one table, read back as a schema read would build it (backlog 727).

        Addressed exactly as ``create`` and ``exists`` address it: every read
        is of the identity's catalog, so a table in another database on the
        server (SQL Server) is read from that database, and providers to
        which a catalog means nothing ignore it. Built by the same ``_table``
        as a schema read -- there is one way a table is constructed.

        Raises:
            DatabaseError: When the schema holds no such table.
        """
        adapter = adapter or _adapter()
        read = adapter.get_schema_metadata(
            conn, identity.schema, database_name=identity.catalog,
        )
        for entry in read.get("tables") or []:
            if str(entry.get("name") or "") == identity.name:
                return _table(
                    adapter, conn, identity.connection, identity.catalog, identity.schema,
                    entry, identity.catalog,
                )
        raise DatabaseError(f"table not found: {_named(identity)}")

    @classmethod
    def create(
        cls,
        conn: Any,
        identity: DatabaseObjectIdentity,
        columns: list[tuple[str, LogicalType]],
        adapter: Any = None,
    ) -> "DatabaseTable":
        """Create this table from a Rey logical schema, where it is not already there.

        Every column is nullable, typed by ``DatabaseType`` for the
        connection's provider, in the order given. An existing table is left
        untouched.

        Commits where the provider's DDL is not part of the load's transaction
        (SQL Server, MySQL), as table creation always has there. PostgreSQL
        and DuckDB DDL is transactional, so a table made for a load that then
        fails goes away with it.

        Returns:
            The table as the server now holds it, read back through ``read``
            -- whether this call created it or it was already there (backlog 727).

        Raises:
            DatabaseError: If no columns are given, the DDL fails, or the
                table cannot be read back.
        """
        adapter = adapter or _adapter()
        if not columns:
            raise DatabaseError(
                f"DatabaseTable.create: no columns given for {_named(identity)}."
            )
        if cls.exists(conn, identity, adapter):
            return cls.read(conn, identity, adapter)
        provider = adapter.provider(conn)
        types = DatabaseType(provider)
        # DatabaseType resolves each physical type; each column writes its own
        # definition (backlog 721).
        listed = ",\n\t".join(
            DatabaseColumn(
                name=name, type=types.column_type(logical), nullable=True,
                default=None, ordinal=position,
            ).definition(conn, adapter)
            for position, (name, logical) in enumerate(columns, start=1)
        )
        table = _qualified(adapter, conn, identity)
        _run(adapter, conn, identity, f"CREATE TABLE {table} (\n\t{listed}\n)")
        if provider in _COMMITS_TABLE_DDL:
            conn.commit()
        return cls.read(conn, identity, adapter)

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

    @classmethod
    def drop(cls, conn: Any, identity: DatabaseObjectIdentity, adapter: Any = None) -> None:
        """Destroy this table itself, not merely its contents (backlog 722).

        **NAMED FOR WHAT IT DOES.** It is not called `recreate`: a load mode
        that rebuilds a destination is built FROM this and a create.

        **DESTRUCTIVE AT SCHEMA LEVEL, and that is the whole difference from
        ``delete_all_rows``.** Emptying a table keeps the thing it empties; this
        does not. WHAT THE TABLE OWNS GOES WITH IT -- its indexes, its
        constraints, its triggers -- and nothing here rebuilds any of them.

        What happens to objects that merely DEPEND on the table -- a view over
        it, a foreign key from elsewhere -- is the provider's answer: the drop
        may be refused, or may reach further. This does not decide it, and a
        caller that needs to know must ask the database it is talking to.

        ``DROP TABLE``, with no ``CASCADE``, through the connector's
        ``quote_identifier`` and ``execute_statement``. It does not commit.

        Raises:
            DatabaseError: Naming the table, if the drop fails.
            UnsupportedDatabaseCapabilityError: If the provider cannot run it.
        """
        adapter = adapter or _adapter()
        table = _qualified(adapter, conn, identity)
        _run(adapter, conn, identity, f"DROP TABLE {table}")

    @classmethod
    def delete_all_rows(
        cls, conn: Any, identity: DatabaseObjectIdentity, adapter: Any = None,
    ) -> int:
        """Remove every row from this table, leaving the table itself (backlog 722).

        **NAMED FOR WHAT IT DOES.** It is not called `replace`: a load mode
        that replaces a destination's contents is built FROM this.

        What it does NOT do, each because something else does:

        - it does not DROP the table. The schema, its constraints, its grants,
          its indexes and its triggers survive, because emptying a table is not
          the same act as destroying and rebuilding one;
        - it does not reset identity or sequence state. A sequence continues
          from where it was; anyone wanting it reset is asking for a different
          operation and should ask for it by name;
        - it does not create anything. A destination that is not there is
          ``create``'s business.

        ``DELETE FROM`` is the same statement on every provider; only the
        spelling of the name and the running of the statement differ, and
        those are the connector's ``quote_identifier`` and
        ``execute_statement``. It does not commit: whether the statement sits
        in a transaction is the connection's, and is the caller's to manage.

        Returns:
            Rows removed, as the ENGINE counted them. Zero means the table was
            already empty -- a real answer, not a failure.

        Raises:
            DatabaseError: Naming the table, if the delete fails.
            UnsupportedDatabaseCapabilityError: If the provider cannot run it.
        """
        adapter = adapter or _adapter()
        table = _qualified(adapter, conn, identity)
        # The provider's own count, carried: coercing it here would be having
        # an opinion about an answer this did not produce.
        return _run(adapter, conn, identity, f"DELETE FROM {table}")

    @classmethod
    def bulk_insert(
        cls,
        conn: Any,
        identity: DatabaseObjectIdentity,
        rows: list[dict[str, Any]],
        columns: list[str],
        adapter: Any = None,
    ) -> int:
        """Insert these rows into this table, in this column order (backlog 751).

        The provider's own bulk path does the writing; which one, and how it
        binds, is the connector's. It does not commit: whether the insert sits
        in a transaction is the connection's, and is the caller's to manage.

        Returns:
            Rows inserted, as the provider counted them.

        Raises:
            DatabaseError: If the insert fails -- including the overflow
                ``is_truncation_error`` recognises.
        """
        adapter = adapter or _adapter()
        schema, table = schema_and_table(identity)
        return adapter.bulk_insert(conn, schema, table, rows, columns)

    @classmethod
    def insert_from_path(
        cls,
        conn: Any,
        identity: DatabaseObjectIdentity,
        source_path: Any,
        columns: list[str],
        adapter: Any = None,
    ) -> int:
        """Insert a file's rows into this table, read by the engine (backlog 751).

        The non-row sibling of ``bulk_insert``: the rows never pass through
        this process. The provider's statement is atomic; like every table
        operation here, it does not commit.

        Returns:
            Rows inserted, as the engine counted them.

        Raises:
            DatabaseError: If the insert fails.
            UnsupportedDatabaseCapabilityError: If the provider cannot read
                a path.
        """
        adapter = adapter or _adapter()
        schema, table = schema_and_table(identity)
        return adapter.insert_from_path(conn, schema, table, source_path, columns)

    @classmethod
    def can_insert_from_path(cls, conn: Any, adapter: Any = None) -> bool:
        """Whether this connection's provider can fill a table from a path (backlog 751).

        The question asked before ``insert_from_path`` is chosen: the
        provider's own capability answer, unchanged. A provider without the
        path read answers no, and the row-by-row load runs instead.
        """
        return bool(
            (adapter or _adapter()).supports_provider_capability(conn, "insert_from_path")
        )

    @classmethod
    def is_truncation_error(cls, exc: Exception, adapter: Any = None) -> bool:
        """Whether a failed write here was the provider's column overflow (backlog 751).

        The question ``widen_column`` answers: only an overflow is repaired by
        widening, so only an overflow is reported as one. Never raises.
        """
        return (adapter or _adapter()).is_truncation_error(exc)

    @classmethod
    def replace_rows(
        cls,
        conn: Any,
        identity: DatabaseObjectIdentity,
        staging: DatabaseObjectIdentity,
        adapter: Any = None,
    ) -> None:
        """Replace every row of this table with a staging table's, atomically (backlog 516).

        **ATOMIC, OR THE TABLE IS UNCHANGED.** The removal and the copy happen
        as one unit on every provider, so a failure leaves exactly the rows
        that were there. The table itself -- its constraints, grants, indexes
        and triggers -- is kept, which is what replacing contents means.

        The staging table is read, never changed; removing it is the caller's.
        How the unit is made is each provider's smallest sufficient answer:

        - PostgreSQL: one statement, a data-modifying CTE, which an autocommit
          handle runs as a single transaction;
        - DuckDB: one call holding its own transaction, rolled back on failure;
        - SQL Server, MySQL: their connections are transactional, so the two
          statements commit together or roll back together, here.

        Raises:
            DatabaseError: Naming the table, if the replacement fails.
            UnsupportedDatabaseCapabilityError: For a provider with no atomic
                answer -- refused rather than run without the guarantee.
        """
        adapter = adapter or _adapter()
        provider = adapter.provider(conn)
        if provider not in ("postgres", "duckdb") and provider not in _COMMITS_TABLE_DDL:
            raise UnsupportedDatabaseCapabilityError(
                f"DatabaseTable.replace_rows: provider '{provider}' has no atomic "
                f"replacement for {_named(identity)}."
            )
        table = _qualified(adapter, conn, identity)
        source = _qualified(adapter, conn, staging)
        listed = ", ".join(
            adapter.quote_identifier(conn, name)
            for name in cls.column_names(conn, staging, adapter)
        )
        copy = f"INSERT INTO {table} ({listed}) SELECT {listed} FROM {source}"
        if provider == "postgres":
            _run(adapter, conn, identity, f"WITH removed AS (DELETE FROM {table}) {copy}")
        elif provider == "duckdb":
            _atomic_call(
                adapter, conn, identity,
                f"BEGIN TRANSACTION; DELETE FROM {table}; {copy}; COMMIT",
            )
        else:
            _committed_together(
                adapter, conn, identity, [f"DELETE FROM {table}", copy],
            )

    @classmethod
    def replace_table(
        cls,
        conn: Any,
        identity: DatabaseObjectIdentity,
        staging: DatabaseObjectIdentity,
        adapter: Any = None,
    ) -> None:
        """Replace this table itself with a staging table, atomically (backlog 516).

        **RECREATE'S MEANING, KEPT.** Recreating a destination drops the table
        and builds it again from the load's logical schema, and what the old
        table owned goes with it. The staging table was built by ``create``
        from that same schema, so it IS the table recreate would have built;
        this puts it in the old one's place under the old one's name.

        **ATOMIC, OR THE TABLE IS UNCHANGED.** A failure leaves the old table
        exactly as it was. Per provider:

        - PostgreSQL: one ``DO`` block, dropping the old and renaming the
          staging table, which an autocommit handle runs as one transaction;
        - DuckDB: one ``CREATE OR REPLACE TABLE ... AS SELECT`` from staging;
          the staging table remains, for the caller to remove;
        - MySQL: one ``RENAME TABLE`` swapping both names, which MySQL makes
          atomic even though its DROP and CREATE are not; the old table is
          dropped afterwards, the target already replaced;
        - SQL Server: drop and ``sp_rename`` in the connection's transaction,
          committed together or rolled back together, here.

        Raises:
            DatabaseError: Naming the table, if the replacement fails -- or, on
                MySQL, if the old table could not be removed afterwards (the
                target is then already replaced, and the message says so).
            UnsupportedDatabaseCapabilityError: For a provider with no atomic
                answer -- refused rather than run without the guarantee.
        """
        adapter = adapter or _adapter()
        provider = adapter.provider(conn)
        table = _qualified(adapter, conn, identity)
        source = _qualified(adapter, conn, staging)
        name = adapter.quote_identifier(conn, identity.name)
        if provider == "postgres":
            _run(
                adapter, conn, identity,
                f"DO $rey$ BEGIN DROP TABLE {table}; "
                f"ALTER TABLE {source} RENAME TO {name}; END $rey$",
            )
        elif provider == "duckdb":
            _run(
                adapter, conn, identity,
                f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM {source}",
            )
        elif provider == "mysql":
            old = replace(identity, name=_beside(identity.name, "old"))
            retired = _qualified(adapter, conn, old)
            _run(
                adapter, conn, identity,
                f"RENAME TABLE {table} TO {retired}, {source} TO {table}",
            )
            try:
                _run(adapter, conn, identity, f"DROP TABLE {retired}")
            except DatabaseError as exc:
                raise DatabaseError(
                    f"{_named(identity)} was replaced, but its previous table "
                    f"{_named(old)} could not be removed: {exc}"
                ) from exc
        elif provider == "sqlserver":
            # sp_rename works in the database it is called in, so a table in
            # another catalog is renamed by that catalog's own procedure.
            procedure = "sp_rename" if not staging.catalog else (
                f"{adapter.quote_identifier(conn, staging.catalog)}.sys.sp_rename"
            )
            local = _qualified(adapter, conn, replace(staging, catalog=""))
            _committed_together(adapter, conn, identity, [
                f"DROP TABLE {table}",
                f"EXEC {procedure} N{_sql_text(local)}, N{_sql_text(identity.name)}",
            ])
        else:
            raise UnsupportedDatabaseCapabilityError(
                f"DatabaseTable.replace_table: provider '{provider}' has no atomic "
                f"replacement for {_named(identity)}."
            )

    @classmethod
    def column_names(
        cls, conn: Any, identity: DatabaseObjectIdentity, adapter: Any = None,
    ) -> list[str]:
        """This table's column names, in ordinal order (backlog 722).

        Asked through the connector's ``get_table_columns`` primitive, with the
        schema as providers have always received it: ``catalog.schema`` where
        the identity names a catalog. Empty when the table is not there -- a
        table that exists always has at least one column.

        Raises:
            UnsupportedDatabaseCapabilityError: If the provider cannot describe a table.
        """
        adapter = adapter or _adapter()
        schema = f"{identity.catalog}.{identity.schema}" if identity.catalog else identity.schema
        return adapter.get_table_columns(conn, schema, identity.name)

    @classmethod
    def create_includes_constraints(cls, conn: Any, adapter: Any = None) -> bool:
        """Whether this table's CREATE TABLE also creates its keys and constraints.

        Primary key, foreign keys, unique, check and exclusion constraints. A
        fact about the table's own creation statement (backlog 695), stated per
        provider. Where it is true, their standalone DDL would create them twice.

        Raises:
            DatabaseError: For a provider it is not stated for.
        """
        return _creation_fact(_CREATE_INCLUDES_CONSTRAINTS, "constraints", conn, adapter)

    @classmethod
    def create_includes_indexes(cls, conn: Any, adapter: Any = None) -> bool:
        """Whether this table's CREATE TABLE also creates its indexes.

        A fact about the table's own creation statement (backlog 695), stated
        per provider. Where it is true, their standalone DDL would create them
        twice.

        Raises:
            DatabaseError: For a provider it is not stated for.
        """
        return _creation_fact(_CREATE_INCLUDES_INDEXES, "indexes", conn, adapter)

    @_ReadDdl
    def read_ddl(
        cls, conn: Any, identity: DatabaseObjectIdentity, adapter: Any = None,
        *, database_name: str = "",
    ) -> str:
        """This table's own DDL (backlog 695).

        Reading a table's DDL is the table's operation; how a provider's
        catalog is asked is the connector's. Where the database supplies its
        own complete definition (MySQL, DuckDB) that definition IS the DDL,
        ENGINE and character set included, and nothing is reconstructed. Where
        it does not (PostgreSQL, SQL Server) the table composes CREATE TABLE
        from the columns its catalog states. The DDL export asks this for a
        table; reading a schema does too.

        Raises:
            DatabaseError: If the table is not there, or the read fails.
            UnsupportedDatabaseCapabilityError: If the provider answers neither.
        """
        adapter = adapter or _adapter()
        provider = adapter.provider(conn)
        if provider in _NATIVE_TABLE_DEFINITION:
            definition = adapter.table_native_definition(
                conn, identity.schema, identity.name,
            ).rstrip()
            _found(definition, identity)
            return definition if definition.endswith(";") else definition + ";"
        columns = adapter.table_column_catalog(
            conn, identity.schema, identity.name, database_name=database_name,
        )
        _found(columns, identity)
        # schema.table, as table DDL has always been written: the catalog a
        # read carries is the connection's own database, not the statement's.
        qualified = ".".join(
            adapter.quote_identifier(conn, part) for part in (identity.schema, identity.name)
        )
        # The catalog's facts become columns, and each writes its own
        # definition (backlog 721).
        listed = ",\n".join(
            "    " + DatabaseColumn(
                name=column["name"], type=column["type"], nullable=column["nullable"],
                default=column["default"], ordinal=position,
            ).definition(conn, adapter)
            for position, column in enumerate(columns, start=1)
        )
        return f"CREATE TABLE {qualified} (\n{listed}\n);" + _BATCH_END.get(provider, "")

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
    depends_on: tuple[DatabaseObjectReference, ...] = ()

    #: Its kind in the canonical vocabulary (backlog 723).
    KIND: ClassVar[str] = "views"
    reference = property(_reference)

    @classmethod
    def list(
        cls, conn: Any, database: str | None = None, adapter: Any = None,
    ) -> tuple["DatabaseView", ...]:
        """Every view the database holds, unread, from its inventory (backlog 723)."""
        return _listed(cls, conn, database, adapter)

    @classmethod
    def _from_inventory(cls, row: dict[str, Any]) -> "DatabaseView":
        """One view this type claimed, addressed as its ``read_ddl`` addresses it."""
        return cls(identity=_row_identity(row), depends_on=_depends_on(row))

    @_ReadDdl
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
        if provider not in ("postgres", "mysql", "sqlserver", "duckdb"):
            raise DatabaseError(f"DatabaseView.read_ddl: provider '{provider}' has no view DDL.")
        # The server's definition is a retrieved fact (backlog 698); the DDL is
        # the view's.
        definition = adapter.view_native_definition(conn, identity.schema, identity.name)
        if provider in _VIEW_DEFINITION_IS_BODY:
            return f"CREATE OR REPLACE VIEW {qualified} AS\n{definition};"
        if provider == "mysql":
            return definition.rstrip() + ";"
        if provider == "sqlserver":
            return definition.rstrip() + "\nGO" if definition else ""
        # DuckDB's catalog statement already ends.
        return definition.rstrip()

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
    depends_on: tuple[DatabaseObjectReference, ...] = ()

    #: Its kind in the canonical vocabulary (backlog 723).
    KIND: ClassVar[str] = "procedures"
    reference = property(_reference)

    @classmethod
    def list(
        cls, conn: Any, database: str | None = None, adapter: Any = None,
    ) -> tuple["DatabaseProcedure", ...]:
        """Every procedure the database holds, unread, from its inventory (backlog 723)."""
        return _listed(cls, conn, database, adapter)

    @classmethod
    def _from_inventory(cls, row: dict[str, Any]) -> "DatabaseProcedure":
        """One procedure this type claimed, addressed as its ``read_ddl`` addresses it."""
        return cls(identity=_row_routine_identity(row), depends_on=_depends_on(row))

    @_ReadDdl
    def read_ddl(
        cls, conn: Any, identity: DatabaseRoutineIdentity, adapter: Any = None,
    ) -> str:
        """This procedure's own DDL (backlog 695).

        Reading a procedure's DDL is the procedure's operation; the server's
        stored definition is a retrieved fact
        (``DBAdapter.routine_native_definition``), addressed by Rey's routine
        identity -- schema, name and signature, never a provider handle. The
        procedure ends the statement as its provider's script needs: a
        semicolon, and on SQL Server the batch separator.

        Raises:
            DatabaseError: When there is no such procedure.
            UnsupportedDatabaseCapabilityError: If the provider has no procedures.
        """
        adapter = adapter or _adapter()
        definition = adapter.routine_native_definition(
            conn, identity.schema, identity.name, identity.signature, "procedure",
        ).rstrip()
        return _routine_terminated(adapter.provider(conn), definition)

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
    depends_on: tuple[DatabaseObjectReference, ...] = ()

    #: Its kind in the canonical vocabulary (backlog 723).
    KIND: ClassVar[str] = "functions"
    reference = property(_reference)

    @classmethod
    def list(
        cls, conn: Any, database: str | None = None, adapter: Any = None,
    ) -> tuple["DatabaseFunction", ...]:
        """Every function the database holds, unread, from its inventory (backlog 723)."""
        return _listed(cls, conn, database, adapter)

    @classmethod
    def _from_inventory(cls, row: dict[str, Any]) -> "DatabaseFunction":
        """One function this type claimed, addressed as its ``read_ddl`` addresses it."""
        return cls(identity=_row_routine_identity(row), depends_on=_depends_on(row))

    @_ReadDdl
    def read_ddl(
        cls, conn: Any, identity: DatabaseRoutineIdentity, adapter: Any = None,
    ) -> str:
        """This function's own DDL (backlog 695).

        Reading a function's DDL is the function's operation; the server's
        stored definition is a retrieved fact
        (``DBAdapter.routine_native_definition``), addressed by Rey's routine
        identity -- schema, name and signature, never a provider handle. The
        function ends the statement as its provider's script needs.

        Raises:
            DatabaseError: When there is no such function.
            UnsupportedDatabaseCapabilityError: If the provider has no functions.
        """
        adapter = adapter or _adapter()
        definition = adapter.routine_native_definition(
            conn, identity.schema, identity.name, identity.signature, "function",
        ).rstrip()
        return _routine_terminated(adapter.provider(conn), definition)

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
    depends_on: tuple[DatabaseObjectReference, ...] = ()

    #: Its kind in the canonical vocabulary (backlog 723).
    KIND: ClassVar[str] = "schemas"
    reference = property(_reference)

    @classmethod
    def _from_inventory(cls, row: dict[str, Any]) -> "DatabaseSchema":
        """One schema this type claimed, unread, addressed by its name."""
        return cls(
            connection="", catalog="", schema=_row_schema(row), depends_on=_depends_on(row),
        )

    @classmethod
    def list(
        cls, conn: Any, connection: str, adapter: Any = None,
    ) -> tuple["DatabaseSchema", ...]:
        """Every schema the connection holds, each unread (backlog 720).

        Schema enumeration is the schema's: one ``DatabaseSchema`` per schema,
        carrying its connection, catalog and name and nothing it holds --
        what a schema contains is ``read``'s answer. Ordered by name.

        Raises:
            UnsupportedDatabaseCapabilityError: Naming the provider, where it
                cannot enumerate schemas. Refused, never answered with none: a
                provider that cannot list schemas is not a database with zero.
        """
        adapter = adapter or _adapter()
        return tuple(
            cls(connection=connection, catalog=str(entry.get("catalog") or ""),
                schema=str(entry.get("name") or ""))
            for entry in adapter.list_schemas(conn)
            if str(entry.get("name") or "")
        )

    @_ReadDdl
    def read_ddl(cls, conn: Any, schema: str, adapter: Any = None) -> str:
        """This schema's own CREATE SCHEMA (backlog 695).

        Composed from the schema's name alone -- creating a schema needs no
        other fact. The name is quoted by the provider's own rule; SQL Server's
        guarded form also carries it as a string literal and inside EXEC's
        string, each escaped, so any schema name is written correctly.

        Raises:
            DatabaseError: For a provider with no CREATE SCHEMA stated here.
        """
        adapter = adapter or _adapter()
        provider = adapter.provider(conn)
        if provider != "sqlserver" and provider not in _CREATE_SCHEMA_IF_NOT_EXISTS:
            raise DatabaseError(f"no CREATE SCHEMA is stated for {provider}")
        quoted = adapter.quote_identifier(conn, schema)
        if provider == "sqlserver":
            literal = schema.replace("'", "''")
            create = f"CREATE SCHEMA {quoted}".replace("'", "''")
            return f"IF SCHEMA_ID(N'{literal}') IS NULL EXEC('{create}');"
        return f"CREATE SCHEMA IF NOT EXISTS {quoted};"

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


#: Providers whose server supplies a view's SELECT body rather than a statement;
#: DatabaseView writes the CREATE around it (backlog 698).
_VIEW_DEFINITION_IS_BODY: frozenset[str] = frozenset({"postgres"})

#: Providers whose column definition takes no explicit NULL: a nullable column
#: is written without one. Column-definition semantics, so DatabaseColumn's.
_NO_EXPLICIT_NULL: frozenset[str] = frozenset({"duckdb"})

#: Providers whose table DDL is not part of the surrounding transaction, so a
#: created table is committed as it always has been there.
_COMMITS_TABLE_DDL: frozenset[str] = frozenset({"sqlserver", "mysql"})


@dataclass(frozen=True)
class DatabaseObjectListing:
    """One database's objects, each claimed by exactly one canonical type (backlog 723).

    A listing, not a model: it reads the provider's inventory once, has every
    entry claimed by exactly one database-object type, lets that type build the
    object, and carries the provider and database the objects were listed
    from. Raw inventory exists only long enough to find its owner; what this
    holds are canonical objects.
    """

    provider: str
    database: str
    objects: tuple[Any, ...] = ()

    @classmethod
    def read(
        cls, conn: Any, database: str | None = None, adapter: Any = None,
    ) -> "DatabaseObjectListing":
        """List the database, every entry owned exactly once.

        Raises:
            DatabaseError: When an entry is claimed by no type -- naming its
                kind -- or by more than one, or the inventory is malformed.
                An object Rey has no owner for is refused, never dropped: an
                export missing it would look complete.
        """
        adapter = adapter or _adapter()
        provider = adapter.provider(conn)
        rows = _inventory(adapter, conn, database, provider)
        objects: list[Any] = []
        for row in rows:
            owners = [owner for owner in _LISTED if _claims(owner, row)]
            kind = _kind_of(str(row.get("object_type") or ""))
            where = f"{kind}:{_row_schema(row)}.{_row_name(row)}"
            if not owners:
                raise DatabaseError(f"No database object owns the DDL of a '{kind}' ({where}).")
            if len(owners) > 1:
                named = ", ".join(owner.__name__ for owner in owners)
                raise DatabaseError(
                    f"'{kind}' {where} is claimed by {named}; exactly one database "
                    f"object owns each."
                )
            objects.append(owners[0]._from_inventory(row))
        listed = next(
            (str(row.get("database")).strip() for row in rows
             if str(row.get("database") or "").strip()),
            "",
        )
        return cls(
            provider=provider,
            database=listed or database or _connection_database(conn),
            objects=tuple(objects),
        )


#: Every type an inventory entry may belong to. Each claims its own entries
#: (``KIND``, and for constraints its own partition) and builds its own object.
_LISTED: tuple[type, ...] = (
    DatabaseSchema,
    DatabaseUserDefinedType,
    DatabaseSequence,
    DatabaseTable,
    DatabasePrimaryKey,
    DatabaseForeignKey,
    DatabaseConstraint,
    DatabaseIndex,
    DatabaseView,
    DatabaseFunction,
    DatabaseProcedure,
    DatabaseTrigger,
)


def _inventory(
    adapter: Any, conn: Any, database: str | None, provider: str = "",
) -> list[dict[str, Any]]:
    """The provider's inventory, refused where it is not one entry per named object."""
    rows = adapter.list_database_objects(conn, database)
    provider = provider or adapter.provider(conn)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise DatabaseError(
            f"Provider '{provider}' list_database_objects() must return list[dict]."
        )
    for row in rows:
        if not _row_name(row):
            raise DatabaseError(f"Provider '{provider}' returned object with empty name.")
    return rows


def _claims(owner: type, row: dict[str, Any]) -> bool:
    """Whether ``owner`` claims this inventory entry: its kind, and its own partition."""
    if _kind_of(str(row.get("object_type") or "")) != owner.KIND:
        return False
    owns = getattr(owner, "_owns_constraint", None)
    return owns is None or owns(str(row.get("kind") or ""))


def _listed(owner: type, conn: Any, database: str | None, adapter: Any) -> tuple[Any, ...]:
    """One type's objects, by the same claim and build the listing uses."""
    adapter = adapter or _adapter()
    return tuple(
        owner._from_inventory(row)
        for row in _inventory(adapter, conn, database)
        if _claims(owner, row)
    )


def _row_schema(row: dict[str, Any]) -> str:
    return str(row.get("schema") or "public").strip() or "public"


def _row_name(row: dict[str, Any]) -> str:
    return str(row.get("name") or "").strip()


def _row_identity(row: dict[str, Any]) -> DatabaseObjectIdentity:
    return DatabaseObjectIdentity(
        connection="", catalog="", schema=_row_schema(row), name=_row_name(row),
    )


def _row_routine_identity(row: dict[str, Any]) -> DatabaseRoutineIdentity:
    return DatabaseRoutineIdentity(
        connection="", catalog="", schema=_row_schema(row), name=_row_name(row),
        signature=str(row.get("signature") or ""),
    )


def _row_table(row: dict[str, Any]) -> DatabaseObjectIdentity:
    """The table a child entry belongs to."""
    return DatabaseObjectIdentity(
        connection="", catalog="", schema=_row_schema(row),
        name=str(row.get("table") or "").strip(),
    )


def _depends_on(row: dict[str, Any]) -> tuple[DatabaseObjectReference, ...]:
    """What the entry depends on, as canonical references."""
    return tuple(_dependency(dep) for dep in (row.get("dependencies") or []))


def _dependency(dep: Any) -> DatabaseObjectReference:
    """One stated dependency as a reference; one it cannot state has no name."""
    unstated = DatabaseObjectReference(kind="", schema="", name="")
    if isinstance(dep, str):
        kind, colon, rest = dep.strip().partition(":")
        schema, dot, name = rest.partition(".")
        if not (colon and dot and name):
            return unstated
        return DatabaseObjectReference(kind=_kind_of(kind), schema=schema, name=name)
    if isinstance(dep, dict):
        name = str(dep.get("name") or "").strip()
        if not name:
            return unstated
        return DatabaseObjectReference(
            kind=_kind_of(str(dep.get("object_type") or "table")),
            schema=str(dep.get("schema") or "public").strip() or "public",
            name=name,
        )
    return unstated


def _connection_database(conn: Any) -> str:
    """The database a connection names, where the inventory named none."""
    candidate = (
        getattr(conn, "database", None)
        or getattr(getattr(conn, "info", None), "dbname", None)
        or getattr(getattr(conn, "info", None), "database", None)
    )
    return str(candidate).strip() if candidate else "database"


def _adapter() -> Any:
    """The connector, constructed where an operation is not handed one."""
    from rey_lib.db.db_adapter import DBAdapter  # noqa: PLC0415 -- the adapter imports widely

    return DBAdapter()


def schema_and_table(identity: DatabaseObjectIdentity) -> tuple[str, str]:
    """A table's identity as one ``(schema, table)`` pair (backlog 751).

    The identity keeps catalog and schema apart; a write to a table names them
    as one schema argument -- ``database.schema`` where a catalog is given --
    beside the table's own name. Unchanged from what writes have always sent.
    """
    catalog = str(getattr(identity, "catalog", "") or "")
    schema = str(getattr(identity, "schema", "") or "")
    return (f"{catalog}.{schema}" if catalog else schema, str(identity.name))


def _named(identity: DatabaseObjectIdentity) -> str:
    """The identity's parts, joined, for a message."""
    return ".".join(part for part in (identity.catalog, identity.schema, identity.name) if part)


def _qualified(adapter: Any, conn: Any, identity: DatabaseObjectIdentity) -> str:
    """The table as one DDL reference: each part quoted, catalog where it is given."""
    return ".".join(
        adapter.quote_identifier(conn, part)
        for part in (identity.catalog, identity.schema, identity.name) if part
    )


def _atomic_call(
    adapter: Any, conn: Any, identity: DatabaseObjectIdentity, statements: str,
) -> None:
    """Run one call that opens and commits its own transaction, or undo it (backlog 516).

    A failure inside leaves that transaction open and aborted; it is rolled
    back here so the table is as it was and the connection usable. The
    original failure is what is raised.
    """
    try:
        _run(adapter, conn, identity, statements)
    except DatabaseError:
        try:
            adapter.execute_statement(conn, "ROLLBACK")
        except DatabaseError as rollback_exc:
            _logger.debug("Nothing to roll back for %s: %s", _named(identity), rollback_exc)
        raise


def _committed_together(
    adapter: Any, conn: Any, identity: DatabaseObjectIdentity, statements: list[str],
) -> None:
    """Run statements in the connection's transaction: all committed, or all undone.

    For a provider whose connection is transactional (``_COMMITS_TABLE_DDL``),
    where ``create`` already commits its own DDL the same way.
    """
    try:
        for statement in statements:
            _run(adapter, conn, identity, statement)
    except DatabaseError:
        conn.rollback()
        raise
    conn.commit()


def _beside(name: str, role: str) -> str:
    """A table name for a short-lived companion of ``name``, within every provider's limit.

    Deterministic, so a companion a failed run left behind is found again by
    the next one. Long names keep a readable prefix and a hash of the whole.
    """
    suffix = f"__rey_{role}"
    if len(name) + len(suffix) <= 63:
        return name + suffix
    return f"{name[:40]}{suffix}_{sha1(name.encode('utf-8')).hexdigest()[:8]}"


def _sql_text(value: str) -> str:
    """One value as a SQL text literal."""
    return "'" + str(value).replace("'", "''") + "'"


def _run(adapter: Any, conn: Any, identity: DatabaseObjectIdentity, statement: str) -> int:
    """Run one table statement, naming the table where it fails.

    Returns the engine's own count, carried as the provider reported it.
    """
    try:
        return adapter.execute_statement(conn, statement)
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
    database_name: str = "",
) -> DatabaseTable:
    """One table, from the schema read plus its own DDL.

    Its columns are the catalog's facts (backlog 724), asked through the
    connector's table_column_catalog -- the same facts the table's DDL is
    written from. Every read here is of ``database_name`` -- the one the
    schema read was of -- so the table is built from one database (backlog 727).
    """
    name = str(read.get("name") or "")
    columns = _columns(adapter.table_column_catalog(
        handle, schema, name, database_name=database_name,
    ))
    identity = DatabaseObjectIdentity(
        connection=connection, catalog=catalog, schema=schema, name=name,
    )
    return DatabaseTable(
        identity=identity,
        columns=_described(adapter, handle, columns),
        primary_key=_primary_key(
            adapter, handle, identity, read.get("primary_key"), database_name,
        ),
        foreign_keys=tuple(
            _foreign_key(adapter, handle, identity, entry, database_name)
            for entry in read.get("foreign_keys") or []
        ),
        indexes=tuple(
            _index(adapter, handle, identity, entry, database_name)
            for entry in read.get("indexes") or []
        ),
        constraints=tuple(
            _constraint(adapter, handle, identity, entry, database_name)
            for entry in read.get("unique_constraints") or []
        ) + tuple(
            _check(adapter, handle, identity, entry, database_name)
            for entry in read.get("check_constraints") or []
        ),
        ddl=_table_ddl(
            adapter, handle, connection, catalog, schema, name, database_name,
        ),
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
    view by its definition, and a view's columns are a second question --
    answered by the catalog's facts, through the connector's
    table_column_catalog, as a table's are (backlog 724).
    """
    name = str(read.get("name") or "")
    columns = _described(
        adapter, handle, _columns(adapter.table_column_catalog(handle, schema, name)),
    )
    return DatabaseView(
        identity=DatabaseObjectIdentity(
            connection=connection, catalog=catalog, schema=schema, name=name,
        ),
        columns=columns,
        # The view's DDL is DatabaseView.read_ddl's alone (backlog 725): no
        # second definition from the structure read stands in for it.
        ddl=_view_ddl(adapter, handle, connection, catalog, schema, name),
        initial_select=_initial_select(schema, name, columns),
    )


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
            # A procedure or function reads its own DDL (backlog 695).
            ddl=_routine_object_ddl(build, adapter, handle, identity),
            # Carried as the provider wrote it. Absent is an ordinary answer,
            # and nothing here composes one in its place: a call assembled by
            # something that does not know the provider's rules can reach the
            # wrong routine rather than fail.
            invocation=str(entry.get("invocation") or ""),
        ))
    return built


def _described(
    adapter: Any,
    handle: Any,
    columns: tuple[DatabaseColumn, ...],
) -> tuple[DatabaseColumn, ...]:
    """The columns, each carrying its own definition (backlog 721).

    The column writes it from its own facts. Where it cannot be produced it
    stays empty, the same answer every other object gives.
    """
    described: list[DatabaseColumn] = []
    for column in columns:
        try:
            fragment = column.definition(handle, adapter)
        except Exception:
            fragment = ""
        described.append(DatabaseColumn(
            name=column.name, type=column.type, nullable=column.nullable,
            default=column.default, ordinal=column.ordinal, ddl=fragment,
        ))
    return tuple(described)


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
    adapter: Any, handle: Any, table: DatabaseObjectIdentity, read: Any,
    database_name: str = "",
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
        ddl=_child_object_ddl(DatabasePrimaryKey, adapter, handle, table, name, database_name),
    )


def _foreign_key(
    adapter: Any, handle: Any, table: DatabaseObjectIdentity, read: dict[str, Any],
    database_name: str = "",
) -> DatabaseForeignKey:
    """One foreign key, and what it points at."""
    name = str(read.get("name") or "")
    return DatabaseForeignKey(
        name=name,
        columns=_names(read.get("constrained_columns")),
        referenced_schema=str(read.get("referred_schema") or ""),
        referenced_table=str(read.get("referred_table") or ""),
        referenced_columns=_names(read.get("referred_columns")),
        ddl=_child_object_ddl(DatabaseForeignKey, adapter, handle, table, name, database_name),
    )


def _index(
    adapter: Any, handle: Any, table: DatabaseObjectIdentity, read: dict[str, Any],
    database_name: str = "",
) -> DatabaseIndex:
    """One index of ``table``, with the DDL it writes itself (backlog 695)."""
    name = str(read.get("name") or "")
    return DatabaseIndex(
        name=name,
        columns=_names(read.get("column_names")),
        unique=bool(read.get("unique", False)),
        ddl=_child_object_ddl(DatabaseIndex, adapter, handle, table, name, database_name),
    )


def _child_object_ddl(
    owner: Any, adapter: Any, handle: Any, table: DatabaseObjectIdentity, name: str,
    database_name: str = "",
) -> str:
    """One index's, key's or constraint's standalone DDL, as it writes it.

    Nothing where it has no name to be asked by, or cannot be produced.
    """
    if not name:
        return ""
    try:
        return owner.read_ddl(handle, table, name, adapter, database_name=database_name)
    except Exception:
        return ""


def _constraint(
    adapter: Any, handle: Any, table: DatabaseObjectIdentity, read: dict[str, Any],
    database_name: str = "",
) -> DatabaseConstraint:
    """One unique constraint, in the provider's own word for its kind."""
    name = str(read.get("name") or "")
    return DatabaseConstraint(
        name=name,
        columns=_names(read.get("column_names")),
        kind="unique",
        ddl=_child_object_ddl(DatabaseConstraint, adapter, handle, table, name, database_name),
    )


def _check(
    adapter: Any, handle: Any, table: DatabaseObjectIdentity, read: dict[str, Any],
    database_name: str = "",
) -> DatabaseConstraint:
    """One check constraint. It constrains an expression, not a column list."""
    name = str(read.get("name") or "")
    return DatabaseConstraint(
        name=name,
        kind="check",
        ddl=_child_object_ddl(DatabaseConstraint, adapter, handle, table, name, database_name),
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


#: Providers whose server supplies a table's complete definition itself.
_NATIVE_TABLE_DEFINITION: frozenset[str] = frozenset({"mysql", "duckdb"})

# What a provider's CREATE TABLE, as DatabaseTable.read_ddl writes it, creates
# besides the table. Stated, not derived from _NATIVE_TABLE_DEFINITION:
#   mysql     -- SHOW CREATE TABLE carries every key, constraint and index.
#   duckdb    -- the stored CREATE TABLE carries its keys and constraints, which
#                DuckDB cannot add any other way; its indexes are separate
#                CREATE INDEX statements.
#   postgres, sqlserver -- composed from the columns alone.
_CREATE_INCLUDES_CONSTRAINTS: dict[str, bool] = {
    "mysql": True,
    "duckdb": True,
    "postgres": False,
    "sqlserver": False,
}
_CREATE_INCLUDES_INDEXES: dict[str, bool] = {
    "mysql": True,
    "duckdb": False,
    "postgres": False,
    "sqlserver": False,
}

# The providers whose CREATE SCHEMA takes IF NOT EXISTS. SQL Server has no such
# clause; DatabaseSchema writes its guarded form instead.
_CREATE_SCHEMA_IF_NOT_EXISTS: frozenset[str] = frozenset({"postgres", "mysql", "duckdb"})

# The providers whose server supplies a sequence's own definition (DuckDB's
# duckdb_sequences().sql); elsewhere the sequence composes it from its catalog.
_NATIVE_SEQUENCE_DEFINITION: frozenset[str] = frozenset({"duckdb"})

# The providers whose CREATE SEQUENCE accepts IF NOT EXISTS, as their sequence
# DDL has always been written.
_SEQUENCE_IF_NOT_EXISTS: frozenset[str] = frozenset({"postgres"})

# The providers whose server supplies a constraint's own definition; elsewhere
# the owning object composes it from its catalog.
_NATIVE_CONSTRAINT_DEFINITION: frozenset[str] = frozenset({"postgres"})

# The providers whose server supplies an index's own definition; elsewhere the
# index composes it from its catalog.
_NATIVE_INDEX_DEFINITION: frozenset[str] = frozenset({"postgres"})

#: How a provider's script ends one statement's batch, after its semicolon.
_BATCH_END: dict[str, str] = {"sqlserver": "\nGO"}


def _found(read: Any, identity: DatabaseObjectIdentity) -> None:
    """Refuse a catalog read that found no such table, naming it."""
    if not read:
        raise DatabaseError(f"table not found: {identity.schema}.{identity.name}")


def _table_ddl(
    adapter: Any, handle: Any, connection: str, catalog: str, schema: str, name: str,
    database_name: str = "",
) -> str:
    """One table's DDL, as the table writes it, or nothing when it cannot be produced."""
    identity = DatabaseObjectIdentity(
        connection=connection, catalog=catalog, schema=schema, name=name,
    )
    try:
        return DatabaseTable.read_ddl(handle, identity, adapter, database_name=database_name)
    except Exception:
        return ""


def _routine_terminated(provider: str, definition: str) -> str:
    """A routine's or trigger's stored source, ended as its provider's script needs it."""
    if provider in _BATCH_END:
        return definition + _BATCH_END[provider]
    return definition if definition.endswith(";") else definition + ";"


def _routine_object_ddl(
    owner: Any, adapter: Any, handle: Any, identity: DatabaseRoutineIdentity,
) -> str:
    """One procedure's or function's DDL, as it writes it, or nothing when it cannot be produced."""
    try:
        return owner.read_ddl(handle, identity, adapter)
    except Exception:
        return ""


def _constraint_ddl(
    owner: str,
    owns: Any,
    conn: Any,
    table: DatabaseObjectIdentity,
    name: str,
    adapter: Any,
    *,
    database_name: str = "",
) -> str:
    """One key's or constraint's standalone DDL, written by ``owner``.

    ALTER TABLE <table> ADD CONSTRAINT <name>, then the server's own clause
    where it supplies one, or the clause composed from the catalog. A
    constraint of a kind ``owner`` does not own is refused, naming both.
    """
    adapter = adapter or _adapter()
    where = f"{table.schema}.{table.name}.{name}"
    catalog = adapter.constraint_catalog(
        conn, table.schema, table.name, name, database_name=database_name,
    )
    if not catalog:
        raise DatabaseError(f"constraint not found: {where}")
    kind = str(catalog["kind"])
    if not owns(kind):
        raise DatabaseError(f"constraint {where} is a {kind}; its DDL is not {owner}'s")
    provider = adapter.provider(conn)

    def quote(part: str) -> str:
        return adapter.quote_identifier(conn, part)

    def listed(names: Iterable[str]) -> str:
        return ", ".join(quote(column) for column in names)

    head = f"ALTER TABLE {quote(table.schema)}.{quote(table.name)} ADD CONSTRAINT {quote(name)}"
    if provider in _NATIVE_CONSTRAINT_DEFINITION:
        definition = adapter.constraint_native_definition(
            conn, table.schema, table.name, name,
        ).strip()
        if not definition:
            raise DatabaseError(f"constraint not found: {where}")
        return f"{head} {definition};"
    if kind == "primary_key":
        clause = f"PRIMARY KEY ({listed(catalog['columns'])})"
    elif kind == "unique":
        clause = f"UNIQUE ({listed(catalog['columns'])})"
    elif kind == "foreign_key":
        clause = (
            f"FOREIGN KEY ({listed(catalog['columns'])}) REFERENCES "
            f"{quote(catalog['referenced_schema'])}.{quote(catalog['referenced_table'])} "
            f"({listed(catalog['referenced_columns'])})"
        )
    elif kind == "check" and catalog.get("check"):
        clause = f"CHECK ({catalog['check']})"
    else:
        raise DatabaseError(f"constraint {where}: no standalone {provider} form for a {kind}")
    return f"{head} {clause};" + _BATCH_END.get(provider, "")


def _creation_fact(stated: dict[str, bool], what: str, conn: Any, adapter: Any) -> bool:
    """One stated fact about a provider's CREATE TABLE, refused where it is not stated."""
    provider = (adapter or _adapter()).provider(conn)
    if provider not in stated:
        raise DatabaseError(
            f"whether a {provider} table's creation includes its {what} is not stated"
        )
    return stated[provider]
