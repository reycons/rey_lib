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

Who holds it
------------
The runtime, through ``ConnectionOwner``. A context is discovery: it says which
connections are configured and what each one is. It does not carry the objects.

That distinction is the whole of the objectification, and getting it wrong is
what broke the console's database tree. ``shared_connection`` looked the object
up in a dict on the ctx, so sharing was scoped to a ctx rather than to the
runtime: two contexts for one installation held two objects and two handles, and
a context built any other way held none. The Console resolves a fresh context
per request, so every installation-scoped request found nothing and reported the
connection as unconfigured.

A configured connection has one runtime Connection object. Any context capable
of identifying that configured connection resolves to that same object.
"""

from __future__ import annotations

from typing import Any, Optional

from rey_lib.db.db_adapter import DBAdapter
from rey_lib.errors.error_utils import ConfigError

__all__ = [
    "Connection",
    "ConnectionOwner",
    "build_connections",
    "connection_owner",
    "shared_connection",
]

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


class ConnectionOwner:
    """Holds one Connection per configured connection, for the runtime.

    The authority the objectification intended. A caller names a connection and
    receives the object for it; whether that object already existed is this
    object's business and nobody else's.

    Identity is the *configured* connection -- the installation config it was
    declared in, and its name within that config. Not the endpoint: two
    installations that each configure ``Rey Apps`` against one database are two
    configured connections, and collapsing them would be deduplication the
    configuration model never asked for. Not the bare name, which means
    different databases in different installations. Not the context object,
    because a context is not an identity -- it is one of several ways to arrive
    at the same one.

    Nothing is opened here. ``Connection`` opens lazily on first use, so holding
    one costs nothing until somebody works through it.
    """

    def __init__(self) -> None:
        """Hold nothing yet."""
        self._owned: dict[tuple[str, str], Connection] = {}

    def __repr__(self) -> str:
        return f"<ConnectionOwner {len(self._owned)} held>"

    @property
    def name(self) -> str:
        """A label for runtime collection reporting."""
        return "connections"

    def resolve(self, ctx: Any, name: str) -> Connection:
        """Return the Connection for the named configured connection.

        Built from the definition ``ctx`` carries the first time it is asked
        for, and returned as-is afterwards.

        Parameters
        ----------
        ctx : Any
            A context that can identify the configured connection. Used for
            discovery -- its ``connections`` list and its ``config_path`` -- and
            carried into the Connection so a credential naming an environment
            variable can be read as the handle opens.
        name : str
            The connection's name within that configuration.

        Raises
        ------
        ConfigError
            When no name is given, or the configuration declares none by that
            name.
        """
        if not name:
            raise ConfigError("connection: no connection name was given.")

        key = (_config_identity(ctx), str(name))
        held = self._owned.get(key)
        if held is not None:
            return held

        definitions = _connection_definitions(ctx)
        record = definitions.get(str(name))
        if record is None:
            raise ConfigError(
                f"connection: '{name}' is not configured. Known connections: "
                f"{', '.join(sorted(definitions)) or 'none'}."
            )
        built = Connection(record, ctx=ctx)
        self._owned[key] = built
        return built

    def close(self) -> None:
        """Close every connection held, and hold none.

        The last close, called by the boundary that owns runtime shutdown. One
        failure does not stop the rest -- the alternative is that a single
        broken handle leaves the others open.
        """
        held, self._owned = list(self._owned.values()), {}
        for connection in held:
            connection.close()


_OWNER = ConnectionOwner()


def connection_owner() -> ConnectionOwner:
    """Return the runtime's connection owner."""
    return _OWNER


def _config_identity(ctx: Any) -> str:
    """Return the configuration a context was built from.

    ``config_path`` is set on every context built from a path, which is both
    ways one is built, so it identifies the installation without a second
    vocabulary for saying which one this is.
    """
    return str(getattr(ctx, "config_path", "") or "")


def _connection_definitions(ctx: Any) -> dict[str, Any]:
    """Return the configured connection records, keyed by name.

    Discovery. A duplicate name is a configuration error rather than a silent
    last-one-wins: two records under one name is how a consumer ends up on a
    different database from the one it asked for.
    """
    records = list(getattr(ctx, "connections", None)
                   or getattr(ctx, "db_connections", None) or [])
    found: dict[str, Any] = {}
    for record in records:
        name = getattr(record, "name", None)
        if name is None and isinstance(record, dict):
            name = record.get("name")
        if not name:
            raise ConfigError("connection: a connection config must carry a name.")
        if str(name) in found:
            raise ConfigError(
                f"connections declares '{name}' more than once. A name "
                "must identify one database, or a consumer naming it cannot know "
                "which one it reached."
            )
        found[str(name)] = record
    return found


def build_connections(ctx: Any) -> dict[str, Connection]:
    """Return one Connection per configured connection, keyed by name.

    Every one resolved through the runtime owner, so this returns the same
    objects a later ``shared_connection`` will. It no longer decides who holds
    them -- it did once, and a context holding the only copy is what scoped
    sharing to a context instead of to the runtime.

    What it still is: the eager check that a configuration is usable, which
    fails at composition rather than at the first query.
    """
    return {
        name: _OWNER.resolve(ctx, name)
        for name in _connection_definitions(ctx)
    }


def shared_connection(ctx: Any, name: str) -> Connection:
    """Return the shared Connection named ``name``.

    The one lookup surface for consumers. Nothing resolves a connection config
    or opens a handle itself: naming a connection yields the object every other
    consumer of that name already holds.

    Any context that can identify the configured connection resolves to the same
    object, whether or not it went through a composition step. That is what lets
    a Console request resolve a fresh context per request and still reach the
    connection everything else is using.

    Raises
    ------
    ConfigError
        When no connection is configured under that name.
    """
    return _OWNER.resolve(ctx, name)
