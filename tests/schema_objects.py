"""A schema map test's schema as the database domain's own objects (#1441).

The map tests state their schemas as short literals -- each table's identity,
columns, keys, indexes and constraints. A schema map reads a ``DatabaseSchema``,
so this builds the objects those literals describe: identities completed with the
map's connection and catalog, everything else as the literal states it.
"""

from __future__ import annotations

from typing import Any

from rey_lib.db.database_objects import (
    DatabaseColumn,
    DatabaseConstraint,
    DatabaseForeignKey,
    DatabaseIndex,
    DatabaseObjectIdentity,
    DatabasePrimaryKey,
    DatabaseSchema,
    DatabaseTable,
)


def schema_objects(read: dict[str, Any], *, connection: str, catalog: str, schema: str) -> DatabaseSchema:
    """The ``DatabaseSchema`` a test's literal describes: its tables, with their parts."""
    return DatabaseSchema(connection=connection, catalog=catalog, schema=schema,
                          tables=tuple(_table(one, connection, catalog) for one in read.get("tables", [])))


def _table(literal: dict[str, Any], connection: str, catalog: str) -> DatabaseTable:
    identity = DatabaseObjectIdentity(connection=connection, catalog=catalog,
                                      schema=literal["identity"]["schema"],
                                      name=literal["identity"]["name"])
    key = literal.get("primary_key")
    return DatabaseTable(
        identity=identity,
        columns=tuple(DatabaseColumn(name=c["name"], type=c["type"], nullable=c["nullable"],
                                     default=None, ordinal=c["ordinal"])
                      for c in literal.get("columns") or []),
        primary_key=(DatabasePrimaryKey(name=key["name"], table=identity, columns=tuple(key["columns"]))
                     if key else None),
        foreign_keys=tuple(DatabaseForeignKey(name=k["name"], table=identity, columns=tuple(k["columns"]),
                                              referenced_schema=k["referenced_schema"],
                                              referenced_table=k["referenced_table"],
                                              referenced_columns=tuple(k["referenced_columns"]))
                           for k in literal.get("foreign_keys") or []),
        indexes=tuple(DatabaseIndex(name=i["name"], table=identity, columns=tuple(i["columns"]),
                                    unique=bool(i.get("unique")))
                      for i in literal.get("indexes") or []),
        constraints=tuple(DatabaseConstraint(name=c["name"], table=identity, columns=tuple(c["columns"]),
                                             kind=c["kind"])
                          for c in literal.get("constraints") or []),
    )
