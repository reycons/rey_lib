"""Where logical records go, and the policies that govern getting them there.

No database is opened. What is asserted is what the object DECIDED -- which
table operations reached the primitive beneath DatabaseTable, in what order,
and when it refused -- because every rule here is a policy rather than a
mechanism. DataLoader holds no adapter (backlog 751), so the double stands
where DatabaseTable resolves its primitive.

The three that matter most are the ones that only surface in production:

    a missing destination is a RUN-level fault, not a per-file one
    each load commits ALONE, so a batch may partially succeed
    a truncation retries exactly once, through injected widening
"""

from __future__ import annotations

from typing import Any

import inspect

import pytest

from rey_lib.errors.error_utils import ConfigError, DatabaseError
from rey_lib.db import database_objects
from rey_lib.db.database_objects import DatabaseObjectIdentity
from rey_lib.db.data_loader import DataLoader
from rey_lib.data.logical_type import LogicalType
from tests.support.connector_double import ConnectorPrimitives

_DEFS = [("a", LogicalType("integer")), ("b", LogicalType("text", length=20))]
#: How DatabaseTable writes ``_DEFS`` for PostgreSQL, the double's provider.
_CREATED = [("a", "INTEGER"), ("b", "VARCHAR(20)")]
_RECORDS = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
#: The staging table's name beside ``t`` (backlog 516).
_STAGE = "__rey_stage"
#: A value too long for ``b``: what a truncation is about.
_OVERFLOWING = [{"a": 1, "b": "x" * 25}, {"a": 2, "b": "y"}]


class _Conn:
    """A connection that records the transaction boundaries it was given."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class _Adapter(ConnectorPrimitives):
    """An adapter that records calls instead of reaching a database."""

    def __init__(self, *, exists: bool, insert_error: Exception | None = None,
                 truncation: bool = False) -> None:
        self._exists = exists
        self._insert_error = insert_error
        self._truncation = truncation
        self.calls: list[str] = []
        self.created: list[tuple] = []
        self.emptied: list[tuple] = []
        self.dropped: list[tuple] = []
        self.inserted: list[tuple] = []
        #: Staging tables beside the destination, kept apart from it (backlog 516).
        self.staged: set[str] = set()
        #: Each promotion, as (statement kind, target, staging).
        self.promoted: list[tuple] = []

    def supports_provider_capability(self, _conn, _capability) -> bool:
        """No native execution path, so the materialised one runs.

        Answered rather than absent: the loader asks every adapter
        this, and a double that omitted it would fail on the question
        rather than on what the test is about.
        """
        return False

    def has_table(self, _conn, _schema, table) -> bool:
        self.calls.append("table_exists")
        return table in self.staged if table.endswith(_STAGE) else self._exists

    def execute_statement(self, conn, sql_text: str) -> int:
        """DROP and DELETE arrive as DatabaseTable writes them (backlog 722);
        a promotion as replace_rows / replace_table write it (backlog 516)."""
        for kind, opening in (("replace_rows", "WITH removed AS (DELETE FROM "),
                              ("replace_table", "DO $rey$ BEGIN DROP TABLE ")):
            if sql_text.startswith(opening):
                self.calls.append(kind)
                self.promoted.append((kind, "t", "t" + _STAGE))
                if kind == "replace_table":
                    self.staged.discard("t" + _STAGE)
                    self._exists = True
                return 0
        for verb, call, kept in (("DROP TABLE ", "drop_table", self.dropped),
                                 ("DELETE FROM ", "delete_all_rows", self.emptied)):
            if sql_text.startswith(verb):
                schema, table = (part.strip('"') for part in
                                 sql_text[len(verb):].split('"."'))
                self.calls.append(call)
                kept.append((schema, table))
                if call == "drop_table":
                    if table.endswith(_STAGE):
                        self.staged.discard(table)
                    else:
                        self._exists = False
                return 0
        return super().execute_statement(conn, sql_text)

    def get_table_columns(self, _conn, _schema, _table) -> list[str]:
        self.calls.append("get_table_columns")
        return ["a", "b"]

    def record_create(self, _conn, schema, table, defs):
        self.calls.append("create")
        self.created.append((schema, table, defs))
        if table.endswith(_STAGE):
            self.staged.add(table)
        else:
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


#: The primitive DatabaseTable resolves while a test runs.
_PRIMITIVE: dict[str, Any] = {}


@pytest.fixture(autouse=True)
def _primitive_beneath_the_table(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Stand the test's double where DatabaseTable resolves its primitive."""
    monkeypatch.setattr(database_objects, "_adapter", lambda: _PRIMITIVE["adapter"])
    yield
    _PRIMITIVE.clear()


