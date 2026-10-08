"""
Tests for the logging handler: one record built once, written by the writer.

Covers:
1. No diagnostics config — legacy flat ctx_fields behavior unchanged.
2. diagnostics.error.dump_ctx = true — ctx_dump present with whitelisted fields only.
3. diagnostics.error.dump_ctx = false — no ctx_dump, event fields still logged.
4. One writer, one shape (backlog logging.app_logger.s8_json_output_cleanup): bound
   or unbound, an event is the same record in the same file; the database row keeps
   today's content; the writer's guard drops what its own persistence produces.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.errors.error_utils import AppError
from rey_lib.logs.jsonl_handler import JsonlHandler
from rey_lib.logs.log_record_builder import LogRecordBuilder
from tests.conftest import MintingDatabaseWriter, make_log_writer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(**attrs: Any) -> SimpleNamespace:
    """Return a minimal ctx-like object with the given attributes."""
    return SimpleNamespace(**attrs)


def _make_diag(level: str, **flags: Any) -> SimpleNamespace:
    """Build a minimal ctx with ctx.diagnostics.{level} configured."""
    level_ns  = SimpleNamespace(**flags)
    diag_ns   = SimpleNamespace(**{level: level_ns})
    return _make_ctx(diagnostics=diag_ns, batch_id=42, env="test", secret="s3cr3t")


def _emit_and_read(
    handler: JsonlHandler,
    message: str = "test message",
    level: int = logging.ERROR,
    extra: dict | None = None,
) -> dict:
    """Emit one log record through handler and return the parsed JSONL dict."""
    logger = logging.getLogger(f"test.{id(handler)}")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.propagate = False

    logger.log(level, message, extra=extra or {})

    handler.close()
    path = handler._writer.json_path()
    return json.loads(path.read_text(encoding="utf-8").strip())


def _make_handler(
    tmp_path: Path, ctx: Any, ctx_fields: tuple = (), *, destination: str = "jsonl",
    database: Any = None,
) -> tuple[JsonlHandler, Path]:
    """Return (handler, jsonl_path): the handler over a builder and a writer."""
    p = tmp_path / "test.jsonl"
    writer = make_log_writer(tmp_path, destination=destination, path=str(p), database=database)
    builder = LogRecordBuilder(run_id=7, run_timestamp="20260822_000000", ctx=ctx,
                               ctx_fields=ctx_fields)
    return JsonlHandler(builder, writer), p


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLegacyFlatBehavior:
    """Scenario 1 — no diagnostics config, constructor ctx_fields used flat."""

    def test_flat_fields_written_to_record(self, tmp_path: Path) -> None:
        """ctx_fields from constructor are written as flat top-level keys."""
        ctx = _make_ctx(batch_id=99, env="dev")
        handler, path = _make_handler(tmp_path, ctx, ctx_fields=("batch_id", "env"))

        logger = logging.getLogger(f"test.legacy.{tmp_path}")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False
        logger.error("legacy test")
        handler.close()

        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert record["batch_id"] == 99
        assert record["env"] == "dev"
        assert "ctx_dump" not in record

    def test_missing_ctx_attr_writes_none(self, tmp_path: Path) -> None:
        """A ctx_field that doesn't exist on ctx writes None without raising."""
        ctx = _make_ctx(batch_id=1)
        handler, path = _make_handler(tmp_path, ctx, ctx_fields=("batch_id", "missing_attr"))

        logger = logging.getLogger(f"test.missing.{tmp_path}")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False
        logger.error("missing attr test")
        handler.close()

        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert record["batch_id"] == 1
        assert record["missing_attr"] is None


class TestDumpCtxTrue:
    """Scenario 2 — diagnostics.error.dump_ctx = true."""

    def test_ctx_dump_contains_whitelisted_fields(self, tmp_path: Path) -> None:
        """ctx_dump is a nested dict with only the fields listed in ctx_fields."""
        ctx = _make_diag(
            "error",
            dump_ctx=True,
            ctx_fields=["batch_id", "env"],
            dump_stack_trace=False,
            dump_sql=True,
        )
        handler, path = _make_handler(tmp_path, ctx)

        logger = logging.getLogger(f"test.dumpctx.{tmp_path}")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False
        logger.error("dump ctx test")
        handler.close()

        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert "ctx_dump" in record
        assert record["ctx_dump"]["batch_id"] == 42
        assert record["ctx_dump"]["env"] == "test"
        assert "secret" not in record["ctx_dump"]

    def test_ctx_dump_excludes_none_values(self, tmp_path: Path) -> None:
        """Fields that are None on ctx are excluded from ctx_dump."""
        ctx = _make_diag(
            "error",
            dump_ctx=True,
            ctx_fields=["batch_id", "destination_table"],
            dump_stack_trace=False,
            dump_sql=True,
        )
        # destination_table not set on ctx
        handler, path = _make_handler(tmp_path, ctx)

        logger = logging.getLogger(f"test.none.{tmp_path}")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False
        logger.error("none field test")
        handler.close()

        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert "ctx_dump" in record
        assert "destination_table" not in record["ctx_dump"]

    def test_event_fields_still_present(self, tmp_path: Path) -> None:
        """operation and other extra fields appear alongside ctx_dump."""
        ctx = _make_diag(
            "error",
            dump_ctx=True,
            ctx_fields=["batch_id"],
            dump_stack_trace=False,
            dump_sql=True,
        )
        handler, path = _make_handler(tmp_path, ctx)

        logger = logging.getLogger(f"test.eventfields.{tmp_path}")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False
        logger.error("event fields test", extra={"operation": "REJECT", "source_path": "/tmp/f.csv"})
        handler.close()

        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert record["operation"] == "REJECT"
        assert record["source_path"] == "/tmp/f.csv"
        assert "ctx_dump" in record


