"""Where logical records go, and the policies that govern getting them there.

No database is opened. What is asserted is what the object DECIDED -- which
adapter calls it made, in what order, and when it refused -- because every
rule here is a policy rather than a mechanism.

The three that matter most are the ones that only surface in production:

    a missing destination is a RUN-level fault, not a per-file one
    each load commits ALONE, so a batch may partially succeed
    a truncation retries exactly once, through injected widening
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.errors.error_utils import ConfigError, DatabaseError
from rey_lib.db.database_objects import DatabaseObjectIdentity
from rey_lib.files.data_loader import DataLoader

_DEFS = [("a", "INTEGER"), ("b", "VARCHAR(20)")]
_RECORDS = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]


class _Conn:
    """A connection that records the transaction boundaries it was given."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class _Adapter:
    """An adapter that records calls instead of reaching a database."""

    def __init__(self, *, exists: bool, insert_error: Exception | None = None,
                 truncation: bool = False) -> None:
        self._exists = exists
        self._insert_error = insert_error
        self._truncation = truncation
        self.calls: list[str] = []
        self.created: list[tuple] = []
        self.inserted: list[tuple] = []

    def supports_provider_capability(self, _conn, _capability) -> bool:
        """No native execution path, so the materialised one runs.

        Answered rather than absent: the loader asks every adapter
        this, and a double that omitted it would fail on the question
        rather than on what the test is about.
        """
        return False

    def table_exists(self, _conn, _schema, _table) -> bool:
        self.calls.append("table_exists")
        return self._exists

    def get_table_columns(self, _conn, _schema, _table) -> list[str]:
        self.calls.append("get_table_columns")
        return ["a", "b"]

    def create_staging_table_if_not_exists(self, _conn, schema, table, defs):
        self.calls.append("create")
        self.created.append((schema, table, defs))
        self._exists = True
        return True

    def bulk_insert(self, _conn, schema, table, records, columns) -> int:
        self.calls.append("bulk_insert")
        self.inserted.append((schema, table, records, columns))
        if self._insert_error is not None:
            error, self._insert_error = self._insert_error, None
            raise error
        return len(records)

    def is_truncation_error(self, _exc) -> bool:
        return self._truncation


#: The destination these tests load into. An identity, because that is what
#: the loader is handed now -- it holds no schema or table of its own.
_TARGET = DatabaseObjectIdentity(
    connection="c", catalog="", schema="s", name="t",
)


def _loader(adapter, **kwargs) -> DataLoader:
    return DataLoader(adapter=adapter, **kwargs)


class TestTheDependenciesAreExplicit:
    """Nothing is reached for; everything is handed over."""

    def test_a_stub_adapter_needs_no_patching_at_all(self) -> None:
        """The sharpest proof the dependency is injected.

        A test that had to monkeypatch a module attribute to substitute the
        adapter would be evidence the object reaches for one instead of being
        given one -- which is what extracting it from the procedural loader
        was for.
        """
        adapter = _Adapter(exists=True)

        loaded = _loader(adapter).load(_Conn(), _TARGET, _RECORDS, _DEFS)

        assert loaded == 2

    def test_the_module_knows_nothing_it_should_not(self) -> None:
        """No ctx, no loader, no config, no adapter of its own.

        Asserted structurally because each is easy to reintroduce with one
        convenient import, and the whole point of the extraction is that the
        application context stops above this object.
        """
        from pathlib import Path

        import rey_lib.files.data_loader as module

        source = Path(module.__file__).read_text(encoding="utf-8")

        assert "file_loader" not in source
        assert "rey_lib.config" not in source
        assert "DBAdapter()" not in source
        # ctx appears only in prose explaining why it is absent.
        assert "ctx." not in source

    def test_it_interprets_no_configuration(self) -> None:
        """schema and table arrive RESOLVED.

        Parsing "database.schema.table" is configuration interpretation and
        belongs at the construction boundary. Doing it here would also mean
        reaching into file_loader for the parser.
        """
        from pathlib import Path

        import rey_lib.files.data_loader as module

        assert "_parse_destination" not in Path(module.__file__).read_text(
            encoding="utf-8"
        )

    def test_it_has_no_format_knowledge(self) -> None:
        """A destination does not care what kind of file produced the rows."""
        from pathlib import Path

        import rey_lib.files.data_loader as module

        source = Path(module.__file__).read_text(encoding="utf-8")

        assert "file_type" not in source
        assert "JSONL" not in source


class TestTheCreatePolicy:
    """Whether an absent destination may be made from the records."""

    def test_an_existing_destination_is_never_recreated(self) -> None:
        """The policy, not merely its usual outcome.

        IF NOT EXISTS is no defence: a table dropped between the check and
        the call would be recreated against a load that said not to.
        """
        adapter = _Adapter(exists=True)

        _loader(adapter).load(_Conn(), _TARGET, _RECORDS, _DEFS)

        assert adapter.created == []
        assert adapter.calls == ["table_exists", "get_table_columns",
                                 "bulk_insert"]

    def test_an_absent_destination_not_declared_raises(self) -> None:
        """A RUN-level fault: every file would fail identically.

        So it is a ConfigError that stops the run, not a per-file failure
        that fills a rejected folder with good files.
        """
        adapter = _Adapter(exists=False)

        with pytest.raises(ConfigError) as raised:
            _loader(adapter).load(_Conn(), _TARGET, _RECORDS, _DEFS)

        assert "s.t" in str(raised.value)
        assert adapter.created == []
        assert adapter.inserted == []

    def test_an_absent_destination_declared_is_created_then_loaded(self) -> None:
        """In that order, and from the records' own column defs."""
        adapter = _Adapter(exists=False)

        loaded = _loader(adapter, create_destination=True).load(
            _Conn(), _TARGET, _RECORDS, _DEFS
        )

        assert loaded == 2
        assert adapter.calls == ["table_exists", "create", "bulk_insert"]
        assert adapter.created[0][2] == _DEFS