def _loader(adapter, **kwargs) -> DataLoader:
    """A DataLoader whose table operations reach ``adapter`` beneath DatabaseTable."""
    _PRIMITIVE["adapter"] = adapter
    return DataLoader(**kwargs)


class TestTheDependenciesAreExplicit:
    """Nothing is reached for; every database operation is DatabaseTable's."""

    def test_it_holds_no_adapter(self) -> None:
        """Higher layers use the canonical objects, not the primitive (backlog 751).

        No parameter, no attribute, and no primitive call of its own: every
        database operation it makes is a DatabaseTable behaviour.
        """
        from pathlib import Path

        import rey_lib.db.data_loader as module

        assert "adapter" not in inspect.signature(DataLoader.__init__).parameters
        assert not hasattr(DataLoader(), "adapter")
        assert "adapter" not in Path(module.__file__).read_text(encoding="utf-8")

    def test_its_writes_reach_the_primitive_through_the_table(self) -> None:
        adapter = _Adapter(exists=True)

        loaded = _loader(adapter).load(_Conn(), _TARGET, _RECORDS, _DEFS)

        assert loaded == 2
        assert adapter.inserted == [("s", "t", _RECORDS, ["a", "b"])]

    def test_the_module_knows_nothing_it_should_not(self) -> None:
        """No ctx, no loader, no config, no adapter of its own.

        Asserted structurally because each is easy to reintroduce with one
        convenient import, and the whole point of the extraction is that the
        application context stops above this object.
        """
        from pathlib import Path

        import rey_lib.db.data_loader as module

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

        import rey_lib.db.data_loader as module

        assert "_parse_destination" not in Path(module.__file__).read_text(
            encoding="utf-8"
        )

    def test_it_has_no_format_knowledge(self) -> None:
        """A destination does not care what kind of file produced the rows."""
        from pathlib import Path

        import rey_lib.db.data_loader as module

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
        # DatabaseTable.create asks once more before its DDL: it owns what "exists" means.
        assert adapter.calls == ["table_exists", "table_exists", "create", "bulk_insert"]
        assert adapter.created[0][2] == _CREATED


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
    """The least-covered path in the loader, and the easiest to lose.

    Widening is the loader's own, through DatabaseTable (backlog 690): an
    overflowing bounded text column is made wide enough, and the insert is
    retried once.
    """

    def test_a_truncation_widens_the_overflowing_column_and_retries_once(self) -> None:
        adapter = _Adapter(exists=True, truncation=True,
                           insert_error=DatabaseError("too long"))

        loaded = _loader(adapter).load(_Conn(), _TARGET, _OVERFLOWING, _DEFS)

        assert loaded == 2
        assert adapter.calls.count("bulk_insert") == 2
        # Only b overflowed: the longest value plus the margin.
        assert adapter.altered == ['ALTER TABLE "s"."t" ALTER COLUMN "b" TYPE VARCHAR(35)']

    def test_a_failed_rollback_leaves_the_inserts_own_error_standing(self) -> None:
        """The handle refusing to roll back (DuckDB with no transaction open)
        must not replace what actually went wrong (backlog 746)."""
        refused = RuntimeError("cannot rollback - no transaction is active")
        conn = _Conn()
        conn.rollback = lambda: (_ for _ in ()).throw(refused)
        original = DatabaseError("the insert failed")

        with pytest.raises(DatabaseError) as raised:
            _loader(_Adapter(exists=True, insert_error=original)).load(
                conn, _TARGET, _RECORDS, _DEFS,
            )

        assert raised.value is original

    def test_after_a_failed_rollback_nothing_is_widened_or_retried(self) -> None:
        """Even a truncation: a retry is only safe once the rollback succeeded."""
        adapter = _Adapter(exists=True, truncation=True,
                           insert_error=DatabaseError("too long"))
        conn = _Conn()
        conn.rollback = lambda: (_ for _ in ()).throw(RuntimeError("no transaction"))

        with pytest.raises(DatabaseError, match="too long"):
            _loader(adapter).load(conn, _TARGET, _OVERFLOWING, _DEFS)

        assert adapter.calls.count("bulk_insert") == 1
        assert getattr(adapter, "altered", []) == []

    def test_a_NON_truncation_error_widens_nothing(self) -> None:
        """Widening a column would not repair an unrelated failure."""
        adapter = _Adapter(exists=True, truncation=False,
                           insert_error=DatabaseError("permission denied"))

        with pytest.raises(DatabaseError):
            _loader(adapter).load(_Conn(), _TARGET, _OVERFLOWING, _DEFS)

        assert getattr(adapter, "altered", []) == []

    def test_a_truncation_nothing_overflowed_is_reported(self) -> None:
        """Retrying an identical insert would fail identically."""
        adapter = _Adapter(exists=True, truncation=True,
                           insert_error=DatabaseError("too long"))

        with pytest.raises(DatabaseError):
            _loader(adapter).load(_Conn(), _TARGET, _RECORDS, _DEFS)

        assert adapter.calls.count("bulk_insert") == 1
        assert getattr(adapter, "altered", []) == []

    def test_a_table_the_rollback_took_is_created_again_wider(self) -> None:
        """Transactional DDL: the table this load created went with the rollback."""
        adapter = _Adapter(exists=False, truncation=True,
                           insert_error=DatabaseError("too long"))
        conn = _Conn()
        conn.rollback = lambda: setattr(adapter, "_exists", False)

        loaded = _loader(adapter, create_destination=True).load(
            conn, _TARGET, _OVERFLOWING, _DEFS
        )

        assert loaded == 2
        assert [defs for _s, _t, defs in adapter.created] == [
            _CREATED, [("a", "INTEGER"), ("b", "VARCHAR(35)")],
        ]


