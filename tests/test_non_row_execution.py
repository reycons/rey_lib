"""Letting the engine do the work, without losing the checks that guard it.

A transform can offer an equivalent non-row form, and until now nothing asked.
When the provider can read the source itself and the destination already
exists, the rows are inserted by the engine over the file rather than carried
through this process as ``list[dict]``.

THE POINT IS NOT SPEED. It is that the two things the materialised path does
BESIDES moving rows must still happen:

    source.validate(expected_columns)      does the file match the table?
    the configured-columns rule            does it match the configuration?

The second is the one worth being nervous about. It caught two feeds that had
silently drifted, and it lives in ``logical_schema``, which a path with no
records never calls. So the rule is asked over ordered NAMES, fed by
``records[0].keys()`` on one path and by the structure the source declares on
the other -- one rule, never two implementations.

Most tests here open a real DuckDB, because the whole claim is about what an
engine actually does with a file.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from rey_lib.db.db_adapter import DBAdapter, _PROVIDER_CONTRACT_CAPABILITIES
from rey_lib.db.database_objects import DatabaseObjectIdentity
from rey_lib.db.duckdb_utils import insert_from_path
from rey_lib.errors.error_utils import DatabaseError
from rey_lib.load import load_operation
from rey_lib.files.data_file import data_file_for
from rey_lib.data.data_transform import IdentityTransform

_TARGET = DatabaseObjectIdentity(
    connection="c", catalog="", schema="main", name="asset",
)
_COLUMNS = ["asset_id", "name"]


def _csv(tmp_path: Path, body: str = "1,Alpha\n2,Beta\n", *,
         header: str = "asset_id,name", name: str = "source.csv") -> Path:
    path = tmp_path / name
    path.write_text(f"{header}\n{body}", encoding="utf-8")
    return path


def _duck(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    """A real DuckDB with the destination already there."""
    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    conn.execute('CREATE TABLE "main"."asset" (asset_id VARCHAR, name VARCHAR)')
    return conn


class _Adapter(DBAdapter):
    """The real adapter, pinned to DuckDB so dispatch is genuine.

    Subclassed rather than faked: the whole question is whether the provider
    contract resolves ``insert_from_path`` from the duckdb module, and a
    double would answer that by construction instead of by fact.
    """

    def _provider_for_conn(self, _conn) -> str:
        return "duckdb"


def _run(tmp_path, monkeypatch, run_log, conn, source, *, declared=None,
         adapter=None, transform=None, captured=None):
    """Run _load_one_file against a real connection.

    ``captured`` receives whatever validation the load recorded, patched the
    way every other loader test records it -- the run log writes JSONL, and
    the assertion here is about the NAME the check reported under.
    """
    if captured is not None:
        monkeypatch.setattr(
            load_operation, "log_validation_result",
            lambda _run_log, **kwargs: captured.update(kwargs),
        )
    monkeypatch.setattr(load_operation, "_db_adapter", adapter or _Adapter())
    monkeypatch.setattr(load_operation, "execute_movements", lambda *a, **k: None)
    monkeypatch.setattr(
        load_operation, "shared_connection",
        lambda _ctx, _name: SimpleNamespace(handle=lambda: conn),
    )
    return load_operation._load_one_file(
        data_file_for(source, file_type="CSV", encoding="utf-8"),
        IdentityTransform(columns=declared) if transform is None else transform,
        _TARGET,
        ctx=SimpleNamespace(log_depth=0), run_log=run_log,
        transform_cfg=SimpleNamespace(file_type="CSV", encoding="utf-8"),
        load_cfg=SimpleNamespace(
            name="my_load",
            load=SimpleNamespace(connection="c", destination_table="main.asset"),
            movements=SimpleNamespace(failure=[], success=[])),
        paths=SimpleNamespace(),
    )


class TestTheRecordsAreNeverMaterialized:
    """THE CENTRAL CLAIM, counted rather than timed."""

    def test_the_file_is_never_read_into_records(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """Instrumented on the source itself, so nothing can read it quietly."""
        from rey_lib.files.data_file.delimited import DelimitedHeaderFile

        reads: list[str] = []
        for name in ("read", "read_validated"):
            real = getattr(DelimitedHeaderFile, name)
            monkeypatch.setattr(
                DelimitedHeaderFile, name,
                (lambda n, r: lambda self, *a, **k: (
                    reads.append(n), r(self, *a, **k))[1])(name, real),
            )

        conn = _duck(tmp_path)
        loaded = _run(tmp_path, monkeypatch, run_log, conn, _csv(tmp_path))

        assert loaded == 2
        assert reads == [], f"the materialised path was entered: {reads}"

    def test_the_rows_actually_arrive(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """Equivalent, not merely quiet -- read the destination back."""
        conn = _duck(tmp_path)
        _run(tmp_path, monkeypatch, run_log, conn, _csv(tmp_path))

        assert conn.execute(
            'SELECT asset_id, name FROM "main"."asset" ORDER BY asset_id'
        ).fetchall() == [("1", "Alpha"), ("2", "Beta")]

    def test_the_insert_selects_by_name_not_by_position(
        self, tmp_path: Path
    ) -> None:
        """The destination's order builds the insert, so the file's cannot.

        Asserted at the primitive, because the loader never reaches it with
        a mismatched order -- the header check refuses that first. What has
        to hold here is that `columns` drives BOTH sides of the statement,
        so a source listing its fields the other way round still lands in
        the right columns rather than transposed.
        """
        conn = _duck(tmp_path)
        source = _csv(tmp_path, header="name,asset_id", body="Alpha,1\nBeta,2\n")

        insert_from_path(conn, "main", "asset", source, _COLUMNS)

        assert conn.execute(
            'SELECT asset_id, name FROM "main"."asset" ORDER BY asset_id'
        ).fetchall() == [("1", "Alpha"), ("2", "Beta")]


class TestTheChecksStillRun:
    """Neither validation is skipped, and neither is reimplemented."""

    def test_a_header_that_disagrees_with_the_table_is_refused(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        conn = _duck(tmp_path)
        source = _csv(tmp_path, header="asset_id,wrong_name")
        captured: dict = {}

        assert _run(tmp_path, monkeypatch, run_log, conn, source,
                    captured=captured) == 0
        assert conn.execute('SELECT count(*) FROM "main"."asset"').fetchone() == (0,)
        assert captured["validation_name"] == "load_header"

    @pytest.mark.parametrize(
        "declared,why",
        [
            pytest.param(["asset_id"], "extra", id="the file has one too many"),
            pytest.param(["asset_id", "name", "extra"], "missing",
                         id="the file is missing one"),
            pytest.param(["name", "asset_id"], "order",
                         id="SAME COLUMNS, DIFFERENT ORDER"),
        ],
    )
    def test_configured_columns_are_still_enforced(
        self, tmp_path: Path, monkeypatch, run_log, declared, why
    ) -> None:
        """THE CHECK THIS ROW EXISTED TO PRESERVE.

        `logical_schema` never runs on this path, so without the shared rule
        a declared column list would stop being enforced -- which is exactly
        how two feeds drifted unnoticed before it existed.

        The order case matters most: a set comparison passes it, and the
        mismatch would surface on a LATER load against a table created in the
        other order.
        """
        conn = _duck(tmp_path)
        captured: dict = {}

        loaded = _run(tmp_path, monkeypatch, run_log, conn, _csv(tmp_path),
                      declared=declared, captured=captured)

        assert loaded == 0
        assert conn.execute('SELECT count(*) FROM "main"."asset"').fetchone() == (0,)
        assert captured["validation_name"] == "configured_columns", why

    def test_matching_configured_columns_still_load(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """The control: the rule refuses mismatches, not everything."""
        conn = _duck(tmp_path)

        assert _run(tmp_path, monkeypatch, run_log, conn, _csv(tmp_path),
                    declared=["asset_id", "name"]) == 2


class TestAnEmptySourceIsStillRefused:
    """Reported by the engine rather than counted here."""

    def test_a_header_only_file_is_refused(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        conn = _duck(tmp_path)
        source = _csv(tmp_path, body="")
        captured: dict = {}

        assert _run(tmp_path, monkeypatch, run_log, conn, source,
                    captured=captured) == 0
        assert captured["validation_name"] == "load_rows"


class TestTheProviderPrimitive:
    """What `insert_from_path` promises, asserted against a real engine."""

    def test_it_returns_the_engine_s_count(self, tmp_path: Path) -> None:
        conn = _duck(tmp_path)

        assert insert_from_path(
            conn, "main", "asset", _csv(tmp_path), _COLUMNS,
        ) == 2

    def test_an_empty_source_returns_exactly_zero(self, tmp_path: Path) -> None:
        """The value the empty-source refusal rests on."""
        conn = _duck(tmp_path)

        assert insert_from_path(
            conn, "main", "asset", _csv(tmp_path, body=""), _COLUMNS,
        ) == 0

    def test_the_count_is_not_read_from_rowcount(self, tmp_path: Path) -> None:
        """THE TRAP for whoever edits this next.

        `rowcount` is the obvious attribute and DuckDB reports -1 for an
        INSERT. Reading it would make every load look empty and every file
        get refused.
        """
        conn = _duck(tmp_path)
        result = conn.execute(
            'INSERT INTO "main"."asset" VALUES (?, ?)', ["1", "Alpha"],
        )

        assert result.rowcount == -1
        assert result.fetchall() == [(1,)]

    def test_a_format_it_cannot_read_is_refused_not_guessed(
        self, tmp_path: Path
    ) -> None:
        from rey_lib.errors.error_utils import ConfigError

        conn = _duck(tmp_path)
        odd = tmp_path / "source.xyz"
        odd.write_text("nothing", encoding="utf-8")

        with pytest.raises(ConfigError, match="not a supported file format"):
            insert_from_path(conn, "main", "asset", odd, _COLUMNS)

    def test_a_failure_leaves_the_destination_untouched(
        self, tmp_path: Path
    ) -> None:
        """Why no transaction is opened around it.

        The statement is atomic, so a partial failure is not a partial
        insert -- and nothing here has to roll back work that a previous
        file already committed.
        """
        conn = _duck(tmp_path)
        conn.execute('DROP TABLE "main"."asset"')
        conn.execute('CREATE TABLE "main"."asset" (asset_id INTEGER, name VARCHAR)')
        conn.execute('INSERT INTO "main"."asset" VALUES (99, \'committed earlier\')')
        conn.commit()

        source = _csv(tmp_path, body="1,Alpha\nNOT_A_NUMBER,Beta\n3,Gamma\n")
        with pytest.raises(DatabaseError):
            insert_from_path(conn, "main", "asset", source, _COLUMNS)

        # The earlier row survives and nothing partial was added.
        assert conn.execute(
            'SELECT asset_id, name FROM "main"."asset"'
        ).fetchall() == [(99, "committed earlier")]
        assert conn.execute("SELECT 1").fetchone() == (1,)


class TestEveryGateRefusesOnItsOwn:
    """Four questions, each asserted alone.

    A predicate that answered False for the wrong reason would satisfy a
    single test, so each gate is broken in isolation and the materialised
    path must run.
    """

    @staticmethod
    def _possible(**over) -> bool:
        defaults = dict(
            source=SimpleNamespace(declares_structure=True),
            transform=IdentityTransform(),
            loader=SimpleNamespace(adapter=SimpleNamespace(
                supports_provider_capability=lambda *_a, **_k: True)),
            conn=object(),
            exists=True,
        )
        defaults.update(over)
        return load_operation._non_row_execution_possible(**defaults)

    def test_all_four_together_permit_it(self) -> None:
        assert self._possible() is True

    def test_a_transform_with_no_equivalent_form_refuses(self) -> None:
        class _RowOnly(IdentityTransform):
            def execution_form(self):
                return None

        assert self._possible(transform=_RowOnly()) is False

    def test_an_absent_transform_refuses(self) -> None:
        assert self._possible(transform=None) is False

    def test_a_source_that_only_exhibits_its_structure_refuses(self) -> None:
        """A keyed file names its columns on every record, so it must be read."""
        assert self._possible(
            source=SimpleNamespace(declares_structure=False)
        ) is False

    def test_an_absent_destination_refuses(self) -> None:
        """Creating one needs a schema, and a schema needs records."""
        assert self._possible(exists=False) is False

    def test_a_provider_without_the_capability_refuses(self) -> None:
        assert self._possible(
            loader=SimpleNamespace(adapter=SimpleNamespace(
                supports_provider_capability=lambda *_a, **_k: False))
        ) is False

    def test_a_keyed_source_takes_the_materialised_path_end_to_end(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """The gate, proven through the loader rather than only in isolation."""
        source = tmp_path / "source.jsonl"
        source.write_text('{"asset_id": "1", "name": "Alpha"}\n', encoding="utf-8")

        monkeypatch.setattr(load_operation, "_db_adapter", _Adapter())
        monkeypatch.setattr(load_operation, "execute_movements", lambda *a, **k: None)
        conn = _duck(tmp_path)
        monkeypatch.setattr(
            load_operation, "shared_connection",
            lambda _ctx, _name: SimpleNamespace(handle=lambda: conn),
        )
        loaded = load_operation._load_one_file(
            data_file_for(source, file_type="JSONL", encoding="utf-8"),
            IdentityTransform(), _TARGET,
            ctx=SimpleNamespace(log_depth=0), run_log=run_log,
            transform_cfg=SimpleNamespace(file_type="JSONL", encoding="utf-8"),
            load_cfg=SimpleNamespace(
                name="my_load",
                load=SimpleNamespace(connection="c", destination_table="main.asset"),
                movements=SimpleNamespace(failure=[], success=[])),
            paths=SimpleNamespace(),
        )

        assert loaded == 1


class TestSupportIsResolvedNotDeclaredTwice:
    """The capability answers from the provider module, as the others do."""

    def test_duckdb_has_it(self, tmp_path: Path) -> None:
        assert _Adapter().supports_provider_capability(
            _duck(tmp_path), "insert_from_path",
        ) is True

    @pytest.mark.parametrize("provider", ["postgres", "mysql"])
    def test_a_provider_without_the_function_answers_no(self, provider) -> None:
        """Absent, not listed as unsupported. A second table could disagree."""
        class _Pinned(DBAdapter):
            def _provider_for_conn(self, _conn) -> str:
                return provider

        assert _Pinned().supports_provider_capability(
            object(), "insert_from_path",
        ) is False

    def test_it_is_a_declared_capability(self) -> None:
        """So an unknown-name typo is still refused rather than answered."""
        assert "insert_from_path" in _PROVIDER_CONTRACT_CAPABILITIES
