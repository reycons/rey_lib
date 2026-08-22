"""A configured database connection, as one shared object.

One ``Connection`` per configured connection, shared by every consumer that
names it. Two subsystems asking for ``control`` receive the same instance, so
the live handle beneath them is the same handle rather than two of them opened
independently.

What it owns
------------
- the connection's name, as immutable identity
- the provider and the resolved configuration
- the live handle, opened lazily on first use and reused after
- ``close``, which is idempotent

What it does not own
--------------------
- procedure maps, control semantics, logging semantics, run or batch identity,
  SQL-operation intent, application behaviour
- provider behaviour. Every operation delegates to the existing ``DBAdapter``,
  which dispatches on ``provider``; nothing about postgres, sqlserver, mysql or
  duckdb is reimplemented here.

Lifetime
--------
Opening is lazy because the previous behaviour opened per call and closed
after: a connection was never held open across an idle stretch, and eagerly
opening every configured connection at launch would connect to databases a run
never touches.

``close`` is deliberately not called by ordinary consumers. A shared object
closed by whoever happened to finish first is a handle pulled out from under
everyone still holding it, which is the failure this sharing otherwise invites.
"""

from __future__ import annotations

from typing import Any, Optional

from rey_lib.db.db_adapter import DBAdapter
from rey_lib.errors.error_utils import ConfigError

__all__ = ["Connection", "build_connections", "shared_connection"]

_db = DBAdapter()


class Connection:
    """One configured database connection, opened once and shared."""

    def __init__(self, config: Any, ctx: Any = None) -> None:
        """Hold a resolved connection config; open nothing yet.

        Parameters
        ----------
        config : Any
            A resolved ``connections[]`` record. Must carry ``name``.
        ctx : Any
            Carried through to the backend so a credential naming an
            environment variable can be read as the connection opens.
        """
        name = getattr(config, "name", None)
        if name is None and isinstance(config, dict):
            name = config.get("name")
        if not name:
            raise ConfigError("connection: a connection config must carry a name.")

        self._name = str(name)
        self._config = config
        self._ctx = ctx
        self._handle: Optional[Any] = None

    def __repr__(self) -> str:
        state = "open" if self._handle is not None else "closed"
        return f"<Connection {self._name} {self.provider or '?'} {state}>"

    # -- identity -----------------------------------------------------------

    @property
    def name(self) -> str:
        """The configured connection name. Immutable identity."""
        return self._name

    @property
    def provider(self) -> Optional[str]:
        """The configured provider, or None when the config names none."""
        value = getattr(self._config, "provider", None)
        if value is None and isinstance(self._config, dict):
            value = self._config.get("provider")
        return str(value) if value else None

    @property
    def config(self) -> Any:
        """The resolved configuration this connection was built from."""
        return self._config

    @property
    def is_open(self) -> bool:
        """Whether a live handle is currently held."""
        return self._handle is not None

    # -- lifecycle ----------------------------------------------------------

    def handle(self) -> Any:
        """Return the live handle, opening it on first use.

        Reused on every later call, which is the point of sharing the object:
        repeated operations against one configured connection do not each open
        their own.
        """
        if self._handle is None:
            self._handle = _db.get_connection(self._config, ctx=self._ctx)
        return self._handle

    def close(self) -> None:
        """Close the live handle if one is open. Safe to call more than once.

        Owned by runtime shutdown, not by consumers: this object is shared, and
        an individual consumer closing it would take the handle away from every
        other holder.
        """
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.close()
        except Exception:  # noqa: BLE001 — a failed close must not mask shutdown.
            pass

    # -- operations ---------------------------------------------------------

    def execute(self, sql_text: str, named_params: dict[str, Any],
                result_mode: str) -> Any:
        """Execute parameter-bound SQL text through the provider backend."""
        return _db.execute_sql(self.handle(), sql_text, named_params, result_mode)

    def execute_procedure(self, routine: str, named_params: dict[str, Any]) -> None:
        """Call a stored procedure through the provider backend."""
        return _db.execute_procedure(self.handle(), routine, named_params)

    def execute_function(self, routine: str, named_params: dict[str, Any]) -> Any:
        """Call a function and return its scalar through the provider backend."""
        return _db.execute_function(self.handle(), routine, named_params)

    def execute_function_rows(self, routine: str,
                              named_params: dict[str, Any]) -> list[dict[str, Any]]:
        """Call a set-returning function and return its rows through the backend."""
        return _db.execute_function_rows(self.handle(), routine, named_params)


def build_connections(ctx: Any) -> dict[str, Connection]:
    """Build one Connection per configured connection, keyed by name.

    Built once at context composition so every consumer that later names a
    connection receives the same instance. Duplicate names are a configuration
    error rather than a silent last-one-wins: two records under one name is how
    a consumer ends up on a different database from the one it asked for.
    """
    records = list(getattr(ctx, "connections", None)
                   or getattr(ctx, "db_connections", None) or [])
    built: dict[str, Connection] = {}
    for record in records:
        connection = Connection(record, ctx=ctx)
        if connection.name in built:
            raise ConfigError(
                f"connections declares '{connection.name}' more than once. A name "
                "must identify one database, or a consumer naming it cannot know "
                "which one it reached."
            )
        built[connection.name] = connection
    return built


def shared_connection(ctx: Any, name: str) -> Connection:
    """Return the shared Connection named ``name``.

    The one lookup surface for consumers. Nothing resolves a connection config
    or opens a handle itself: naming a connection yields the object every other
    consumer of that name already holds.

    Raises
    ------
    ConfigError
        When no connection is configured under that name, or the shared
        connections were never built for this context.
    """
    if not name:
        raise ConfigError("connection: no connection name was given.")

    shared = getattr(ctx, "shared_connections", None)
    if not shared:
        raise ConfigError(
            "connection: shared connections are not built on this context. They "
            "are created once at composition by build_ctx_for_app."
        )
    if name not in shared:
        raise ConfigError(
            f"connection: '{name}' is not configured. Known connections: "
            f"{', '.join(sorted(shared)) or 'none'}."
        )
    return shared[name]
