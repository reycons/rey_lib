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

Nothing here reads a database, opens a connection, renders SQL for a provider,
or knows what draws it. They are values, and ``to_dict`` is the whole of what
they offer beyond their fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
    """One procedure: which one it is, and the SQL that created it.

    No statement that opens it. Opening a procedure is a call, and what a call
    looks like is not this object's to decide.
    """

    identity: DatabaseRoutineIdentity
    ddl: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"identity": self.identity.to_dict(), "ddl": self.ddl}


@dataclass(frozen=True)
class DatabaseFunction:
    """One function, identified as a procedure is: by name and arguments."""

    identity: DatabaseRoutineIdentity
    ddl: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"identity": self.identity.to_dict(), "ddl": self.ddl}


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
