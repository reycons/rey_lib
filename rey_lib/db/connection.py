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
    "CONNECTION_ALIASES_ATTR",
    "build_connections",
    "connection_owner",
    "call_routine",
    "is_database_connection",
    "is_http_connection",
    "shared_connection",
    "validate_connection_aliases",
]

# Where a run's connection aliases live on the context. An ordinary ctx
# attribute, deliberately: the pipeline coordinator deep-copies the whole
# context into each step's snapshot, so a map kept here crosses the process
# boundary with the run and a module global would not.
CONNECTION_ALIASES_ATTR = "connection_aliases"

# The provider whose records are HTTP endpoints, not databases. Named here, at
# the dispatch, and nowhere else: a consumer asks is_database_connection.
_HTTP_PROVIDER = "http"
#: Where a governed HTTP job's connection is authoritative; also the namespace
#: the owner holds it under, apart from the declared connections (backlog 838).
_GOVERNED_NAMESPACE = "control.http_adapter"

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
    def llm(self) -> bool:
        """Whether a language model may be given this connection to query.

        Declared by the connection, because what a connection reaches and under
        whose role is the connection's own fact. Absent means no: a connection
        that has not said an LLM may use it has not said so, and is offered
        nowhere.

        Read the same way ``provider`` is -- the config record travels whole,
        and this asks it one more question.
        """
        value = getattr(self._config, "llm", None)
        if value is None and isinstance(self._config, dict):
            value = self._config.get("llm")
        return bool(value)

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
        self._owned: dict[tuple[str, ...], Connection] = {}
        #: Each held governed connection's definition, to check the next asker
        #: against (backlog 838). Never a value read from the environment.
        self._definitions: dict[tuple[str, ...], dict[str, Any]] = {}

    def __repr__(self) -> str:
        return f"<ConnectionOwner {len(self._owned)} held>"

    @property
    def name(self) -> str:
        """A label for runtime collection reporting."""
        return "connections"

    def resolve(
        self, ctx: Any, name: str, definition: Optional[dict[str, Any]] = None,
    ) -> Connection:
        """Return the Connection for the named configured connection.

        Built from the definition ``ctx`` carries the first time it is asked
        for, and returned as-is afterwards.

        **A GOVERNED HTTP CONNECTION** (backlog 838) is handed in as
        ``definition`` -- a governed job's connection record, authoritative in
        ``control.http_adapter`` -- rather than declared in ``connections``. It
        is held in a namespace of its own, so it never collides with a declared
        connection of the same name, and is never translated as a declared name
        is. Asked again with an equal definition, the held one is returned; with
        a different one, it is refused: two governed jobs naming one connection
        must agree on what it is.

        Parameters
        ----------
        ctx : Any
            A context that can identify the configured connection. Used for
            discovery -- its ``connections`` list and its ``config_path`` -- and
            carried into the Connection so a credential naming an environment
            variable can be read as the handle opens.
        name : str
            The connection's name within that configuration.
        definition : dict, optional
            A governed job's connection record. None resolves a declared one.

        Raises
        ------
        ConfigError
            When no name is given, the configuration declares none by that
            name, or a governed definition disagrees with the one held.
        """
        if not name:
            raise ConfigError("connection: no connection name was given.")
        if definition is not None:
            return self._governed(ctx, str(name), definition)

        # Translate BEFORE the cache key is built. The contract above is that
        # naming a connection yields the object every other consumer of that
        # name already holds; keying on the requested name would build a second
        # Connection over one definition, and two handles where the estate
        # guarantees one.
        name = _effective_connection_name(ctx, name)

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
        # AN HTTP ENDPOINT IS A CONNECTION TOO, declared in the same list and
        # held by the same owner -- only the object differs. Imported here so
        # rey_lib.web_utils is never an import-time dependency of rey_lib.db.
        if _provider_of(record) == _HTTP_PROVIDER:
            from rey_lib.web_utils import HttpConnection  # noqa: PLC0415

            built: Any = HttpConnection(record, ctx=ctx)
        else:
            built = Connection(record, ctx=ctx)
        self._owned[key] = built
        return built

    def _governed(self, ctx: Any, name: str, definition: dict[str, Any]) -> Any:
        """The held HttpConnection for one governed connection definition."""
        key = (_config_identity(ctx), _GOVERNED_NAMESPACE, name)
        wanted = dict(definition)
        held = self._owned.get(key)
        if held is not None:
            differing = sorted(
                field for field in set(wanted) | set(self._definitions[key])
                if wanted.get(field) != self._definitions[key].get(field)
            )
            if differing:
                # logs reaches the control layer, which uses this module.
                from rey_lib.logs import AppLogger  # noqa: PLC0415

                AppLogger(__name__).debug(
                    "connection: refused -- governed connection %s asked for with differing %s",
                    name, ", ".join(differing),
                )
                raise ConfigError(
                    f"connection: governed connection '{name}' is already held with a "
                    f"different definition ({', '.join(differing)} differ). Every governed "
                    f"job naming one connection must agree on it."
                )
            return held
        from rey_lib.web_utils import HttpConnection  # noqa: PLC0415

        built = HttpConnection({**wanted, "name": name}, ctx=ctx)
        self._owned[key] = built
        self._definitions[key] = wanted
        return built

    def close(self) -> None:
        """Close every connection held, and hold none.

        The last close, called by the boundary that owns runtime shutdown. One
        failure does not stop the rest -- the alternative is that a single
        broken handle leaves the others open.
        """
        held, self._owned = list(self._owned.values()), {}
        self._definitions = {}
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


