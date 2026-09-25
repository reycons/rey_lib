"""A load that ENDS at a file, and the role model that makes it one mechanism.

    QuerySource  -> IdentityTransform -> DataFile
    DataFile     -> IdentityTransform -> DataFile

THE ARCHITECTURAL PROOF. Source and target are ROLES, not classes: the same
``DataFile`` family that has always answered "what is this file" on the way in
now answers it on the way out, and the boundary asks the target which family
it belongs to rather than being told by a flag.

What that buys, and what is asserted here, is that the two ends are
INDEPENDENT. Neither knows what the other is, so the four shapes are one
mechanism:

    file  -> table      already worked
    query -> table      already worked
    query -> file       here
    file  -> file       here, and it is the one that proves independence

Real files and a real engine on both ends. A double answering ``write`` would
prove the boundary calls it, which was never in doubt; what is worth proving
is that a statement's own result reaches a file a reader can open, with its
columns in the order the insert would have used.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.db.db_adapter import DBAdapter
from rey_lib.db.query_source import QuerySource
from rey_lib.files.data_file import data_file_for
from rey_lib.load import load_operation


class _Table:
    """A write adapter, for the one case here that ends at a database."""

    def __init__(self, columns: list[str]) -> None:
        self.columns = columns

    def supports_provider_capability(self, _conn, _capability) -> bool:
        return False

    def table_exists(self, *_args, **_kwargs) -> bool:
        return True

    def get_table_columns(self, *_args, **_kwargs) -> list[str]:
        return list(self.columns)

    def bulk_insert(self, _conn, _schema, _table, rows, _columns) -> int:
        return len(rows)

    def is_truncation_error(self, _exc) -> bool:
        return False


@pytest.fixture()
def engine():
    """One in-memory DuckDB holding the source relation."""
    from rey_lib.db.duckdb_utils import open_memory_connection

    with open_memory_connection() as conn:
        conn.execute("CREATE TABLE orders (a INTEGER, b VARCHAR)")
        conn.execute("INSERT INTO orders VALUES (1, 'x'), (2, 'y'), (3, 'z')")
        yield conn


def _query(engine, statement: str = "SELECT a, b FROM orders ORDER BY a"):
    return QuerySource(engine, statement, adapter=DBAdapter())


def _load(run_log, source: Any, target: Any, **kwargs) -> int:
    """One load through the shared boundary, with NOTHING stubbed.

    No connection is patched and no write adapter is installed, which is the
    point: a file destination reaches none of that. A load that quietly opened
    a connection would fail here rather than passing on a substitute.
    """
    return load_operation._load_one_file(
        source,
        None,
        target,
        ctx=SimpleNamespace(log_depth=0),
        run_log=run_log,
        movements=None,
        load_name="a_file_destination_load",
        **kwargs,
    )


class TestAQueryReachesAFile:
    """The shape the loader screen is gaining."""

    def test_its_rows_are_written(self, engine, run_log, tmp_path) -> None:
        out = tmp_path / "rows.csv"

        written = _load(run_log, _query(engine), data_file_for(out))

        assert written == 3
        assert out.exists()

    def test_the_file_reads_back_as_the_query_produced_it(
        self, engine, run_log, tmp_path
    ) -> None:
        """READ BACK THROUGH THE SAME FAMILY, which is the whole claim.

        A write that only the writer could read would be a private format
        wearing a suffix. Reading it with the object the suffix resolves to
        proves the round trip.
        """
        out = tmp_path / "rows.csv"

        _load(run_log, _query(engine), data_file_for(out))

        assert data_file_for(out).read() == [
            {"a": "1", "b": "x"}, {"a": "2", "b": "y"}, {"a": "3", "b": "z"},
        ]

    def test_the_header_is_the_querys_own_column_order(
        self, engine, run_log, tmp_path
    ) -> None:
        """Ordered, for the same reason the insert is.

        A set comparison passes a file whose columns are shuffled, and every
        value then sits under the wrong name.
        """
        out = tmp_path / "rows.csv"

        _load(
            run_log,
            _query(engine, "SELECT b, a FROM orders ORDER BY a"),
            data_file_for(out),
        )

        assert out.read_text(encoding="utf-8").splitlines()[0] == "b,a"

    def test_no_connection_is_opened_for_the_destination(
        self, engine, run_log, tmp_path, monkeypatch
    ) -> None:
        """A file destination HAS none, and does not resolve one anyway.

        Asserted rather than assumed: reaching `shared_connection` with a file
        target would mean the boundary still believes every destination is a
        database, and the failure would only show on an installation where
        that name resolves to nothing.
        """
        monkeypatch.setattr(
            load_operation, "shared_connection",
            lambda *_a, **_k: pytest.fail(
                "a file destination resolved a connection"
            ),
        )

        assert _load(run_log, _query(engine), data_file_for(tmp_path / "o.csv")) == 3

    def test_no_loader_is_built_for_it(
        self, engine, run_log, tmp_path, monkeypatch
    ) -> None:
        """`DataLoader` is the DATABASE's destination mechanic.

        A file writes itself. Building one for a file target would be the
        "N objects, one mechanism" fault arriving from the other direction --
        a mechanic constructed and then not used.
        """
        monkeypatch.setattr(
            load_operation, "_build_data_loader",
            lambda *_a, **_k: pytest.fail("a file destination built a loader"),
        )

        assert _load(run_log, _query(engine), data_file_for(tmp_path / "o.csv")) == 3


class TestEachFormatReachesItsOwnWriter:
    """The format resolves from the PATH, and nothing here lists a suffix."""

    def test_json_is_written_as_a_document(self, engine, run_log, tmp_path) -> None:
        out = tmp_path / "rows.json"

        _load(run_log, _query(engine), data_file_for(out))

        # A BARE ARRAY, which is one of the two shapes the reader accepts. The
        # other names a table, and nothing here knows what to call one.
        assert json.loads(out.read_text(encoding="utf-8")) == [
            {"a": 1, "b": "x"}, {"a": 2, "b": "y"}, {"a": 3, "b": "z"},
        ]

    def test_jsonl_is_written_a_record_per_line(
        self, engine, run_log, tmp_path
    ) -> None:
        out = tmp_path / "rows.jsonl"

        _load(run_log, _query(engine), data_file_for(out))

        lines = out.read_text(encoding="utf-8").strip().splitlines()
        assert [json.loads(one) for one in lines] == [
            {"a": 1, "b": "x"}, {"a": 2, "b": "y"}, {"a": 3, "b": "z"},
        ]

    def test_json_keeps_the_types_a_csv_cannot(
        self, engine, run_log, tmp_path
    ) -> None:
        """Not a detail of this change, and worth pinning anyway.

        The integers arrive as integers because JSON carries them; the CSV
        round trip above reads them back as text because the format does not.
        Both are correct, and a reader choosing a suffix is choosing that.
        """
        out = tmp_path / "rows.json"

        _load(run_log, _query(engine), data_file_for(out))

        assert json.loads(out.read_text(encoding="utf-8"))[0]["a"] == 1

    def test_column_order_survives_where_the_format_carries_one(
        self, engine, run_log, tmp_path
    ) -> None:
        """STATED, because the three formats do not answer this alike.

        A delimited file DECLARES its structure -- a header, in order -- and
        JSONL renders each record in the order it was built, so both keep the
        query's column order. A JSON document is written through the pretty
        renderer, which sorts keys, so it does not.

        That is not a defect to correct here. A keyed format carries its names
        on every record and `declares_structure` is False for exactly that
        reason: order is not part of what it means, and the reader matches by
        key. Pinned so the difference is a known one.
        """
        statement = "SELECT b, a FROM orders ORDER BY a"

        _load(run_log, _query(engine, statement), data_file_for(tmp_path / "r.csv"))
        _load(run_log, _query(engine, statement), data_file_for(tmp_path / "r.jsonl"))
        _load(run_log, _query(engine, statement), data_file_for(tmp_path / "r.json"))

        header = (tmp_path / "r.csv").read_text(encoding="utf-8").splitlines()[0]
        first_line = (tmp_path / "r.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()[0]
        document = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))

        assert header == "b,a"
        assert list(json.loads(first_line)) == ["b", "a"]
        assert list(document[0]) == ["a", "b"]

    def test_a_suffix_naming_no_format_is_refused_by_name(self, tmp_path) -> None:
        """The EXISTING resolver, doing what it already does.

        No destination-format field exists, because this is the answer: a path
        that does not say what it is is refused before a load starts, rather
        than guessed at and written wrongly.
        """
        with pytest.raises(ValueError) as raised:
            data_file_for(tmp_path / "rows.unknown")

        assert "rows.unknown" in str(raised.value)

    def test_a_format_with_no_writer_refuses_saying_which(self, tmp_path) -> None:
        """Read-only is a real answer, and it names itself.

        A headerless delimited file states its width nowhere, so there is no
        writer for it. The refusal says so rather than producing a file whose
        columns nothing can recover.
        """
        source = data_file_for(tmp_path / "rows.txt", file_type="DELIMITED_NO_HEADER")

        with pytest.raises(ValueError) as raised:
            source.write([{"a": 1}])

        assert "DELIMITED_NO_HEADER" in str(raised.value)


class TestTheTwoEndsAreIndependent:
    """file -> file, which neither end can special-case its way to."""

    def test_a_file_loads_into_another_file(self, run_log, tmp_path) -> None:
        """THE PROOF THE ROLES ARE INDEPENDENT.

        Nothing in this load is a database. If either end were still deciding
        by "what the other one is", there is no arrangement of a connection, a
        loader or an adapter that would get this through -- it would have to
        be written as a fourth path.
        """
        given = tmp_path / "in.csv"
        given.write_text("a,b\n1,x\n2,y\n", encoding="utf-8")
        out = tmp_path / "out.jsonl"

        written = _load(run_log, data_file_for(given), data_file_for(out))

        assert written == 2
        assert data_file_for(out).read() == [
            {"a": "1", "b": "x"}, {"a": "2", "b": "y"},
        ]

    def test_it_crosses_formats(self, run_log, tmp_path) -> None:
        """CSV in, JSON out, and neither object was told about the other."""
        given = tmp_path / "in.csv"
        given.write_text("a,b\n1,x\n", encoding="utf-8")
        out = tmp_path / "out.json"

        _load(run_log, data_file_for(given), data_file_for(out))

        assert json.loads(out.read_text(encoding="utf-8")) == [{"a": "1", "b": "x"}]

    def test_an_empty_source_is_refused_rather_than_written(
        self, run_log, tmp_path
    ) -> None:
        """The SHARED refusal, reached with a file at both ends.

        An empty result writing a header-only file would look like a
        successful export of nothing. The boundary already refuses it, and
        this asserts a file target does not route around that.
        """
        given = tmp_path / "in.csv"
        given.write_text("a,b\n", encoding="utf-8")
        out = tmp_path / "out.csv"

        assert _load(run_log, data_file_for(given), data_file_for(out)) == 0
        assert not out.exists()


class TestTheWayIn:
    """`load_query_to_file`, beside `load_query_to_table`."""

    def test_it_builds_both_ends_and_returns_what_it_wrote(
        self, engine, run_log, tmp_path, monkeypatch
    ) -> None:
        out = tmp_path / "rows.csv"
        monkeypatch.setattr(
            load_operation, "shared_connection",
            lambda _ctx, _name: SimpleNamespace(handle=lambda: engine),
        )

        written = load_operation.load_query_to_file(
            SimpleNamespace(log_depth=0), run_log,
            "SELECT a, b FROM orders ORDER BY a", "warehouse", str(out),
        )

        assert written == 3
        assert data_file_for(out).read()[0] == {"a": "1", "b": "x"}

    def test_it_accepts_no_declared_format(self) -> None:
        """ONE WAY TO SAY IT, and it is the path.

        `file_type` describes a data file being READ -- the loader refuses it
        beside a statement for exactly that reason. Accepting one here would
        give the same name a second meaning, which is how a parameter comes to
        mean "roughly format".
        """
        from inspect import signature

        assert "file_type" not in signature(
            load_operation.load_query_to_file
        ).parameters

    def test_an_out_file_whose_suffix_names_nothing_is_refused(
        self, engine, run_log, tmp_path, monkeypatch
    ) -> None:
        """Refused BY NAME, before anything is read or written."""
        monkeypatch.setattr(
            load_operation, "shared_connection",
            lambda _ctx, _name: SimpleNamespace(handle=lambda: engine),
        )

        with pytest.raises(ValueError) as raised:
            load_operation.load_query_to_file(
                SimpleNamespace(log_depth=0), run_log,
                "SELECT a FROM orders", "warehouse",
                str(tmp_path / "rows.out"),
            )

        assert "rows.out" in str(raised.value)

    def test_it_takes_no_destination_connection(self) -> None:
        """A file destination has none, so none is accepted.

        Taking one and ignoring it would leave a reader believing the file was
        written somewhere it was not.
        """
        from inspect import signature

        taken = set(signature(load_operation.load_query_to_file).parameters)
        assert "connection" not in taken
        assert "source_connection" in taken


class TestWhatTheEvidenceSays:
    """A file destination is recorded as one."""

    def test_it_names_the_path_rather_than_a_table(
        self, engine, run_log, tmp_path, monkeypatch
    ) -> None:
        """`schema` and `table` mean something, and a file is neither.

        Recording a path under `table` would put a value in a column other
        readers join on, which is worse than recording nothing. So the
        destination is carried in the TARGET'S OWN vocabulary.
        """
        recorded: list[dict[str, Any]] = []
        monkeypatch.setattr(
            load_operation, "log_row_count",
            lambda _run_log, **kwargs: recorded.append(kwargs),
        )
        out = tmp_path / "rows.csv"

        _load(run_log, _query(engine), data_file_for(out))

        assert recorded, "the load recorded no row count"
        assert recorded[-1]["count_name"] == "loaded_rows"
        assert recorded[-1]["count"] == 3
        assert recorded[-1]["destination_path"] == str(out)
        assert "schema" not in recorded[-1]
        assert "table" not in recorded[-1]

    def test_a_table_destination_still_names_its_schema_and_table(
        self, run_log, tmp_path, monkeypatch
    ) -> None:
        """UNCHANGED, and this is what says so.

        The evidence fields moved from two positional strings to the target's
        own mapping. A database load must come out of that saying exactly what
        it always said.
        """
        from rey_lib.db.database_objects import DatabaseObjectIdentity

        recorded: list[dict[str, Any]] = []
        monkeypatch.setattr(
            load_operation, "log_row_count",
            lambda _run_log, **kwargs: recorded.append(kwargs),
        )
        monkeypatch.setattr(load_operation, "_write_adapter", _Table(["a", "b"]))
        monkeypatch.setattr(
            load_operation, "shared_connection",
            lambda _ctx, _name: SimpleNamespace(
                handle=lambda: SimpleNamespace(
                    commit=lambda: None, rollback=lambda: None,
                )
            ),
        )
        given = tmp_path / "in.csv"
        given.write_text("a,b\n1,x\n", encoding="utf-8")

        _load(
            run_log, data_file_for(given),
            DatabaseObjectIdentity(
                connection="warehouse", catalog="", schema="landing",
                name="records",
            ),
        )

        assert recorded[-1]["schema"] == "landing"
        assert recorded[-1]["table"] == "records"
        assert "destination_path" not in recorded[-1]