class TestTheReplacePolicy:
    """Whether an EXISTING destination's contents are removed first.

    Its sibling `create_destination` answers what happens when the table is
    ABSENT. These two never apply to the same destination: one acts because
    there is nothing there, the other because there is.
    """

    def test_absence_adds_to_what_is_there(self) -> None:
        """WHAT A LOAD HAS ALWAYS DONE, and what an undeclared load means.

        The case that matters most: every load in the estate declares nothing,
        so every one of them must still take this path.
        """
        adapter = _Adapter(exists=True)

        _loader(adapter).load(_Conn(), _TARGET, _RECORDS, _DEFS)

        assert adapter.emptied == []
        assert adapter.calls == ["table_exists", "get_table_columns",
                                 "bulk_insert"]

    def test_declared_it_stages_then_the_table_replaces_its_rows(self) -> None:
        """THE DESTINATION IS NOT TOUCHED UNTIL THE NEW ROWS ARE SAFE (backlog
        516): they go to a staging table first, and the table then replaces
        its rows from it in one atomic promotion. No separate emptying.
        """
        adapter = _Adapter(exists=True)

        loaded = _loader(adapter, replace_destination=True).load(
            _Conn(), _TARGET, _RECORDS, _DEFS
        )

        assert loaded == 2
        assert adapter.emptied == []
        assert [(s, t) for s, t, _r, _c in adapter.inserted] == [("s", "t" + _STAGE)]
        assert adapter.promoted == [("replace_rows", "t", "t" + _STAGE)]
        assert adapter.dropped == [("s", "t" + _STAGE)]   # staging removed after
        assert adapter.calls.index("bulk_insert") < adapter.calls.index("replace_rows")

    def test_a_failed_fill_never_reaches_the_destination(self) -> None:
        adapter = _Adapter(exists=True, insert_error=DatabaseError("nope"))

        with pytest.raises(DatabaseError):
            _loader(adapter, replace_destination=True).load(
                _Conn(), _TARGET, _RECORDS, _DEFS
            )

        assert adapter.promoted == [] and adapter.emptied == []
        assert adapter.dropped == [("s", "t" + _STAGE)]   # staging removed on failure

    def test_a_destination_it_just_created_is_not_emptied(self) -> None:
        """Nothing to remove, and emptying would be a second answer to a
        question `create` already settled.
        """
        adapter = _Adapter(exists=False)

        _loader(
            adapter, create_destination=True, replace_destination=True,
        ).load(_Conn(), _TARGET, _RECORDS, _DEFS)

        assert adapter.emptied == []
        # DatabaseTable.create asks once more before its DDL: it owns what "exists" means.
        assert adapter.calls == ["table_exists", "table_exists", "create", "bulk_insert"]

    def test_it_is_the_table_that_is_asked(self) -> None:
        """The loader names no provider and spells no SQL. It asks the table,
        which owns emptying itself (backlog 722).
        """
        source = inspect.getsource(DataLoader)

        assert "DatabaseTable.replace_rows" in source
        assert "self.adapter.delete_all_rows" not in source
        for named in ("postgres", "mysql", "sqlserver", "duckdb",
                      "DELETE", "TRUNCATE"):
            assert named not in source, named


