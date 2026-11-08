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
            AIRequestError: when there is nothing to generate from, or the
                connection is not one a language model may use.
            AIError: as AI.execute_task raises.
        """
        if not str(sql_with_instruction_block or "").strip():
            raise AIRequestError("SQL generation needs the reader's SQL and request.")
        # The catalog is read on the model's behalf, so the connection's own
        # declaration that a language model may use it applies; AI holds those
        # names (backlog 1049, D-a).
        if connection not in self._ai.connections():
            raise AIRequestError(
                f"Connection '{connection}' is not one a language model may use, "
                "so no SQL is generated against it."
            )
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
        from rey_lib.db.catalog import schema_operations
        return schema_operations(conn, connection, adapter)