class TestTheInsert:
    """What reaches bulk_insert, and what commits."""

    def test_the_insert_columns_are_DERIVED_from_the_column_defs(self) -> None:
        """So a CREATE and its INSERT cannot be handed different lists.

        They agreed by construction in the procedural loader because one
        caller computed both; deriving removes the chance of them drifting
        when two callers exist.
        """
        adapter = _Adapter(exists=True)

        _loader(adapter).load(_Conn(), _TARGET, _RECORDS, _DEFS)

        _schema, _table, _records, columns = adapter.inserted[0]
        assert columns == ["a", "b"]

    def test_each_load_commits_ALONE(self) -> None:
        """No transaction spans two loads.

        A batch is an aggregate of separate loads: 96 succeeded, 3 failed is
        a valid outcome, and the 96 must stay committed.
        """
        adapter = _Adapter(exists=True)
        conn = _Conn()
        loader = _loader(adapter)

        loader.load(conn, _TARGET, _RECORDS, _DEFS)
        loader.load(conn, _TARGET, _RECORDS, _DEFS)

        assert conn.commits == 2
        assert conn.rollbacks == 0

    def test_an_earlier_success_survives_a_later_failure(self) -> None:
        """The property the per-load commit exists to give."""
        conn = _Conn()
        _loader(_Adapter(exists=True)).load(conn, _TARGET, _RECORDS, _DEFS)

        failing = _Adapter(exists=True, insert_error=DatabaseError("nope"))
        with pytest.raises(DatabaseError):
            _loader(failing).load(conn, _TARGET, _RECORDS, _DEFS)

        assert conn.commits == 1        # the first load stayed committed
        assert conn.rollbacks == 1


class TestTheTruncationRetry:
    """The least-covered path in the loader, and the easiest to lose."""

    def _widening(self, calls: list) -> Any:
        def _widen(conn, target, records, column_defs) -> bool:
            calls.append((conn, records, column_defs))
            return True
        return _widen

    def test_a_truncation_widens_and_retries_exactly_once(self) -> None:
        calls: list = []
        adapter = _Adapter(exists=True, truncation=True,
                           insert_error=DatabaseError("too long"))

        loaded = _loader(adapter, widen_columns=self._widening(calls)).load(
            _Conn(), _TARGET, _RECORDS, _DEFS
        )

        assert loaded == 2
        assert len(calls) == 1
        assert adapter.calls.count("bulk_insert") == 2

    def test_the_callback_gets_the_connection_load_was_given(self) -> None:
        """Identity, not just that it fired.

        The contract that stops a future implementation quietly resolving its
        own connection -- which is what the current one does, and why the
        argument exists at all.
        """
        calls: list = []
        conn = _Conn()
        adapter = _Adapter(exists=True, truncation=True,
                           insert_error=DatabaseError("too long"))

        _loader(adapter, widen_columns=self._widening(calls)).load(
            conn, _TARGET, _RECORDS, _DEFS
        )

        assert calls[0][0] is conn

    def test_a_NON_truncation_error_never_reaches_the_callback(self) -> None:
        """Widening a column would not repair an unrelated failure."""
        calls: list = []
        adapter = _Adapter(exists=True, truncation=False,
                           insert_error=DatabaseError("permission denied"))

        with pytest.raises(DatabaseError):
            _loader(adapter, widen_columns=self._widening(calls)).load(
                _Conn(), _TARGET, _RECORDS, _DEFS
            )

        assert calls == []

    def test_no_callback_means_a_truncation_is_simply_reported(self) -> None:
        """The direct path configures no widening, and must still be usable."""
        adapter = _Adapter(exists=True, truncation=True,
                           insert_error=DatabaseError("too long"))

        with pytest.raises(DatabaseError):
            _loader(adapter).load(_Conn(), _TARGET, _RECORDS, _DEFS)

        assert adapter.calls.count("bulk_insert") == 1

    def test_widening_that_changed_nothing_re_raises(self) -> None:
        """Retrying an identical insert would fail identically."""
        adapter = _Adapter(exists=True, truncation=True,
                           insert_error=DatabaseError("too long"))

        with pytest.raises(DatabaseError):
            _loader(adapter,
                    widen_columns=lambda *_a: False).load(
                _Conn(), _TARGET, _RECORDS, _DEFS
            )

        assert adapter.calls.count("bulk_insert") == 1

    def test_a_load_with_no_widening_touches_no_application_context(
        self
    ) -> None:
        """The widening callback is the ONLY indirect ctx dependency.

        Asserted behaviourally rather than by reading the module, because a
        future path reaching ctx some other way would look like an ordinary
        call and pass the structural check.
        """
        adapter = _Adapter(exists=False)

        loaded = _loader(adapter, create_destination=True).load(
            _Conn(), _TARGET, _RECORDS, _DEFS
        )

        assert loaded == 2
