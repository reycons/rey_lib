"""Connection is the only way a database handle is acquired at runtime.

Objectifying connections is only worth anything if the old path is gone. A
shared object beside a still-usable raw one is two paths, and the second is the
one that reopens a database someone else already holds.

These assert absence across the production tree, which is the half a boundary
rule cannot state on its own: the rule proves no *call* crosses a line, these
prove the old vocabulary is not present to be called.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PRODUCTION = REPO / "rey_lib"

# The rejection writer is deferred, not exempt: it resolves its connection from
# sql_configs rather than any connection registry, so repointing it changes
# which record it uses. Named once here so the exception stays visible.
_DEFERRED = PRODUCTION / "files" / "file_loader.py"


def _sources() -> list[Path]:
    """Every production module in this repository.

    Hidden directories are skipped: they hold tooling state and worktrees, not
    this repository's production code.
    """
    return sorted(
        p for p in PRODUCTION.rglob("*.py")
        if not any(part.startswith(".") or part == "__pycache__" for part in p.parts)
    )


def _calls_named(path: Path, name: str) -> list[int]:
    """Line numbers where ``name`` is called as an attribute."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == name]


class TestTheRetiredRegistryIsUnread:
    """db.connections was a second registry; nothing reads it."""

    def test_no_production_module_reads_ctx_db_connections(self) -> None:
        offenders = [
            str(p.relative_to(REPO)) for p in _sources()
            if 'ctx.db.connections' in p.read_text(encoding="utf-8")
            or '"db", None), "connections"' in p.read_text(encoding="utf-8")
        ]

        assert offenders == []


class TestNoRawConnectionAcquisition:
    """A handle comes from a shared Connection, never from a resolved config."""

    def test_resolve_connection_config_no_longer_exists(self) -> None:
        from rey_lib.db import procedure_map

        assert not hasattr(procedure_map, "resolve_connection_config")
        assert "resolve_connection_config" not in procedure_map.__all__

    def test_only_connection_opens_a_handle(self) -> None:
        offenders = [
            f"{p.relative_to(REPO)}:{line}"
            for p in _sources() if p.name not in ("connection.py", "db_adapter.py")
            and p != _DEFERRED
            for line in _calls_named(p, "get_connection")
        ]

        assert offenders == []

    def test_the_deferred_exception_is_exactly_one_site(self) -> None:
        """Stated so it shrinks to zero rather than quietly growing."""
        assert len(_calls_named(_DEFERRED, "get_connection")) == 1


class TestConnectionIsTheRuntimePath:
    """What consumers use instead."""

    def test_the_shared_lookup_is_the_public_surface(self) -> None:
        from rey_lib.db import connection

        # call_routine joins them as the connector's routine entry: it is where
        # a procedure-map binding is normalized into one provider-neutral call,
        # so nothing below this module reads a binding or a result mode.
        assert set(connection.__all__) == {
            "Connection", "ConnectionOwner", "build_connections",
            "call_routine", "connection_owner", "shared_connection"}

    def test_composition_checks_the_configuration_and_holds_nothing(self) -> None:
        """The boundary validates and registers; it does not become the holder.

        A context holding the only copy is what scoped sharing to a context
        rather than to the runtime, so a second context for the same
        installation got a second object and a context built any other way got
        none.
        """
        source = (PRODUCTION / "config" / "bootstrap.py").read_text(encoding="utf-8")

        assert "build_connections(ctx)" in source
        assert "connection_owner()" in source
        assert "ctx.shared_connections" not in source

    def test_configured_names_must_be_unique(self) -> None:
        from types import SimpleNamespace

        from rey_lib.db.connection import build_connections
        from rey_lib.errors.error_utils import ConfigError

        duplicate = [SimpleNamespace(name="control", provider="postgres"),
                     SimpleNamespace(name="control", provider="postgres")]

        with pytest.raises(ConfigError, match="more than once"):
            build_connections(SimpleNamespace(connections=duplicate))


class TestConsumersDoNotClose:
    """A shared object closed by one holder is taken from all of them."""

    def test_no_production_consumer_closes_a_shared_connection(self) -> None:
        # Connection.close exists and is shutdown's to call. Consumers that
        # borrowed a handle must not close it, so no consumer calls close on
        # what shared_connection gave it.
        offenders = []
        for path in _sources():
            if path.name == "connection.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "shared_connection(" not in text:
                continue
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "close"
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id in {"conn", "connection", "shared"}):
                    offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")

        assert offenders == []

    def test_close_is_owned_by_connection(self) -> None:
        from rey_lib.db.connection import Connection

        assert callable(Connection.close)
