"""HTTP choices, selected positively (backlog 668).

http_connections offers the connections is_http_connection accepts -- the http
provider, not "anything that is not a database" -- and http_operations offers
the installation's enabled HTTP operations, as the control database answers
them (backlog 813). connections and database_connections are unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rey_lib.config.applications import _choices
from rey_lib.config.config_namespace import Namespace
from rey_lib.db.connection import is_database_connection, is_http_connection, shared_connection


def _records(*records: dict) -> SimpleNamespace:
    return SimpleNamespace(connections=[Namespace(dict(record)) for record in records])


_DB = {"name": "control", "provider": "postgres"}
_HTTP = {"name": "web", "provider": "http", "base_url": "https://api.example.test/",
         "timeout_seconds": 5}
_OTHER = {"name": "bucket", "provider": "s3"}


def _offered(source: str, ctx: SimpleNamespace) -> tuple[str, ...]:
    return _choices({"name": "p", "possible_values_from": source}, ctx, "loader")


class TestThePredicate:
    def test_http_records_and_connections_are_http(self) -> None:
        ctx = SimpleNamespace(
            config_path="/installations/test/http_choices.yaml",
            connections=[SimpleNamespace(**_HTTP)],
        )

        assert is_http_connection(_HTTP)
        assert is_http_connection(shared_connection(ctx, "web"))

    def test_a_database_or_another_provider_is_not(self) -> None:
        assert not is_http_connection(_DB)
        assert not is_http_connection(_OTHER)

    def test_another_provider_is_neither(self) -> None:
        # Positive selection: a future non-database provider is not offered
        # where an HTTP connection is asked for. (Whether it is a database is
        # is_database_connection's answer, unchanged here.)
        assert not is_http_connection(_OTHER)
        assert is_database_connection(_OTHER)


class TestChoiceSources:
    def test_http_connections_offers_http_only(self) -> None:
        assert _offered("http_connections", _records(_DB, _HTTP, _OTHER)) == ("web",)

    def test_the_other_connection_sources_are_unchanged(self) -> None:
        ctx = _records(_DB, _HTTP)

        assert _offered("connections", ctx) == ("control", "web")
        assert _offered("database_connections", ctx) == ("control",)

    def test_http_operations_offers_the_enabled_operations(self) -> None:
        asked: list[str] = []
        control = SimpleNamespace(http_operation=lambda installation: asked.append(installation) or [
            {"adapter_key": "lookup", "is_enabled": True},
            {"adapter_key": "dormant", "is_enabled": False},
            {"adapter_key": "fetch", "is_enabled": True},
        ])
        ctx = SimpleNamespace(shared_control=control, installation=SimpleNamespace(name="local"))

        assert _offered("http_operations", ctx) == ("fetch", "lookup")
        assert asked == ["local"]

    def test_a_runtime_without_control_offers_none(self) -> None:
        # Absence of control is ordinary; the transform refuses where it resolves.
        assert _offered("http_operations", SimpleNamespace(shared_control=None)) == ()
