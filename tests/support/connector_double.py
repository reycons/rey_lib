"""The connector primitives ``DatabaseTable`` reaches a database through, for adapter doubles.

``DatabaseTable`` owns table DDL and existence (backlog 690) and asks the
connector only for ``provider``, ``quote_identifier``, ``execute_statement``
and ``table_exists`` (backlog 698), and reads the table back after creating it
through ``get_schema_metadata`` and ``table_column_catalog`` (backlog 727). An
adapter double mixes this in and keeps answering in its own terms:

- ``has_table(conn, schema, table)`` -- whether the table is there;
- ``record_create(conn, schema, table, defs)`` -- told each CREATE TABLE, with
  ``defs`` the ``(column, physical type)`` pairs the DDL declared.

Every statement is kept in ``statements``; each ALTER also in ``altered``.
"""

from __future__ import annotations

import re
from typing import Any

#: CREATE TABLE "schema"."table" ( ...columns... ) as DatabaseTable writes it.
_CREATE = re.compile(r'^CREATE TABLE (?P<name>.+?) \(\n(?P<columns>.*)\n\)$', re.S)

#: One declared column: "name" TYPE [NULL].
_COLUMN = re.compile(r'^\s*"(?P<name>(?:[^"]|"")*)" (?P<type>.+?)(?: NULL)?,?$')


class ConnectorPrimitives:
    """Mix into an adapter double: the four primitives, answered as PostgreSQL."""

    provider_name = "postgres"

    def provider(self, _conn: Any) -> str:
        return self.provider_name

    def quote_identifier(self, _conn: Any, name: str) -> str:
        return '"' + str(name).replace('"', '""') + '"'

    def table_exists(self, conn: Any, _catalog: str, schema: str, table: str) -> bool:
        # Remembered, so the read-back that follows can list it (backlog 727).
        self.__dict__.setdefault("asked", set()).add((schema, table))
        return bool(self.has_table(conn, schema, table))

    def get_schema_metadata(
        self, _conn: Any, schema: str, *, database_name: str = "",
    ) -> dict[str, Any]:
        """The tables asked about in ``schema``, with no keys.

        From what this double recorded, never ``has_table`` -- the doubles
        count their own existence checks, and a read-back is not one.
        """
        return {"views": [], "tables": [
            {"name": table, "primary_key": {}, "foreign_keys": [], "indexes": [],
             "unique_constraints": [], "check_constraints": []}
            for asked_schema, table in sorted(self.__dict__.get("asked", ()))
            if asked_schema == schema
        ]}

    def table_column_catalog(
        self, _conn: Any, schema: str, table: str, *, database_name: str = "",
    ) -> list[dict[str, Any]]:
        """The columns the CREATE this double saw declared, else none."""
        return [
            {"name": name, "type": physical, "default": None, "nullable": True}
            for name, physical in self.__dict__.get("declared", {}).get((schema, table), [])
        ]

    def execute_statement(self, conn: Any, sql_text: str) -> int:
        self.__dict__.setdefault("statements", []).append(sql_text)
        created = _CREATE.match(sql_text)
        if created:
            parts = [part.strip('"') for part in created.group("name").split('"."')]
            schema, table = ".".join(parts[:-1]), parts[-1]
            defs = [
                (column.group("name").replace('""', '"'), column.group("type"))
                for column in (
                    _COLUMN.match(line) for line in created.group("columns").split("\n")
                )
                if column
            ]
            self.__dict__.setdefault("declared", {})[(schema, table)] = defs
            self.record_create(conn, schema, table, defs)
        elif sql_text.startswith("ALTER TABLE"):
            self.__dict__.setdefault("altered", []).append(sql_text)
        return 0