class TestDotPathResolution:
    """Dot-separated paths in ctx_fields are walked generically."""

    def test_dot_path_resolved_into_ctx_dump(self, tmp_path: Path) -> None:
        """A path like 'loads.load.destination_table' is walked on ctx."""
        load_ns  = SimpleNamespace(destination_table="NaviStage.Advantage_SCH.transaction")
        loads_ns = SimpleNamespace(name="advantage_transactions", load=load_ns)
        ctx = SimpleNamespace(
            loads=loads_ns,
            batch_id=1,
            diagnostics=SimpleNamespace(
                error=SimpleNamespace(
                    enabled=True,
                    dump_ctx=True,
                    ctx_fields=["loads.name", "loads.load.destination_table"],
                    dump_stack_trace=False,
                    dump_sql=True,
                )
            ),
        )
        handler, path = _make_handler(tmp_path, ctx)

        logger = logging.getLogger(f"test.dotpath.{tmp_path}")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False
        logger.error("dot path test")
        handler.close()

        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert record["ctx_dump"]["loads.name"] == "advantage_transactions"
        assert record["ctx_dump"]["loads.load.destination_table"] == "NaviStage.Advantage_SCH.transaction"

    def test_missing_segment_excluded(self, tmp_path: Path) -> None:
        """A path whose intermediate segment is None is excluded from ctx_dump."""
        ctx = SimpleNamespace(
            loads=None,
            diagnostics=SimpleNamespace(
                error=SimpleNamespace(
                    enabled=True,
                    dump_ctx=True,
                    ctx_fields=["loads.load.destination_table"],
                    dump_stack_trace=False,
                    dump_sql=True,
                )
            ),
        )
        handler, path = _make_handler(tmp_path, ctx)

        logger = logging.getLogger(f"test.missing_seg.{tmp_path}")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False
        logger.error("missing segment test")
        handler.close()

        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert record.get("ctx_dump", {}).get("loads.load.destination_table") is None


class TestDumpCtxFalse:
    """Scenario 3 — diagnostics.error.dump_ctx = false."""

    def test_no_ctx_dump_in_record(self, tmp_path: Path) -> None:
        """ctx_dump is absent when dump_ctx is false."""
        ctx = _make_diag(
            "error",
            dump_ctx=False,
            ctx_fields=["batch_id", "env"],
            dump_stack_trace=False,
            dump_sql=True,
        )
        handler, path = _make_handler(tmp_path, ctx)

        logger = logging.getLogger(f"test.nodump.{tmp_path}")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False
        logger.error("no dump test")
        handler.close()

        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert "ctx_dump" not in record

    def test_event_fields_still_logged(self, tmp_path: Path) -> None:
        """Event-level extra fields are present even when dump_ctx is false."""
        ctx = _make_diag(
            "error",
            dump_ctx=False,
            ctx_fields=["batch_id"],
            dump_stack_trace=False,
            dump_sql=True,
        )
        handler, path = _make_handler(tmp_path, ctx)

        logger = logging.getLogger(f"test.eventsonly.{tmp_path}")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False
        logger.error("event only", extra={"operation": "LOAD", "rows": 500})
        handler.close()

        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert record["operation"] == "LOAD"
        assert record["rows"] == 500
        assert "ctx_dump" not in record


# ---------------------------------------------------------------------------
# One writer, one shape (backlog logging.app_logger.s8_json_output_cleanup).
# ---------------------------------------------------------------------------

def _logger_for(handler: JsonlHandler) -> logging.Logger:
    logger = logging.getLogger(f"test.bound.{id(handler)}")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _bound_run_log(handler: JsonlHandler) -> Any:
    """A RunLog sharing the handler's writer and builder, as open_run_log builds it."""
    from rey_lib.logs.run_log import RunLog

    run_log = RunLog(app="sample", run_id=7, run_timestamp="20260822_000000",
                     writer=handler._writer, builder=handler._builder)
    run_log.set_nest_level("app")
    return run_log


