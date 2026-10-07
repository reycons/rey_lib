"""Rejection rows go through the canonical objects (backlog 751).

The rejection writer names a connection configured in ``ctx.connections``,
takes the shared Connection for it, and writes through DataLoader -- create the
table if it is absent, then append. Asserted against a real in-memory DuckDB.
A failure is a warning and never the transform's error.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.db.connection import shared_connection
from rey_lib.files import file_loader

_ERRORS = [
    (3, "Amount", "abc", ValueError("not a number")),
    (5, "Date", None, ValueError("no date")),
]


def _ctx(name: str, *, configured: bool = True) -> Any:
    """A context whose rejection connection is (or is not) configured."""
    connections = (
        [SimpleNamespace(name=name, provider="duckdb", database=":memory:")]
        if configured else []
    )
    return SimpleNamespace(
        connections=connections,
        rejection=SimpleNamespace(connection=name, table="main.TransformRejection"),
        batch_id=42,
    )


def test_rejections_are_appended_through_the_configured_connection() -> None:
    ctx = _ctx("rejections_751_append")

    file_loader._write_rejections(ctx, Path("in/trades.csv"), _ERRORS)
    file_loader._write_rejections(ctx, Path("in/trades.csv"), _ERRORS[:1])

    handle = shared_connection(ctx, "rejections_751_append").handle()
    rows = handle.execute(
        'SELECT "FileName", "RowNum", "ColumnName", "RawValue", "BatchID" '
        'FROM main."TransformRejection" ORDER BY "RowNum"'
    ).fetchall()
    assert rows == [
        ("trades.csv", 3, "Amount", "abc", 42),
        ("trades.csv", 3, "Amount", "abc", 42),
        ("trades.csv", 5, "Date", None, 42),
    ]


def test_an_unconfigured_connection_is_a_warning_not_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = _ctx("rejections_751_missing", configured=False)

    with caplog.at_level(logging.WARNING):
        file_loader._write_rejections(ctx, Path("in/trades.csv"), _ERRORS)

    assert "Failed to write rejection rows for 'trades.csv'" in caplog.text


def test_the_writer_knows_nothing_beneath_the_objects() -> None:
    writer = inspect.getsource(file_loader._write_rejections)
    module = Path(file_loader.__file__).read_text(encoding="utf-8")

    for absent in ("get_connection", "bulk_insert", "sql_configs", "DatabaseTable"):
        assert absent not in writer, absent
    assert "DBAdapter" not in module.split('"""', 2)[2].split("def ", 1)[0]  # no import
    assert "_db_adapter" not in module
