"""The connector primitives ``DatabaseTable`` reaches a database through, for adapter doubles.

``DatabaseTable`` owns table DDL and existence (backlog 690) and asks the
connector only for ``provider``, ``quote_identifier``, ``execute_statement``
and ``fetch_rows``. An adapter double mixes this in and keeps answering in its
own terms:

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

    def fetch_rows(self, conn: Any, _sql_text: str, params: dict[str, Any]) -> list[dict]:
        present = self.has_table(conn, params["schema"], params["table"])
        return [{"present": 1}] if present else []

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
            self.record_create(conn, schema, table, defs)
        elif sql_text.startswith("ALTER TABLE"):
            self.__dict__.setdefault("altered", []).append(sql_text)
        return 0
