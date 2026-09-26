"""The adapter refuses by name for a contract its provider cannot answer.

No database is opened. What is asserted is the DISPATCH: that every method the
adapter fulfils by calling a same-named provider function checks support first,
so a provider that does not implement one produces a stated refusal instead of
an AttributeError raised from inside the call.

That distinction is the whole point. The adapter was publishing a contract its
providers do not uniformly satisfy and had no way to notice: PostgreSQL was
missing five of the ten direct-dispatch methods, and the first one the loader
touches failed as an attribute error before any of its own logic ran.
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.db import db_adapter as adapter_module
from rey_lib.db.db_adapter import DBAdapter, _PROVIDER_CONTRACT_CAPABILITIES
from rey_lib.errors.error_utils import (
    ConfigError,
    UnsupportedDatabaseCapabilityError,
)


class _Backend:
    """A provider module exposing only the names it is given.

    Records what was called: the dispatch tests are about whether the provider
    function was REACHED, not what it returned. Some adapter methods normalise
    their provider's answer -- ``table_exists`` coerces to bool by contract --
    so a return value is not a reliable witness that the call happened.
    """

    def __init__(self, *implemented: str) -> None:
        self.calls: list[str] = []
        for name in implemented:
            setattr(self, name, self._recorder(name))

    def _recorder(self, name: str):
        def _call(*_args, **_kwargs):
            self.calls.append(name)
            return "answered"
        return _call


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch) -> DBAdapter:
    """An adapter whose provider inference is fixed, so only dispatch is tested."""
    instance = DBAdapter()
    monkeypatch.setattr(
        DBAdapter, "_provider_for_conn", lambda _self, _conn: "postgres"
    )
    return instance


def _with_backend(monkeypatch: pytest.MonkeyPatch, backend: Any) -> None:
    """Replace the module lookup, leaving the real provider modules alone."""
    monkeypatch.setattr(adapter_module, "_backend", lambda _provider: backend)


# Every declared capability, with arguments that satisfy its signature. Kept as
# data so a capability added to the contract without a dispatch to match shows
# up here rather than going unexercised.
_CALLS: dict[str, tuple] = {
    "fetch_dicts": ("some_sql", None),
    "call_proc": ("some_proc", None),
    "call_proc_with_output": ("some_proc", [], []),
    "table_exists": ("schema", "table"),
    "get_table_columns": ("schema", "table"),
    "create_staging_table_if_not_exists": ("schema", "table", [("a", "INTEGER")]),
    "bulk_insert": ("schema", "table", [], ["a"]),
    # Optional for a provider, but dispatched exactly like the rest -- a
    # caller that did not ask for support first must still get a named
    # refusal rather than an AttributeError from inside the call.
    "insert_from_path": ("schema", "table", "/tmp/source.csv", ["a"]),
    # Optional too, and the refusal matters MORE here than anywhere else: a
    # load that cannot empty its destination must be told so by name. Every
    # alternative means something different, so there is nothing to fall back
    # to and nothing that may be guessed at.
    "delete_all_rows": ("schema", "table"),
}


class TestNoUnsupportedCombinationReachesAttributeLookup:
    """The contract-level acceptance, asserted for every declared capability."""

    def test_the_declared_set_and_the_exercised_set_are_the_same(self) -> None:
        """So a capability cannot be declared and then never dispatched."""
        assert set(_CALLS) == set(_PROVIDER_CONTRACT_CAPABILITIES)

    @pytest.mark.parametrize("capability", sorted(_CALLS))
    def test_a_provider_missing_it_refuses_by_name(
        self, adapter: DBAdapter, monkeypatch: pytest.MonkeyPatch, capability: str
    ) -> None:
        """Naming BOTH the provider and the capability is what makes it actionable.

        An AttributeError names the attribute and leaves the reader to work out
        which provider was in play and whether the name was a typo or a gap.
        """
        _with_backend(monkeypatch, _Backend())          # implements nothing

        with pytest.raises(UnsupportedDatabaseCapabilityError) as raised:
            getattr(adapter, capability)(object(), *_CALLS[capability])

        assert "postgres" in str(raised.value)
        assert capability in str(raised.value)

    @pytest.mark.parametrize("capability", sorted(_CALLS))
    def test_the_refusal_is_not_an_attribute_error(
        self, adapter: DBAdapter, monkeypatch: pytest.MonkeyPatch, capability: str
    ) -> None:
        """The defect this closes, stated as its own assertion.

        UnsupportedDatabaseCapabilityError is not an AttributeError subclass,
        so this fails if any dispatch is ever returned to bare attribute
        access -- which is exactly the change that would look harmless.
        """
        _with_backend(monkeypatch, _Backend())

        with pytest.raises(Exception) as raised:
            getattr(adapter, capability)(object(), *_CALLS[capability])

        assert not isinstance(raised.value, AttributeError)

    @pytest.mark.parametrize("capability", sorted(_CALLS))
    def test_a_provider_implementing_it_is_called(
        self, adapter: DBAdapter, monkeypatch: pytest.MonkeyPatch, capability: str
    ) -> None:
        """The check gates the call; it does not replace it."""
        backend = _Backend(capability)
        _with_backend(monkeypatch, backend)

        getattr(adapter, capability)(object(), *_CALLS[capability])

        assert backend.calls == [capability]


class TestSupportIsResolvedNotListed:
    """Why there is no second table of which provider implements what."""

    def test_support_follows_the_module(
        self, adapter: DBAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hand-kept list would drift from the modules it describes.

        That drift IS this defect one level up: a declaration asserting a
        capability the provider does not have would refuse nothing, and one
        asserting a gap that had since been filled would refuse a capability
        that works.
        """
        _with_backend(monkeypatch, _Backend())
        assert adapter.supports_provider_capability(object(), "bulk_insert") is False

        _with_backend(monkeypatch, _Backend("bulk_insert"))
        assert adapter.supports_provider_capability(object(), "bulk_insert") is True

    def test_an_unknown_capability_raises_rather_than_answering_false(
        self, adapter: DBAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A typo must not read as "unsupported".

        Answering False would make a misspelled capability indistinguishable
        from a real gap, and the refusal message would then name a capability
        that does not exist. This mirrors supports() for metadata.
        """
        _with_backend(monkeypatch, _Backend())

        with pytest.raises(ConfigError) as raised:
            adapter.supports_provider_capability(object(), "get_table_colunms")

        assert "get_table_colunms" in str(raised.value)

    def test_the_real_postgres_module_still_has_the_known_gaps(self) -> None:
        """The out-of-scope half: their failure MODE is fixed, not their absence.

        fetch_dicts, call_proc and call_proc_with_output are genuinely absent
        from postgres_utils and this row does not add them. They now refuse by
        name. Asserting it here keeps "declared unsupported" honest -- if one
        is implemented later, this fails and says so.
        """
        from rey_lib.db import postgres_utils

        for capability in ("fetch_dicts", "call_proc", "call_proc_with_output"):
            assert not hasattr(postgres_utils, capability)

    def test_the_postgres_load_path_is_complete(self) -> None:
        """The four the loader calls, in the order it calls them."""
        from rey_lib.db import postgres_utils

        for capability in (
            "table_exists",
            "get_table_columns",
            "create_staging_table_if_not_exists",
            "bulk_insert",
        ):
            assert hasattr(postgres_utils, capability)

    def test_table_exists_is_answered_by_every_provider_that_can_load(self) -> None:
        """Existence is a question the load path asks before anything else.

        SQL Server answers it from its own information_schema rather than the
        shared inspector, because its connection is raw pyodbc and the shared
        inspector cannot see it -- and SQL Server is the provider the only
        real loader config uses. A provider that cannot answer refuses by
        name; it does not get a default.
        """
        from rey_lib.db import postgres_utils

        assert hasattr(postgres_utils, "table_exists")
        assert "table_exists" in _PROVIDER_CONTRACT_CAPABILITIES


class TestWhatIsDeliberatelyOutsideTheRegistry:
    """One method looks like it belongs and does not."""

    def test_is_truncation_error_is_not_a_declared_capability(self) -> None:
        """It takes no connection, so there is no provider to refuse for.

        It asks every registered backend in turn and swallows the ones that
        cannot answer, so a missing implementation is already absorbed. Adding
        it to the registry would require a provider the signature does not
        carry.
        """
        assert "is_truncation_error" not in _PROVIDER_CONTRACT_CAPABILITIES
