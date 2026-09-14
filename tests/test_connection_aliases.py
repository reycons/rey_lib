"""A run may route a configured connection to another, without changing config.

An installation declares which connections exist and who each one connects as.
A particular run sometimes cannot use one of them -- a dev container holds one
login and not the credential the control connection names -- and the run still
has to reach that database, because every run mints its identity in
``control.run_manifest`` before logging opens.

The alias says so without editing the installation. ``control`` stays declared
as it is; the run separately states which connection it reaches for it. That
keeps two facts distinguishable that an edit would collapse into one:

    configured            rey_control_logger
    effective this run    rey_llm_developer

It is honoured in exactly one place -- connection resolution -- so ``Control``,
the indexer, DB admin and every other consumer get the replacement through the
path they already use, and none of them learns why.

Two properties here are load-bearing rather than incidental:

- **Identity.** Under an alias, the configured name and the runtime name yield
  the SAME Connection object. Translating after the resolver's cache key would
  build two Connections over one definition, and two live handles where this
  module's whole contract is one.
- **Chains are refused.** ``a=b`` beside ``b=c`` is an error, not a one-hop
  resolution, because which name a run reached would otherwise depend on the
  order the map happened to be read in.
"""

from __future__ import annotations

import argparse
from typing import Any, Optional

import pytest

from rey_lib.config.cli import _apply_connection_aliases, add_config_args
from rey_lib.config.config_namespace import Namespace
from rey_lib.db.connection import (
    CONNECTION_ALIASES_ATTR,
    connection_owner,
    shared_connection,
    validate_connection_aliases,
)
from rey_lib.errors.error_utils import ConfigError

CONFIGURED = "control"
RUNTIME = "rey_llm_developer"
THIRD = "other"
SPARE = "spare"


def _ctx(aliases: Optional[dict[str, str]] = None,
         config_path: str = "/tmp/aliases.yaml") -> Namespace:
    """A context declaring three connections, optionally carrying aliases.

    Args:
        aliases: The run's alias map, or None to declare none.
        config_path: Identity the resolver caches under; distinct per test so
            one test's held Connection is never another's.

    Returns:
        A context suitable for connection resolution.
    """
    ctx = Namespace({
        "config_path": config_path,
        "connections": [
            {"name": name, "provider": "duckdb", "database": ":memory:"}
            for name in (CONFIGURED, RUNTIME, THIRD, SPARE)
        ],
    })
    if aliases is not None:
        object.__setattr__(ctx, CONNECTION_ALIASES_ATTR, aliases)
    return ctx


def _args(*alias_items: str) -> argparse.Namespace:
    """Return parsed CLI args carrying the given --connection-alias values."""
    parser = argparse.ArgumentParser()
    add_config_args(parser)
    argv: list[str] = []
    for item in alias_items:
        argv += ["--connection-alias", item]
    return parser.parse_args(argv)


@pytest.fixture(autouse=True)
def _release_connections() -> Any:
    """Close every Connection the runtime held, so tests never share handles."""
    yield
    connection_owner().close()


class TestAnAliasRoutesAConnection:
    """What an alias does to resolution."""

    def test_without_an_alias_a_name_resolves_to_itself(self) -> None:
        """The ordinary case is unchanged: no alias, no translation."""
        ctx = _ctx(config_path="/tmp/a1.yaml")

        assert shared_connection(ctx, CONFIGURED).name == CONFIGURED

    def test_an_aliased_name_resolves_to_the_runtime_connection(self) -> None:
        """Asking for the configured name reaches the connection named instead."""
        ctx = _ctx({CONFIGURED: RUNTIME}, config_path="/tmp/a2.yaml")

        assert shared_connection(ctx, CONFIGURED).name == RUNTIME

    def test_an_unaliased_name_is_untouched(self) -> None:
        """An alias routes what it names and nothing else."""
        ctx = _ctx({CONFIGURED: RUNTIME}, config_path="/tmp/a3.yaml")

        assert shared_connection(ctx, THIRD).name == THIRD

    def test_both_names_yield_one_object(self) -> None:
        """The identity property: one definition, one Connection, one handle.

        This is what pins the translation ahead of the resolver's cache key. It
        fails the moment the order is reversed, which a name-only assertion
        would not catch.
        """
        ctx = _ctx({CONFIGURED: RUNTIME}, config_path="/tmp/a4.yaml")

        assert shared_connection(ctx, CONFIGURED) is shared_connection(ctx, RUNTIME)

    def test_the_configuration_is_not_rewritten(self) -> None:
        """Configured and effective stay separately readable.

        An alias that edited the declaration would answer "what did this run
        reach" and destroy "what does this installation declare".
        """
        ctx = _ctx({CONFIGURED: RUNTIME}, config_path="/tmp/a5.yaml")

        shared_connection(ctx, CONFIGURED)

        assert [record["name"] for record in ctx.connections] == [
            CONFIGURED, RUNTIME, THIRD, SPARE,
        ]


