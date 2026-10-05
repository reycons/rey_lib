"""Database-only consumers ask for database connections (backlog 673).

The connection registry is heterogeneous -- a provider: http record is a
connection too, resolved and returned like any other. A consumer that can only
use a database asks is_database_connection, or declares the
"database_connections" choice source; "connections" keeps meaning every
provider.
"""

from __future__ import annotations

from types import SimpleNamespace

from rey_lib.config.applications import _choices
from rey_lib.config.bootstrap import _ai_connections
from rey_lib.config.config_namespace import Namespace
from rey_lib.db.connection import (
    Connection, build_connections, is_database_connection, shared_connection,
)
from rey_lib.web_utils import HttpConnection


def _db(name: str, provider: str = "postgres", **kw: object) -> SimpleNamespace:
    return SimpleNamespace(name=name, provider=provider, **kw)


def _http(name: str = "web") -> SimpleNamespace:
    return SimpleNamespace(
        name=name, provider="http", base_url="https://api.example.test/", timeout_seconds=5,
    )


def _ctx(path: str, *records: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        config_path=f"/installations/test/{path}.yaml", connections=list(records),
    )


class TestThePredicate:
    def test_database_records_and_connections_are_databases(self) -> None:
        ctx = _ctx("predicate_db", _db("control"), _db("files", "duckdb"))

        assert is_database_connection(_db("control"))
        assert is_database_connection(_db("files", "duckdb"))
        assert is_database_connection({"name": "x", "provider": "sqlserver"})
        assert is_database_connection(shared_connection(ctx, "control"))

    def test_an_http_record_and_connection_are_not(self) -> None:
        ctx = _ctx("predicate_http", _http())

        assert not is_database_connection(_http())
        assert not is_database_connection({"name": "web", "provider": "http"})
        assert not is_database_connection(shared_connection(ctx, "web"))


class TestTheRegistryStaysHeterogeneous:
    def test_build_connections_still_returns_every_connection(self) -> None:
        built = build_connections(_ctx("registry", _db("control"), _http()))

        assert isinstance(built["control"], Connection)
        assert isinstance(built["web"], HttpConnection)


class TestChoiceSources:
    """Configuration records arrive as the config loader's Namespace."""

    @staticmethod
    def _records(*records: SimpleNamespace) -> list[Namespace]:
        return [Namespace(dict(vars(record))) for record in records]

    def test_database_connections_offers_databases_only(self) -> None:
        ctx = SimpleNamespace(connections=self._records(
            _db("control"), _http(), _db("files", "duckdb"),
        ))

        assert _choices({"name": "connection", "possible_values_from": "database_connections"},
                        ctx, "loader") == ("control", "files")

    def test_connections_still_offers_every_provider(self) -> None:
        ctx = SimpleNamespace(connections=self._records(_db("control"), _http()))

        assert _choices({"name": "connection", "possible_values_from": "connections"},
                        ctx, "loader") == ("control", "web")


class TestAiConnections:
    def test_an_http_connection_is_never_offered_to_a_model(self) -> None:
        ctx = _ctx("ai", _db("control", llm=True), _http())

        assert _ai_connections(ctx) == ("control",)
