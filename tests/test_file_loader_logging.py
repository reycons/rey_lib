"""Structured run-log evidence emitted by shared file_loader boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from rey_lib.files import file_loader


class Config(SimpleNamespace):
    def items(self):
        return vars(self).items()


def _ctx(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        log_file=str(tmp_path / "loader.run.jsonl"),
        owner_app_name="rey_loader",
    )


def _records(ctx: SimpleNamespace) -> list[dict]:
    return [
        json.loads(line)
        for line in Path(ctx.run_log_path).read_text(encoding="utf-8").splitlines()
    ]


def test_transform_unmatched_header_logs_validation_result(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    inbox_file = tmp_path / "incoming.csv"
    inbox_file.write_text("bad,header\n1,2\n", encoding="utf-8")
    data_source = SimpleNamespace(
        name="trades",
        paths=SimpleNamespace(rejected_path=str(tmp_path / "rejected")),
        transforms=[
            Config(
                name="trade_transform",
                version="v01",
                header="expected,header",
                encoding="utf-8",
                movements=Config(),
            )
        ],
    )

    assert file_loader.transform_one(ctx, data_source, inbox_file) is False

    record = next(r for r in _records(ctx) if r["record_type"] == "VALIDATION_RESULT")
    assert record["validation_name"] == "transform_header"
    assert record["status"] == "failed"
    assert record["path"].endswith("incoming.csv")
    assert record["data_source"] == "trades"
    assert (tmp_path / "rejected" / "incoming.csv").exists()


def test_transform_unmatched_header_does_not_log_sql_execution(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    inbox_file = tmp_path / "incoming.csv"
    inbox_file.write_text("bad,header\n1,2\n", encoding="utf-8")
    data_source = SimpleNamespace(
        name="trades",
        paths=SimpleNamespace(rejected_path=str(tmp_path / "rejected")),
        transforms=[
            Config(
                name="trade_transform",
                version="v01",
                header="expected,header",
                encoding="utf-8",
                movements=Config(),
            )
        ],
    )

    file_loader.transform_one(ctx, data_source, inbox_file)

    assert all(r["record_type"] != "SQL_EXECUTION" for r in _records(ctx))