def _alias_map(ctx: Any) -> dict[str, str]:
    """Return this run's connection aliases, keyed by configured name.

    Absent or empty is the ordinary case and answers an empty mapping: a run
    that declared no alias routes exactly as the installation says.
    """
    aliases = getattr(ctx, CONNECTION_ALIASES_ATTR, None)
    if not aliases:
        return {}
    if hasattr(aliases, "items"):
        return {str(key): str(value) for key, value in aliases.items()}
    return {}


def _effective_connection_name(ctx: Any, name: str) -> str:
    """Return the connection this run actually uses for ``name``.

    One hop, never a chain. ``validate_connection_aliases`` refuses an alias
    whose target is itself aliased, so a single lookup here is the whole
    translation and there is no order-dependent second hop to reason about.

    The configured name is not altered anywhere -- ``ctx.control.connection``
    still reads what the YAML declared. Configuration says what is configured;
    this says what this run reaches.
    """
    return _alias_map(ctx).get(str(name), str(name))


def validate_connection_aliases(ctx: Any) -> None:
    """Refuse an unusable alias map before any application work starts.

    Called once as the context is built rather than at first use. An alias that
    cannot be honoured is a routing error for the whole run, and discovering it
    at the first query means discovering it after work has been done that the
    run cannot record.

    Raises
    ------
    ConfigError
        When an alias names an unconfigured connection on either side, when a
        connection is aliased to itself, or when aliases form a chain.
    """
    aliases = _alias_map(ctx)
    if not aliases:
        return

    definitions = _connection_definitions(ctx)
    known = ", ".join(sorted(definitions)) or "none"

    for configured, runtime in aliases.items():
        if configured not in definitions:
            raise ConfigError(
                f"connection alias '{configured}={runtime}': '{configured}' is not a "
                f"configured connection, so nothing resolves it. Known connections: {known}."
            )
        if runtime not in definitions:
            raise ConfigError(
                f"connection alias '{configured}={runtime}': '{runtime}' is not a "
                f"configured connection. Known connections: {known}."
            )
        if configured == runtime:
            raise ConfigError(
                f"connection alias '{configured}={runtime}' aliases a connection to "
                "itself, which states nothing. Remove it."
            )

    # A chain is refused rather than resolved one hop: which name a run reached
    # would otherwise depend on evaluation order, and a quietly unreached target
    # is exactly the silent wrong answer this mechanism must not introduce.
    for configured, runtime in sorted(aliases.items()):
        if runtime in aliases:
            raise ConfigError(
                f"connection aliases form a chain: '{configured}={runtime}' and "
                f"'{runtime}={aliases[runtime]}'. An alias is one hop -- name the "
                f"connection each configured name should reach directly."
            )


def _provider_of(record: Any) -> str:
    """The provider a connections[] record declares, whether a Namespace or a dict."""
    provider = getattr(record, "provider", None)
    if provider is None and isinstance(record, dict):
        provider = record.get("provider")
    return str(provider or "")


def is_database_connection(item: Any) -> bool:
    """Whether ``item`` is a database connection -- a built one or its record.

    The question a database-only consumer asks of the heterogeneous connection
    list (backlog 673): a list of database choices, a command that opens a
    handle. Answered beside the dispatch that builds each kind, so no consumer
    names the providers that are not databases. The registry itself still
    resolves and returns every connection.

    Args:
        item: A built connection, or a ``connections[]`` record.

    Returns:
        True for a database ``Connection`` or a record that resolves to one --
        both carry the declared ``provider``, as an ``HttpConnection`` does.
    """
    return _provider_of(item) != _HTTP_PROVIDER


def is_http_connection(item: Any) -> bool:
    """Whether ``item`` is an HTTP connection -- a built one or its record.

    POSITIVE SELECTION (backlog 668): the http provider, not "anything that is
    not a database", so a later non-database provider is never offered where an
    HTTP connection is asked for. Answered beside the dispatch, as
    :func:`is_database_connection` is.

    Args:
        item: A built connection, or a ``connections[]`` record.

    Returns:
        True for an ``HttpConnection`` or a record that resolves to one.
    """
    return _provider_of(item) == _HTTP_PROVIDER


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


def shared_connection(
    ctx: Any, name: str, definition: Optional[dict[str, Any]] = None,
) -> Connection:
    """Return the shared Connection named ``name``.

    The one lookup surface for consumers. Nothing resolves a connection config
    or opens a handle itself: naming a connection yields the object every other
    consumer of that name already holds.

    Any context that can identify the configured connection resolves to the same
    object, whether or not it went through a composition step. That is what lets
    a Console request resolve a fresh context per request and still reach the
    connection everything else is using.

    A governed HTTP job hands in its connection ``definition`` (backlog 838):
    the owner holds that HttpConnection, apart from the declared connections,
    for every job naming it.

    Raises
    ------
    ConfigError
        When no connection is configured under that name, or a governed
        definition disagrees with the one held.
    """
    return _OWNER.resolve(ctx, name, definition)