class TestRecreateDestroysTheDestinationAndBuildsItAgain:
    """DROP, then CREATE, then load -- and that order is what Recreate means.

    The one policy here that is destructive at SCHEMA level. Replace keeps the
    table it empties; this does not keep it at all, and nothing rebuilds what
    the table owned.
    """

    def test_declared_it_builds_the_table_beside_and_swaps_it_in(self) -> None:
        """The new table is built from these records as staging, filled, and
        only then put in the old one's place, atomically (backlog 516)."""
        adapter = _Adapter(exists=True)

        loaded = _loader(adapter, recreate_destination=True).load(
            _Conn(), _TARGET, _RECORDS, _DEFS
        )

        assert loaded == 2
        assert adapter.created == [("s", "t" + _STAGE, _CREATED)]
        assert [(s, t) for s, t, _r, _c in adapter.inserted] == [("s", "t" + _STAGE)]
        assert adapter.promoted == [("replace_table", "t", "t" + _STAGE)]
        assert adapter.dropped == []   # no separate drop: the swap is the promotion
        assert adapter.staged == set()

    def test_a_failed_fill_leaves_the_old_table_standing(self) -> None:
        adapter = _Adapter(exists=True, insert_error=DatabaseError("nope"))

        with pytest.raises(DatabaseError):
            _loader(adapter, recreate_destination=True).load(
                _Conn(), _TARGET, _RECORDS, _DEFS
            )

        assert adapter.promoted == []
        assert adapter.dropped == [("s", "t" + _STAGE)]
        assert adapter._exists is True

    def test_it_never_merely_empties(self) -> None:
        """THE FAILURE THAT WOULD LOOK LIKE SUCCESS. Emptying instead of
        dropping leaves standing the very schema the caller asked to rebuild,
        and the rows would land correctly either way.
        """
        adapter = _Adapter(exists=True)

        _loader(adapter, recreate_destination=True).load(
            _Conn(), _TARGET, _RECORDS, _DEFS
        )

        assert adapter.emptied == []

    def test_an_absent_destination_is_simply_created(self) -> None:
        """Nothing to drop. Refusing would be a second answer to a question the
        mode already settles: recreate asks that the table be exactly what
        these records need, and an absent one satisfies that by being created.
        """
        adapter = _Adapter(exists=False)

        _loader(adapter, recreate_destination=True).load(
            _Conn(), _TARGET, _RECORDS, _DEFS
        )

        assert adapter.dropped == []
        # DatabaseTable.create asks once more before its DDL: it owns what "exists" means.
        assert adapter.calls == ["table_exists", "table_exists", "create", "bulk_insert"]

    def test_it_alone_authorizes_creating_what_is_absent(self) -> None:
        """Recreate implies the table may be built, so a load declaring it and
        not `create` is not refused for a destination that is not there.
        """
        adapter = _Adapter(exists=False)

        _loader(adapter, recreate_destination=True).load(
            _Conn(), _TARGET, _RECORDS, _DEFS
        )

        assert adapter.created == [("s", "t", _CREATED)]

    def test_it_is_the_table_that_is_asked(self) -> None:
        source = inspect.getsource(DataLoader)

        assert "DatabaseTable.replace_table" in source
        assert "self.adapter.drop_table" not in source
        # `DROP TABLE` is the statement; "a drop" is English, and the comments
        # here explain the operation in words. It is SQL that must not be
        # written, not the noun.
        for named in ("postgres", "mysql", "sqlserver", "duckdb",
                      "DROP TABLE", "DROP  TABLE"):
            assert named not in source, named


class TestTheRetryHappensAgainstStaging:
    """Backlog 516's second bug: a retry that re-inserted after a rollback had
    restored the old rows appended to them. The retry now never reaches the
    destination -- it widens and refills the staging table, and the table is
    promoted once.
    """

    @pytest.mark.parametrize(("mode", "promotion"), [
        ("replace_destination", "replace_rows"),
        ("recreate_destination", "replace_table"),
    ])
    def test_the_destination_is_promoted_once_after_the_retry(
        self, mode: str, promotion: str,
    ) -> None:
        adapter = _Adapter(exists=True, truncation=True,
                           insert_error=DatabaseError("too long"))

        loaded = _loader(adapter, **{mode: True}).load(
            _Conn(), _TARGET, _OVERFLOWING, _DEFS
        )

        assert loaded == 2
        assert {(s, t) for s, t, _r, _c in adapter.inserted} == {("s", "t" + _STAGE)}
        assert adapter.calls.count("bulk_insert") == 2
        assert adapter.promoted == [(promotion, "t", "t" + _STAGE)]
        assert adapter.emptied == []
        # The staging table is what was widened, never the destination.
        assert all('"t__rey_stage"' in one for one in getattr(adapter, "altered", []))
