"""Read capability adapters over the canonical DatabaseSchema owner.

Shared by runtime composition and SQLGenerator; no database or task is resolved
here, and no query is executed on a caller-selected connection.
"""
from typing import Any
from rey_lib.db.database_objects import DatabaseSchema


def schema_operations(conn: Any, connection: str, adapter: Any = None) -> dict[str, Any]:
    def connection_handle():
        return conn() if callable(conn) else conn

    def schemas(arguments: dict[str, Any], tool: Any) -> list[str]:
        return [one.schema for one in DatabaseSchema.list(connection_handle(), connection, adapter)]

    def schema(arguments: dict[str, Any], tool: Any) -> dict[str, Any]:
        read = DatabaseSchema.read(connection_handle(), connection, '', str(arguments['schema']), adapter)
        def columns(owner):
            return [{'name': c.name, 'type': c.type, 'nullable': c.nullable} for c in owner.columns]
        return {'schema': read.schema,
                'tables': [{'name': t.identity.name, 'columns': columns(t)} for t in read.tables],
                'views': [{'name': v.identity.name, 'columns': columns(v)} for v in read.views]}
    return {'database.schemas': schemas, 'database.schema': schema}
