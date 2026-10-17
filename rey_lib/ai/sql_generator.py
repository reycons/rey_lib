"""SQLGenerator: the canonical owner of natural-language -> SQL generation.

Backlog 1024 (ai.redesign). The flow it owns:

    caller -> SQLGenerator -> AITask -> AI -> AIToolSet / AITool -> catalog owners

It hydrates its governed task (``generate_sql``: contract ``sql_generate_sql``
v2 and that task's engine and settings), sends the reader's SQL with its
``/* AI: ... */`` request, and binds the governed catalog tools to the canonical
catalog owner on the caller's connection, so the model reads the catalog it
needs rather than being handed all of it. The answer is the generated SQL text.

What it does not own:

    executing SQL       DatabaseQuery's, later and by the caller, if at all
    reading a catalog   DatabaseSchema's; the operations here only delegate
                        and serialize what came back
    other SQL work      autocomplete, commenting and contract-driven transforms
                        are ordinary AI tasks, run through AI.execute_task

It is not an AITool and no AI task calls it.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rey_lib.ai.ai import AI
from rey_lib.ai.content import AIInput
from rey_lib.ai.errors import AIRequestError
from rey_lib.ai.task import AITask
from rey_lib.ai.tools import Operation
from rey_lib.logs.app_logger import AppLogger

logger = AppLogger(__name__)

__all__ = ["SQLGenerator"]


class SQLGenerator:
    """Writes SQL from a reader's request, through AI and the governed catalog tools.

    Args:
        ai: The runtime's one AI.
        control: The runtime's Control, through which the task is hydrated.
        installation: Whose governed configuration the task is read from.
    """

    #: The governed task this object runs. Its contract, engine and settings are
    #: the task's, read at every generation; none is chosen here.
    TASK_KEY: ClassVar[str] = "generate_sql"
    #: The binding identities of the governed catalog tools (ai.ai_tool
    #: capability_key). The rows declare the tools; these name what carries them.
    LIST_SCHEMAS: ClassVar[str] = "database.schemas"
    READ_SCHEMA: ClassVar[str] = "database.schema"

    def __init__(self, ai: AI, control: Any, installation: str) -> None:
        self._ai = ai
        self._control = control
        self._installation = str(installation or "")

    def generate(
        self,
        sql_with_instruction_block: str,
        *,
        connection: str,
        conn: Any,
        adapter: Any = None,
    ) -> str:
        """The SQL the request asks for, generated and never executed.

        Args:
            sql_with_instruction_block: The reader's SQL, carrying the request
                in its ``/* AI: ... */`` block, as the contract expects.
            connection: The configured connection's name the SQL is for.
            conn: An open connection on it, which the catalog owner reads
                through. Opened and closed by the caller.
            adapter: The database adapter, where the caller supplies one.

        Returns:
            The generated SQL text, as the contract returns it.

        Raises:
            AIRequestError: when there is nothing to generate from.
            AIError: as AI.execute_task raises.
        """
        if not str(sql_with_instruction_block or "").strip():
            raise AIRequestError("SQL generation needs the reader's SQL and request.")
        task = AITask.from_governed(
            self._control, self._installation, self.TASK_KEY,
            input=AIInput.prompt(sql_with_instruction_block),
        )
        logger.debug("generating SQL for connection %s through task %s",
                     connection, self.TASK_KEY)
        result = self._ai.execute_task(task, self._catalog(conn, connection, adapter))
        return result.text

    def _catalog(self, conn: Any, connection: str, adapter: Any) -> dict[str, Operation]:
        """The catalog capabilities, bound to the canonical owner on this connection.

        Each delegates to DatabaseSchema and serializes its answer for the model:
        names, columns and their types -- not DDL, which the model does not need
        to write a query and which would crowd out what it does.
        """
        from rey_lib.db.database_objects import DatabaseSchema  # noqa: PLC0415

        def schemas(arguments: dict[str, Any], tool: Any) -> list[str]:
            """The connection's schemas, by name."""
            return [one.schema for one in DatabaseSchema.list(conn, connection, adapter)]

        def schema(arguments: dict[str, Any], tool: Any) -> dict[str, Any]:
            """One schema's tables and views, with their columns."""
            read = DatabaseSchema.read(conn, connection, "", str(arguments["schema"]), adapter)

            def columns(owner: Any) -> list[dict[str, Any]]:
                return [
                    {"name": column.name, "type": column.type, "nullable": column.nullable}
                    for column in owner.columns
                ]

            return {
                "schema": read.schema,
                "tables": [
                    {"name": table.identity.name, "columns": columns(table)}
                    for table in read.tables
                ],
                "views": [
                    {"name": view.identity.name, "columns": columns(view)}
                    for view in read.views
                ],
            }

        return {self.LIST_SCHEMAS: schemas, self.READ_SCHEMA: schema}
