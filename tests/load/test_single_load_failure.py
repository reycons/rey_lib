"""A single load that fails in the database fails the run (backlog 515).

A configured batch has a movement policy: one file can fail, be routed to
failure and count 0 while the batch goes on. A single load has none
(movements is None): its one transfer failing IS the load failing, so the
DatabaseError propagates instead of being reported as success with 0 rows.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.db.database_objects import DatabaseObjectIdentity
from rey_lib.errors.error_utils import DatabaseError
from rey_lib.files.data_file import data_file_for
from rey_lib.load import load_operation

_TARGET = DatabaseObjectIdentity(connection="w", catalog="", schema="s", name="t")


class _FailingLoader:
    """A destination mechanic whose first database question fails."""

    create_destination = False
    replace_destination = False
    recreate_destination = False

    def destination_columns(self, _conn: Any, _target: Any) -> Any:
        raise DatabaseError("relation s.t is locked")


@pytest.fixture(autouse=True)
def database(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A connection that can be rolled back, and a loader that fails."""
    rolled_back: list[str] = []
    monkeypatch.setattr(
        load_operation, "shared_connection",
        lambda _ctx, _name: SimpleNamespace(handle=lambda: SimpleNamespace(
            commit=lambda: None, rollback=lambda: rolled_back.append("rollback"),
        )),
    )
    monkeypatch.setattr(load_operation, "_build_data_loader",
                        lambda *_a, **_k: _FailingLoader())
    return rolled_back


@pytest.fixture()
def csv_file(tmp_path: Path) -> Path:
    held = tmp_path / "in.csv"
    held.write_text("a\n1\n", encoding="utf-8")
    return held


def test_a_failed_query_load_raises(run_log, database: list[str]) -> None:
    with pytest.raises(DatabaseError, match="locked"):
        load_operation.load_query_to_table(
            SimpleNamespace(log_depth=0), run_log, "select 1 as a", "src", "s.t", "w",
        )
    assert database == ["rollback"]


def test_a_failed_direct_file_load_raises(run_log, csv_file: Path) -> None:
    with pytest.raises(DatabaseError, match="locked"):
        load_operation.load_file_to_table(
            SimpleNamespace(log_depth=0), run_log, csv_file, "s.t", "w", file_type="csv",
        )


def test_a_configured_batch_still_counts_a_failed_file_as_zero(
    run_log, csv_file: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A movement policy is set, so the file is routed and the batch goes on."""
    moved: list[Any] = []
    monkeypatch.setattr(load_operation, "execute_movements",
                        lambda *args, **_k: moved.append(args))

    loaded = load_operation._load_one_file(
        data_file_for(csv_file, file_type="CSV", encoding="utf-8"),
        None,
        _TARGET,
        ctx=SimpleNamespace(log_depth=0), run_log=run_log,
        loader=_FailingLoader(),
        load_cfg=SimpleNamespace(
            name="a_load", load=SimpleNamespace(destination_table="s.t"),
            movements=SimpleNamespace(failure=["f"], success=[]),
        ),
        paths=SimpleNamespace(),
    )

    assert loaded == 0
    assert moved, "a failed file in a batch is routed to failure"