class TestAnUnusableAliasIsRefusedAtLaunch:
    """Routing that cannot be honoured stops the run before it works."""

    def test_an_unknown_target_is_refused(self) -> None:
        """Aliasing to a connection nothing declares reaches no database."""
        with pytest.raises(ConfigError, match="not a configured connection"):
            validate_connection_aliases(_ctx({CONFIGURED: "absent"}))

    def test_an_unknown_source_is_refused(self) -> None:
        """Aliasing from a name nothing declares can never be asked for."""
        with pytest.raises(ConfigError, match="not a configured connection"):
            validate_connection_aliases(_ctx({"absent": RUNTIME}))

    def test_a_self_alias_is_refused(self) -> None:
        """A connection aliased to itself states nothing."""
        with pytest.raises(ConfigError, match="itself"):
            validate_connection_aliases(_ctx({CONFIGURED: CONFIGURED}))

    def test_a_chain_is_refused_rather_than_resolved_one_hop(self) -> None:
        """Chains are an error, not a truncation.

        Resolving the first hop and silently leaving the second unreached makes
        the effective name depend on evaluation order. Refusing is also what
        makes a cycle impossible without a separate cycle check.
        """
        with pytest.raises(ConfigError, match="chain"):
            validate_connection_aliases(_ctx({CONFIGURED: THIRD, THIRD: RUNTIME}))

    def test_no_alias_validates_trivially(self) -> None:
        """A run declaring no alias is the ordinary case, not a special one."""
        validate_connection_aliases(_ctx())


class TestTheRunCarriesTheAlias:
    """How an alias arrives and how it travels."""

    def test_the_cli_argument_records_the_alias(self) -> None:
        """--connection-alias CONFIGURED=RUNTIME lands on the context."""
        ctx = _ctx()

        _apply_connection_aliases(ctx, _args(f"{CONFIGURED}={RUNTIME}"))

        assert getattr(ctx, CONNECTION_ALIASES_ATTR) == {CONFIGURED: RUNTIME}

    def test_the_argument_is_repeatable(self) -> None:
        """A run may route more than one connection."""
        ctx = _ctx()

        _apply_connection_aliases(ctx, _args(f"{CONFIGURED}={RUNTIME}", f"{THIRD}={RUNTIME}"))

        assert getattr(ctx, CONNECTION_ALIASES_ATTR) == {CONFIGURED: RUNTIME, THIRD: RUNTIME}

    def test_an_inherited_alias_survives(self) -> None:
        """A step restored from a ctx snapshot keeps the launcher's routing.

        The coordinator deep-copies the whole context into each step, so the map
        arrives on ctx. A child that states nothing must not lose it -- that is
        what makes the alias belong to the run rather than to one process.
        """
        ctx = _ctx({CONFIGURED: RUNTIME})

        _apply_connection_aliases(ctx, _args())

        assert getattr(ctx, CONNECTION_ALIASES_ATTR) == {CONFIGURED: RUNTIME}

    def test_a_child_argument_merges_over_what_it_inherited(self) -> None:
        """The child's own alias wins for the name it states, and only that one."""
        ctx = _ctx({CONFIGURED: RUNTIME, THIRD: SPARE})

        _apply_connection_aliases(ctx, _args(f"{CONFIGURED}={SPARE}"))

        assert getattr(ctx, CONNECTION_ALIASES_ATTR) == {CONFIGURED: SPARE, THIRD: SPARE}

    def test_a_merge_that_forms_a_chain_is_refused(self) -> None:
        """The one-hop rule holds over the MERGED map, not each half.

        A parent aliasing ``other`` and a child aliasing onto ``other`` each
        declare one hop; together they are a chain neither side wrote. Checking
        only what the child supplied would let exactly that through, so the run
        is refused on the map it will actually resolve with.
        """
        ctx = _ctx({THIRD: RUNTIME})

        with pytest.raises(ConfigError, match="chain"):
            _apply_connection_aliases(ctx, _args(f"{CONFIGURED}={THIRD}"))

    @pytest.mark.parametrize("malformed", ["control", "=runtime", "control=", "  =  "])
    def test_a_malformed_pair_stops_the_run(self, malformed: str) -> None:
        """Both sides are required, as --set requires both sides of KEY=VALUE."""
        with pytest.raises(SystemExit):
            _apply_connection_aliases(_ctx(), _args(malformed))

    def test_an_unusable_alias_is_refused_as_the_context_is_built(self) -> None:
        """Validation happens at build time, not at the first query."""
        with pytest.raises(ConfigError):
            _apply_connection_aliases(_ctx(), _args(f"{CONFIGURED}=absent"))
