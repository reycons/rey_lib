"""A logical connection and its physical session are two things (#1309).

The handle owns an Engine for its life and a session only while it is in use:
one session for every operation in a row, never a second from the bounded pool,
given back when idle, ended by ``release_idle`` and never while a cursor or a
transaction still holds it. The owner ends every idle session at the runtime's
wait, and the hosted loop is that wait.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.db import _sqlalchemy as boundary
from rey_lib.db._sqlalchemy import (
    POOL_CHECKOUT_SECONDS,
    POOL_MAX_OVERFLOW,
    POOL_SIZE,
    ReyConnection,
    ReyCursor,
    core_connection,
    open_connection,
    own_connection,
)
from rey_lib.db.connection import Connection, ConnectionOwner
from rey_lib.errors.error_utils import DatabaseError
from rey_lib.run.hosted_objects import HostedObjects


class _Cursor:
    def __init__(self) -> None:
        self.closed = False
        self.description = ("a",)

    def close(self) -> None:
        self.closed = True


class _Driver:
    """The DBAPI connection under a session."""

    def cursor(self) -> _Cursor:
        return _Cursor()


class _Session:
    """One physical session: alive from connect until close."""

    def __init__(self, engine: "_Engine") -> None:
        self.engine = engine
        self.closed = False
        self.transaction = False
        self.executed: list[str] = []
        self.connection = SimpleNamespace(driver_connection=_Driver())

    def execute(self, statement: Any) -> str:
        assert not self.closed, "an operation on a closed session"
        self.executed.append(str(statement))
        return "rows"

    def in_transaction(self) -> bool:
        return self.transaction

    def commit(self) -> None:
        self.executed.append("commit")

    def rollback(self) -> None:
        self.executed.append("rollback")

    def execution_options(self, **_options: Any) -> "_Session":
        return self

    def close(self) -> None:
        self.closed = True
        self.engine.live.discard(self)


class _Engine:
    """A bounded pool stand-in: it counts what is live and what was ever opened."""

    def __init__(self) -> None:
        self.opened: list[_Session] = []
        self.live: set[_Session] = set()
        self.disposed = 0
        self.refuse_when_held = False

    def connect(self) -> _Session:
        if self.refuse_when_held and self.live:
            from sqlalchemy.exc import TimeoutError as PoolTimeout  # noqa: PLC0415

            raise PoolTimeout("QueuePool limit reached")
        session = _Session(self)
        self.opened.append(session)
        self.live.add(session)
        return session

    def dispose(self) -> None:
        self.disposed += 1


def _handle() -> tuple[ReyConnection, _Engine]:
    engine = _Engine()
    return ReyConnection("postgres", engine, engine.connect()), engine


def test_sequential_operations_share_one_session_and_do_not_accumulate() -> None:
    conn, engine = _handle()

    for statement in ("select 1", "select 2", "select 3"):
        core_connection(conn).execute(statement)
    conn.commit()

    assert len(engine.opened) == 1
    assert len(engine.live) == 1
    assert engine.opened[0].executed == ["select 1", "select 2", "select 3", "commit"]


def test_release_idle_ends_the_session_and_the_next_operation_opens_a_fresh_one() -> None:
    conn, engine = _handle()
    core_connection(conn).execute("select 1")

    assert conn.release_idle() is True
    assert engine.live == set() and engine.disposed == 1
    # Idle twice in a row: nothing to end the second time.
    assert conn.release_idle() is False

    core_connection(conn).execute("select 2")
    assert len(engine.opened) == 2 and len(engine.live) == 1
    assert engine.opened[1].executed == ["select 2"]


def test_many_operations_with_idle_release_between_never_hold_two_sessions() -> None:
    conn, engine = _handle()

    for turn in range(20):
        core_connection(conn).execute(f"select {turn}")
        assert len(engine.live) == 1
        conn.release_idle()
        assert engine.live == set()

    # The opener's session served the first turn; each later turn opened its own.
    assert len(engine.opened) == 20


def test_an_open_cursor_keeps_the_session_until_it_is_closed() -> None:
    conn, engine = _handle()
    cursor = conn.cursor()

    assert isinstance(cursor, ReyCursor)
    assert cursor.description == ("a",)
    assert conn.release_idle() is False
    assert len(engine.live) == 1

    cursor.close()
    cursor.close()  # closing again changes nothing
    assert conn.release_idle() is True
    assert engine.live == set()


def test_a_cursor_used_as_a_context_manager_releases_on_exit() -> None:
    conn, engine = _handle()

    with conn.cursor() as cursor:
        assert not cursor.closed
        assert conn.release_idle() is False
    assert conn.release_idle() is True
    assert engine.live == set()


def test_a_begun_transaction_keeps_the_session() -> None:
    conn, engine = _handle()
    session = core_connection(conn)
    session.transaction = True

    assert conn.release_idle() is False
    assert len(engine.live) == 1

    session.transaction = False
    assert conn.release_idle() is True


def test_close_ends_the_session_and_disposes_the_engine_for_good() -> None:
    conn, engine = _handle()

    conn.close()
    conn.close()

    assert engine.live == set() and engine.disposed == 1
    assert conn.release_idle() is False
    with pytest.raises(DatabaseError, match="closed"):
        core_connection(conn)


def test_a_borrowed_connection_takes_the_idle_session_back_first() -> None:
    conn, engine = _handle()
    core_connection(conn).execute("select 1")
    first = engine.opened[0]

    with own_connection(conn, isolation_level="REPEATABLE READ") as borrowed:
        # The handle's idle session was given back before the checkout, so the
        # pool's one session is this one and no second is live.
        assert first.closed
        assert engine.live == {borrowed}
    assert engine.live == set()

    # The handle checks a fresh session out for its next operation.
    core_connection(conn).execute("select 2")
    assert len(engine.live) == 1


def test_a_borrowed_connection_while_a_cursor_is_open_is_refused_not_hung() -> None:
    conn, engine = _handle()
    engine.refuse_when_held = True
    cursor = conn.cursor()

    with pytest.raises(DatabaseError, match="open cursor or transaction"):
        with own_connection(conn):
            pass

    cursor.close()
    with own_connection(conn) as borrowed:
        assert engine.live == {borrowed}


def test_the_opener_bounds_the_engine_and_verifies_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: dict[str, Any] = {}
    engine = _Engine()

    def create_engine(url: Any, **options: Any) -> _Engine:
        created.update(options)
        created["url"] = url
        return engine

    monkeypatch.setattr(
        boundary, "_sqlalchemy",
        lambda: (SimpleNamespace(create=lambda **parts: parts), create_engine),
    )

    handle = open_connection("postgres", "postgresql+psycopg", username="u", password="p",
                             host="h", port=1, database="d", isolation_level="AUTOCOMMIT")

    assert (created["pool_size"], created["max_overflow"]) == (POOL_SIZE, POOL_MAX_OVERFLOW)
    assert created["pool_timeout"] == POOL_CHECKOUT_SECONDS
    assert created["pool_pre_ping"] is True
    assert created["isolation_level"] == "AUTOCOMMIT"
    assert (POOL_SIZE, POOL_MAX_OVERFLOW) == (1, 0)
    # Verified at opening: the session is live, and it is the one the first
    # operation uses.
    assert len(engine.live) == 1
    assert core_connection(handle) is engine.opened[0]


class _Handle:
    def __init__(self, releases: bool = True) -> None:
        self.releases = releases
        self.asked = 0

    def release_idle(self) -> bool:
        self.asked += 1
        return self.releases

    def close(self) -> None:
        pass


def test_a_connection_delegates_idle_release_to_its_handle_when_it_has_one() -> None:
    connection = Connection({"name": "control", "provider": "postgres"})

    assert connection.release_idle() is False  # nothing opened yet
    handle = _Handle()
    connection._handle = handle  # noqa: SLF001
    assert connection.release_idle() is True
    assert handle.asked == 1

    connection._handle = object()  # noqa: SLF001 -- a handle with no idle notion
    assert connection.release_idle() is False


def test_the_owner_ends_every_idle_session_and_counts_them() -> None:
    owner = ConnectionOwner()
    ending, holding = Connection({"name": "a"}), Connection({"name": "b"})
    ending._handle, holding._handle = _Handle(True), _Handle(False)  # noqa: SLF001
    owner._owned = {("i", "a"): ending, ("i", "b"): holding}  # noqa: SLF001

    assert owner.release_idle() == 1
    assert ending._handle.asked == 1 and holding._handle.asked == 1  # noqa: SLF001
    assert set(owner._owned) == {("i", "a"), ("i", "b")}  # noqa: SLF001


class _Channel:
    def __init__(self, commands: list[dict[str, Any]]) -> None:
        self._commands = commands
        self.emitted: list[dict[str, Any]] = []

    def commands(self) -> list[dict[str, Any]]:
        return self._commands

    def emit(self, message: dict[str, Any]) -> None:
        self.emitted.append(message)


class _Object:
    def __init__(self, emit: Any) -> None:
        self.emit = emit

    def projection(self) -> dict[str, Any]:
        return {}

    def release(self) -> None:
        pass


def test_the_hosted_loop_gives_sessions_back_after_every_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rey_lib.db import connection as owners

    class _Owner:
        released = 0

        def release_idle(self) -> int:
            _Owner.released += 1
            return 0

    monkeypatch.setattr(owners, "connection_owner", lambda: _Owner())
    hosted = HostedObjects(lambda subject, identity, emit: _Object(emit))
    channel = _Channel([
        {"command": "open", "subject": "s", "identity": {}},
        {"command": "projection", "object": "?"},
        {"command": "close"},
    ])

    hosted.serve(channel)

    # Once after each answered command; not after close, which ends serving.
    assert _Owner.released == 2