class TestOneWriterOneShape:

    def test_bound_and_unbound_events_are_one_shape_in_one_file(self, tmp_path: Path) -> None:
        from rey_lib.logs.record_enrichment import bind_run, reset_run_binding

        handler, path = _make_handler(tmp_path, _make_ctx())
        logger = _logger_for(handler)
        logger.info("before")
        bind_run(_bound_run_log(handler))
        try:
            logger.warning("during %s", "the run")
        finally:
            reset_run_binding()
        handler.close()

        before, during = _lines(path)
        for line in (before, during):
            assert {"record_type", "sequence", "parent_sequence", "depth", "level",
                    "source", "run_id", "nest_level"} <= set(line)
        assert (before["record_type"], before["nest_level"]) == ("INFO", 0)
        assert (during["record_type"], during["message"]) == ("WARNING", "during the run")
        assert (during["nest_level"], during["app"]) == (3, "sample")

    @pytest.mark.parametrize("level, record_type", [
        (logging.WARNING, "WARNING"), (logging.INFO, "INFO"), (logging.DEBUG, "INFO"),
    ])
    def test_both_writes_the_row_first_and_the_line_names_it(
        self, tmp_path: Path, level: int, record_type: str,
    ) -> None:
        database = MintingDatabaseWriter()
        handler, path = _make_handler(tmp_path, _make_ctx(), destination="both", database=database)
        _logger_for(handler).log(level, "said %s", "this", extra={"step": "load"})
        handler.close()

        [line] = _lines(path)
        [row] = database.rows
        assert line["run_log_id"] == row["run_log_id"]
        assert (line["record_type"], line["level"], line["step"]) == (
            record_type, logging.getLevelName(level), "load")
        # The row keeps today's content, not the structured envelope.
        assert row["record_type"] == record_type and row["message"] == "said this"
        assert row[record_type.lower()] == {"logger": f"test.bound.{id(handler)}"}
        assert not {"sequence", "depth", "step", "level"} & set(row)

    def test_db_writes_no_file(self, tmp_path: Path) -> None:
        database = MintingDatabaseWriter()
        handler, path = _make_handler(tmp_path, _make_ctx(), destination="db", database=database)
        _logger_for(handler).warning("only a row")
        handler.close()

        assert handler._writer.json_path() is None
        assert not path.exists()
        assert [row["message"] for row in database.rows] == ["only a row"]

    def test_what_persistence_produces_is_dropped_everywhere(self, tmp_path: Path) -> None:
        class _Noisy(MintingDatabaseWriter):
            logger: logging.Logger

            def write(self, record: dict[str, Any]) -> int:
                self.logger.warning("from inside the write")
                return super().write(record)

        database = _Noisy()
        handler, path = _make_handler(tmp_path, _make_ctx(), destination="both", database=database)
        database.logger = _logger_for(handler)
        database.logger.warning("outer")
        handler.close()

        assert [line["message"] for line in _lines(path)] == ["outer"]
        assert [row["message"] for row in database.rows] == ["outer"]


class _Refused(AppError):
    """An application's own error, rooted in the canonical error object."""


def _log_app_error(logger: logging.Logger, level: int) -> None:
    """What an application does: raise an AppError and log it. Nothing else."""
    try:
        raise _Refused("the transform could not be created")
    except _Refused as exc:
        logger.log(level, "refused: %s", exc, exc_info=exc)


class TestAnAppErrorIsOneErrorRecord:

    @pytest.mark.parametrize("level", [logging.ERROR, logging.CRITICAL])
    def test_it_is_an_error_record_in_both_outputs(self, tmp_path: Path, level: int) -> None:
        database = MintingDatabaseWriter()
        handler, path = _make_handler(tmp_path, _make_ctx(), destination="both", database=database)
        _log_app_error(_logger_for(handler), level)
        handler.close()

        [line] = _lines(path)
        [row] = database.rows
        assert row["record_type"] == line["record_type"] == "ERROR"
        assert line["level"] == logging.getLevelName(level)
        assert "the transform could not be created" in row["message"]
        assert row["error_message"]["error_type"] == "_Refused"
        assert "the transform could not be created" in line["exception"]


class TestDepthFromTheRecord:
    """S4: the handler reads record.log_depth; it owns no depth of its own."""

    @staticmethod
    def _write(tmp_path: Path, extra: dict | None = None) -> dict:
        handler, path = _make_handler(tmp_path, _make_ctx(log_depth=7))
        logger = logging.getLogger(f"test.depth.{id(handler)}")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.propagate = False
        logger.info("depth", extra=extra or {})
        handler.close()
        return json.loads(path.read_text(encoding="utf-8").strip())

    def test_depth_is_read_from_the_record_and_not_written_as_an_extra(self, tmp_path: Path) -> None:
        written = self._write(tmp_path, {"log_depth": 2})

        assert written["depth"] == 2
        assert "log_depth" not in written

    def test_a_record_without_a_depth_is_at_depth_zero(self, tmp_path: Path) -> None:
        written = self._write(tmp_path)

        assert written["depth"] == 0
        assert written["parent_sequence"] is None
