"""
Shared test fixtures for file_operator tests.
"""

from __future__ import annotations

import pytest
from pathlib import Path


@pytest.fixture()
def sample_csv(tmp_path: Path) -> Path:
    """Write a small test CSV and return its path."""
    p = tmp_path / "test_feed.csv"
    p.write_text(
        "ACCOUNT NUMBER,NAME,AMOUNT,DATE\n"
        "12345,SMITH,1500.00,2026-01-15\n"
        "67890,JONES,2200.50,2026-01-16\n"
        "11111,BROWN,300.00,2026-01-17\n"
        "22222,WHITE,750.25,2026-01-18\n"
        "33333,GREEN,90.00,2026-01-19\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture()
def sample_fixed_width(tmp_path: Path) -> Path:
    """Write a small fixed-width test file and return its path."""
    p = tmp_path / "test_fw.txt"
    p.write_text(
        "12345     SMITH     001500\n"
        "67890     JONES     002200\n"
        "11111     BROWN     000300\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture()
def row_dicts() -> list[dict]:
    """Return a minimal list of row dicts for testing."""
    return [
        {"ID": str(i), "NAME": f"NAME_{i}", "AMT": str(i * 10)}
        for i in range(1, 21)
    ]


# ---------------------------------------------------------------------------
# Run logging
# ---------------------------------------------------------------------------

def make_run_log(
    tmp_path,
    *,
    app: str = "data_engine",
    run_id: str = "00000000-0000-4000-8000-000000000001",
    run_timestamp: str = "20260822_000000",
    path: str | None = None,
):
    """Build a RunLog writing into ``tmp_path``.

    Run logging is owned by ``RunLog``; a test that writes records takes one
    rather than handing logging a context to read fields off.
    """
    from pathlib import Path

    from rey_lib.logs.log_record_builder import LogRecordBuilder
    from rey_lib.logs.log_writer import JsonLogWriter, LogWriter
    from rey_lib.logs.run_log import RunLog

    # The JSONL output is the logging writer's (logging.run_store: jsonl).
    json_path = path or str(Path(tmp_path) / f"{app or 'app'}.{run_timestamp}.jsonl")
    return RunLog(
        app=app,
        run_id=run_id,
        run_timestamp=run_timestamp,
        writer=LogWriter("jsonl", json=JsonLogWriter(json_path)),
        builder=LogRecordBuilder(run_id=run_id, run_timestamp=run_timestamp, app=app),
    )


@pytest.fixture()
def run_log(tmp_path):
    """The run log a test writes records through."""
    return make_run_log(tmp_path)
